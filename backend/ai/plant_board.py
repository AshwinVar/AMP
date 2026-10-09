"""The plant board: one day, every machine, as the customer drew it.

WHAT THIS ANSWERS. A moulder's spreadsheet asked for five hourly graphs and
seven monthly tables. Every figure on it is a CONVERSION away from a number no
controller holds:

    parts        = shots x ACTIVE cavities
    kg consumed  = parts x part_weight_g / 1000
    revenue      = parts x price_per_piece
    ideal rate   = 3600 / ideal_cycle_time_s x active cavities
    shift rate   = revenue in an 8-hour block / 8

The shot count is the only input from the machine. Everything else comes from
`PartSpec`, which is process-planning and commercial data a person enters once.

WHY EACH SERIES CARRIES ITS OWN AVAILABILITY. A read-model that returned zeros
for something nobody measured would be indistinguishable from a plant that
consumed no power and packed nothing, and a chart drawn from those zeros is a
lie with axes on it. So every series says whether it has a source, and names
what is missing when it does not. The screen renders the gap and the fix, not a
flat line at zero.

POWER WAS ONE OF THOSE SERIES AND IS NOT ANY MORE, which is the whole argument
for making availability a property of the DATA rather than a constant in the
code. It was hardcoded unavailable on the premise that no meter was installed.
Then a controller on a customer's floor turned out to have been counting kWh per
hour for fourteen months -- 25,133 of them against 1,316,137 shots -- and
nothing had ever read it. The card had been honest and wrong at the same time.
It now draws whatever was measured and says so when nothing was; a plant that
half-measures gets half a chart and an honest hole where the rest would be.

Packing still has no source: the hourly export carries no good/reject split, so
there is nothing to draw. It stays unavailable until something counts it.

WHICH PART A MACHINE IS MAKING comes from the TOOL fitted to it: a mould makes
one part, and moving the mould moves the part. That is why the link lives on
ToolAsset.part_code rather than on the machine -- the customer changes moulds
roughly once a fortnight, and the part follows the mould, not the press.

THE SPEC IN FORCE IS THE ONE FOR THE DAY BEING READ. PartSpec is effective-dated
because price and cavity counts change. Reading today's price onto last month's
output would restate a month already reported, so every lookup is as-of.
"""
import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import models

HOURS = 24
SHIFT_HOURS = 8
SHIFTS = HOURS // SHIFT_HOURS

#: A record covering this many minutes or more describes a WHOLE DAY and is
#: never drawn as an hour -- see `hourly_only`.
#:
#: Deliberately 1440 and not 60. Most of AMP writes planned_minutes=480: the
#: factory simulator, reset_factory, onboard_tenant, the copilot eval fixtures.
#: Those shift-length records have always been bucketed by the hour of their
#: created_at and this is not the change that revisits it -- excluding them
#: would blank the board for every tenant that has ever been seeded. The defect
#: being fixed here is narrower and provable: a record that carries an entire
#: day, which has no hour in it at all.
DAY_MINUTES = 24 * 60

#: Below this fraction of the ideal rate an hour is drawn red. The customer's
#: own wording: "Red being less than acceptable level and green being acceptable
#: level." The threshold is a plant policy, not a law, so it is named here and
#: returned to the caller rather than hard-coded into a colour in the browser.
ACCEPTABLE = 0.80


def _unavailable(reason, fix):
    """A series with no source. Never zeros -- see the module docstring."""
    return {"available": False, "reason": reason, "fix": fix, "points": []}


#: What a reader is told when nothing measured the electricity.
NO_ENERGY = (
    "No machine is reporting electricity for this period.",
    "A controller that already counts kWh (many do -- check its hourly "
    "production page), a 3-phase Modbus meter, or one on the incomer with the "
    "plant total allocated by runtime.")


def _energy_hours(records, hours):
    """Hourly kWh, or None when nothing in the window measured any.

    NULL IS NOT ZERO, and the difference is the whole point of this function.
    `energy_kwh` is NULL on every record whose source did not report energy --
    every row written before the column existed, and every machine with no
    meter. Summing NULL as 0 would draw a flat line along the axis and say the
    plant consumed nothing, which is exactly the lie the power card was a
    placeholder to avoid.

    So: an hour with no measured record is NULL and is drawn as a gap, and a
    window where NOTHING measured anything returns None, which the caller turns
    back into an unavailable series. A plant that half-measures gets half a
    chart and an honest hole where the rest would be.
    """
    measured = [r for r in records if r.energy_kwh is not None]
    if not measured:
        return None
    by_hour = defaultdict(float)
    seen = set()
    for r in measured:
        if not r.created_at:
            continue
        by_hour[r.created_at.hour] += float(r.energy_kwh)
        seen.add(r.created_at.hour)
    if not seen:
        return None
    return [{"hour": h, "kwh": round(by_hour[h], 2) if h in seen else None}
            for h in range(hours)]


def _power(records, energy_points):
    """The day's electricity, in whichever of its three states is true.

    There are THREE, and collapsing them to two is how this card lies:

      nothing measured        no meter reported anything -> unavailable, with
                              the sentence that says what would fix it.
      measured, by the hour   the usual case -> a total and 24 bars.
      measured, by the DAY    a MONTH page gives a day's kWh with no hour in
                              it. There IS a meter and it DID report; what is
                              missing is the hour axis, not the measurement.

    The third state used to fall through to "no source is connected", which
    told a moulder his machine had no meter while AMP was holding the number
    that machine had measured. Reporting the total with `hours_known: false`
    says the true thing: we know the day, we cannot draw the hours.
    """
    measured = [r for r in records if r.energy_kwh is not None]
    if not measured:
        return _unavailable(*NO_ENERGY)
    return {
        "available": True,
        "unit": "kWh",
        # Every measured kWh, hourly or not -- never shrunk to fit the chart.
        "total": round(sum(float(r.energy_kwh) for r in measured), 2),
        "points": energy_points,
        # The same test the production rows use: do the hours account for the
        # day? Asking it as a sum rather than as "were all the records hourly"
        # means an hour that got lost on the way to the chart -- a record with
        # no timestamp, say -- also reads as unknown, instead of being quietly
        # dropped from a chart that still calls itself complete.
        "hours_known": energy_points is not None and round(
            sum(p["kwh"] or 0 for p in energy_points), 2) == round(
                sum(float(r.energy_kwh) for r in measured), 2),
    }


def spec_for(db, tenant, part_code, as_of):
    """The PartSpec in force for this part on this date, or None.

    Picks the latest row whose `effective_from` is not after the date. A part
    whose first spec starts tomorrow has no spec today, which is correct: AMP
    cannot price output made before anyone said what it was worth.
    """
    if not part_code:
        return None
    return (db.query(models.PartSpec)
              .filter(models.PartSpec.tenant_code == tenant,
                      models.PartSpec.part_code == part_code,
                      models.PartSpec.effective_from <= as_of)
              .order_by(models.PartSpec.effective_from.desc())
              .first())


def part_on_machine(db, tenant, machine_id):
    """The part a machine is making, via the tool fitted to it.

    None when no live tool is fitted or the tool names no part. The caller then
    reports parts but cannot price them, which is better than pricing them
    wrongly against whatever part happened to be first in the table.
    """
    tool = (db.query(models.ToolAsset)
              .filter(models.ToolAsset.tenant_code == tenant,
                      models.ToolAsset.machine_id == machine_id,
                      models.ToolAsset.status == "Active")
              .order_by(models.ToolAsset.id.asc())
              .first())
    return (tool.part_code if tool else None), tool


def _records(db, tenant, start, end):
    return (db.query(models.ProductionRecord)
              .filter(models.ProductionRecord.tenant_code == tenant,
                      models.ProductionRecord.created_at >= start,
                      models.ProductionRecord.created_at < end)
              .all())


def hourly_only(records):
    """Just the records that describe a single hour.

    A WHOLE-DAY record -- one transcribed off a controller's MONTH page, which
    prints a day's shots and nothing finer -- carries the whole day and is
    stamped at midnight, because midnight is the only honest stamp for a figure
    with no hour in it. Bucketing it by created_at.hour would draw a full day's
    output as one bar at 00:00 with twenty-three empty hours after it: a plant
    that worked three shifts shown as a plant that stopped before one o'clock.
    That is a worse lie than an empty chart, because it looks like data.

    So the hourly series is built from hourly records only. The day total is
    still taken from everything (see `day`), and the month view groups by day
    and reads these records correctly -- it is the hour axis, and only the hour
    axis, that they cannot answer.

    Shift-length records (planned_minutes=480), which most of AMP writes,
    are NOT excluded -- see DAY_MINUTES for why that is deliberate.
    """
    return [r for r in records if (r.planned_minutes or 0) < DAY_MINUTES]


def day(db, tenant, on: date):
    """Everything the board shows for one day."""
    start = datetime.combine(on, time.min)
    end = start + timedelta(days=1)
    machines = (db.query(models.Machine)
                  .filter(models.Machine.tenant_code == tenant)
                  .order_by(models.Machine.name.asc()).all())
    by_id = {m.id: m for m in machines}
    records = _records(db, tenant, start, end)
    hourly = hourly_only(records)

    # hour -> machine_id -> parts, from HOURLY records only.
    made = defaultdict(lambda: defaultdict(int))
    good = defaultdict(lambda: defaultdict(int))
    for r in hourly:
        if r.machine_id not in by_id or not r.created_at:
            continue
        h = r.created_at.hour
        made[h][r.machine_id] += int(r.total_count or 0)
        good[h][r.machine_id] += int(r.good_count or 0)

    # machine_id -> the day's output from EVERYTHING, hourly or not. A machine
    # whose only source is a MONTH page has a real day total and no hours; the
    # chart must not report its day as zero just because it cannot draw it.
    day_total = defaultdict(int)
    for r in records:
        if r.machine_id in by_id:
            day_total[r.machine_id] += int(r.total_count or 0)

    specs, tools = {}, {}
    for m in machines:
        code, tool = part_on_machine(db, tenant, m.id)
        tools[m.id] = tool
        specs[m.id] = spec_for(db, tenant, code, on)

    production, rm, shift = [], [], []
    for m in machines:
        spec = specs[m.id]
        ideal = round(spec.ideal_parts_per_hour) if spec else 0
        series, rm_series = [], []
        for h in range(HOURS):
            parts = made[h].get(m.id, 0)
            # An hour with no ideal declared has no acceptable level, so it is
            # neither green nor red -- it is unrated. Colouring it green would
            # claim a standard nobody set.
            if not ideal:
                status = "unrated"
            elif parts >= ideal * ACCEPTABLE:
                status = "ok"
            else:
                status = "low"
            series.append({"hour": h, "parts": parts, "ideal": ideal, "status": status})
            grams = parts * float(spec.part_weight_g) if spec else 0.0
            rm_series.append({"hour": h, "kg": round(grams / 1000.0, 3)})
        hourly_total = sum(p["parts"] for p in series)
        total = day_total.get(m.id, 0)
        # True when the hours account for the day. False means the day's figure
        # came from a source with no hour in it, so the bars below are not the
        # day -- and the caller has to say so instead of drawing a flat line.
        hours_known = total == hourly_total
        production.append({
            "machine_id": m.id, "machine": m.name,
            "part": spec.part_name if spec else None,
            "part_code": spec.part_code if spec else None,
            "tool": tools[m.id].tool_no if tools[m.id] else None,
            "ideal_per_hour": ideal,
            "average_per_hour": round(total / HOURS, 1),
            "total": total,
            "hours_known": hours_known,
            "hourly_total": hourly_total,
            "points": series,
        })
        rm.append({"machine_id": m.id, "machine": m.name,
                   "material": spec.material if spec else None,
                   "kg_total": round(sum(p["kg"] for p in rm_series), 3),
                   "points": rm_series})

        # Shift-hour rate: revenue produced in an 8-hour block, per hour.
        blocks = []
        for s in range(SHIFTS):
            lo, hi = s * SHIFT_HOURS, (s + 1) * SHIFT_HOURS
            parts = sum(made[h].get(m.id, 0) for h in range(lo, hi))
            revenue = parts * float(spec.price_per_piece) if spec else 0.0
            blocks.append({"shift": s + 1, "from_hour": lo, "to_hour": hi,
                           "parts": parts,
                           "revenue": round(revenue, 2),
                           "rate_per_hour": round(revenue / SHIFT_HOURS, 2)})
        shift.append({"machine_id": m.id, "machine": m.name,
                      "priced": spec is not None and float(spec.price_per_piece or 0) > 0,
                      "points": blocks})

    # Measured electricity, or nothing at all -- never zeros (_energy_hours).
    # Hourly records only: a day's kWh spread over no hours would stack onto
    # midnight exactly as a day's shots would.
    energy_points = _energy_hours(hourly, HOURS)

    return {
        "date": on.isoformat(),
        "acceptable_fraction": ACCEPTABLE,
        "machines": [{"id": m.id, "name": m.name, "status": m.status} for m in machines],
        "production": production,
        "rm_status": rm,
        "shift_rate": shift,
        "power": _power(records, energy_points),
        "packing": _unavailable(
            "Nothing records packed quantities.",
            "A packing entry screen, a weighing scale, or a count off the "
            "packing station."),
    }


def month(db, tenant, year: int, mon: int):
    """The monthly tables, from the same inputs as the daily board."""
    start = datetime(year, mon, 1)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1)
    machines = {m.id: m for m in db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant).all()}
    records = _records(db, tenant, start, end)

    as_of = date(year, mon, calendar.monthrange(year, mon)[1])
    specs = {}
    for mid in machines:
        code, _ = part_on_machine(db, tenant, mid)
        specs[mid] = spec_for(db, tenant, code, as_of)

    by_part, by_material, by_machine = defaultdict(int), defaultdict(float), defaultdict(float)
    good_by_part = defaultdict(int)
    for r in records:
        spec = specs.get(r.machine_id)
        parts = int(r.total_count or 0)
        if spec:
            by_part[spec.part_name] += parts
            good_by_part[spec.part_name] += int(r.good_count or 0)
            by_material[spec.material] += parts * float(spec.part_weight_g) / 1000.0
            by_machine[r.machine_id] += parts * float(spec.price_per_piece or 0)
        else:
            by_part["(no part assigned)"] += parts
            good_by_part["(no part assigned)"] += int(r.good_count or 0)

    # Hours in the month, for the shift-rate denominator.
    hours = (end - start).days * HOURS
    machine_rates = [{"machine": machines[mid].name,
                      "revenue": round(rev, 2),
                      "rate_per_hour": round(rev / hours, 2) if hours else 0.0}
                     for mid, rev in sorted(by_machine.items(),
                                            key=lambda kv: -kv[1])]
    # Measured electricity by day. Same rule as the daily board: a month where
    # nothing measured anything is unavailable, not a row of zeros.
    by_day = defaultdict(float)
    for r in records:
        if r.energy_kwh is not None and r.created_at:
            by_day[r.created_at.date()] += float(r.energy_kwh)
    month_energy = [{"day": d.isoformat(), "kwh": round(k, 2)}
                    for d, k in sorted(by_day.items())]

    return {
        "year": year, "month": mon,
        "itemwise_production": [{"part": p, "total": t, "good": good_by_part[p]}
                                for p, t in sorted(by_part.items(), key=lambda kv: -kv[1])],
        "rm_consumption": [{"material": mat, "kg": round(kg, 3)}
                           for mat, kg in sorted(by_material.items(), key=lambda kv: -kv[1])],
        "shift_rate_by_machine": machine_rates,
        "total_rate_per_hour": round(sum(r["rate_per_hour"] for r in machine_rates), 2),
        "total_revenue": round(sum(r["revenue"] for r in machine_rates), 2),
        # Per DAY across the month, not per hour -- the month's question is
        # "which days cost what", and 720 hourly bars answer nothing.
        "power": ({"available": True, "unit": "kWh", "points": month_energy,
                   "total": round(sum(p["kwh"] for p in month_energy), 1)}
                  if month_energy else _unavailable(*NO_ENERGY)),
        "packing": _unavailable("Nothing records packed quantities.",
                                "A packing entry screen or a weighing scale."),
    }
