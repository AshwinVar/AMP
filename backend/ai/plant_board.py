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

WHY EACH SERIES CARRIES ITS OWN AVAILABILITY. Two of the five graphs the
customer drew -- power and packing -- have NO SOURCE: no meter is installed and
nobody enters packing counts. A read-model that returned zeros for them would be
indistinguishable from a plant that consumed no power and packed nothing, and a
chart drawn from those zeros is a lie with axes on it. So every series says
whether it has a source, and names what is missing when it does not. The screen
renders "no source connected", not a flat line at zero.

WHICH PART A MACHINE IS MAKING comes from the TOOL fitted to it: a mould makes
one part, and moving the mould moves the part. That is why the link lives on
ToolAsset.part_code rather than on the machine -- the customer changes moulds
roughly once a fortnight, and the part follows the mould, not the press.

THE SPEC IN FORCE IS THE ONE FOR THE DAY BEING READ. PartSpec is effective-dated
because price and cavity counts change. Reading today's price onto last month's
output would restate a month already reported, so every lookup is as-of.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import models

HOURS = 24
SHIFT_HOURS = 8
SHIFTS = HOURS // SHIFT_HOURS

#: Below this fraction of the ideal rate an hour is drawn red. The customer's
#: own wording: "Red being less than acceptable level and green being acceptable
#: level." The threshold is a plant policy, not a law, so it is named here and
#: returned to the caller rather than hard-coded into a colour in the browser.
ACCEPTABLE = 0.80


def _unavailable(reason, fix):
    """A series with no source. Never zeros -- see the module docstring."""
    return {"available": False, "reason": reason, "fix": fix, "points": []}


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


def day(db, tenant, on: date):
    """Everything the board shows for one day."""
    start = datetime.combine(on, time.min)
    end = start + timedelta(days=1)
    machines = (db.query(models.Machine)
                  .filter(models.Machine.tenant_code == tenant)
                  .order_by(models.Machine.name.asc()).all())
    by_id = {m.id: m for m in machines}
    records = _records(db, tenant, start, end)

    # hour -> machine_id -> parts
    made = defaultdict(lambda: defaultdict(int))
    good = defaultdict(lambda: defaultdict(int))
    for r in records:
        if r.machine_id not in by_id or not r.created_at:
            continue
        h = r.created_at.hour
        made[h][r.machine_id] += int(r.total_count or 0)
        good[h][r.machine_id] += int(r.good_count or 0)

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
        total = sum(p["parts"] for p in series)
        production.append({
            "machine_id": m.id, "machine": m.name,
            "part": spec.part_name if spec else None,
            "part_code": spec.part_code if spec else None,
            "tool": tools[m.id].tool_no if tools[m.id] else None,
            "ideal_per_hour": ideal,
            "average_per_hour": round(total / HOURS, 1),
            "total": total,
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

    return {
        "date": on.isoformat(),
        "acceptable_fraction": ACCEPTABLE,
        "machines": [{"id": m.id, "name": m.name, "status": m.status} for m in machines],
        "production": production,
        "rm_status": rm,
        "shift_rate": shift,
        "power": _unavailable(
            "No energy meter is installed on any machine.",
            "A 3-phase Modbus meter per machine, or one on the incomer with the "
            "plant total allocated by runtime."),
        "packing": _unavailable(
            "Nothing records packed quantities.",
            "A packing entry screen, a weighing scale, or a count off the "
            "packing station."),
    }


#: The time buckets a series can be drawn in. The board used to offer one
#: shape only -- 24 hours of one day, and a month of totals with no shape at
#: all -- so "how did last week go?" had no answer on the screen that answers
#: "how did today go?".
BUCKETS = ("hour", "day", "month")
#: Units that ARE kilograms, written out because InventoryItem.unit is free
#: text a person typed. Anything else is a count of something (bags, pieces,
#: drums) and must not be added to a weight.
KG_UNITS = frozenset({"kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"})


def auto_bucket(start: datetime, end: datetime) -> str:
    """The bucket a window is naturally read in.

    Chosen by span, not by what the caller asked for, so a year never arrives
    as 8,760 hourly bars nobody can read and a single day never collapses to
    one. The boundaries are generous: two days still reads hourly, a quarter
    still reads daily.
    """
    span = end - start
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=92):
        return "day"
    return "month"


def _floor(when: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return when.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return datetime.combine(when.date(), time.min)
    return datetime(when.year, when.month, 1)


def _next(when: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return when + timedelta(hours=1)
    if bucket == "day":
        return when + timedelta(days=1)
    return datetime(when.year + (when.month == 12), (when.month % 12) + 1, 1)


def _edges(start: datetime, end: datetime, bucket: str) -> list:
    """Every bucket in the window, including the ones with nothing in them.

    An empty bucket is a REAL zero for a count: the window was watched and
    nothing was made in it. That is not true of the conversions, which is why
    kg and revenue go to None for the whole series when nothing in the window
    has a spec -- see `period`.
    """
    out, cursor = [], _floor(start, bucket)
    while cursor < end and len(out) < 1500:    # bounded: a decade of days
        out.append(cursor)
        cursor = _next(cursor, bucket)
    return out


def rm_added(db, tenant, start: datetime, end: datetime) -> list:
    """Material RECEIVED in the window, from goods receipts that were taken in.

    WHY THIS IS NOT JUST ADDED TO CONSUMPTION. Consumption is always kilograms
    because it is DERIVED: parts x the spec's part weight. A receipt is whatever
    the inventory item happens to be counted in -- `InventoryItem.unit` is free
    text somebody typed, and a moulder books resin in bags as often as in
    kilograms. Adding the two would put bags into a kilogram total and print a
    number that is not a weight. So every material is reported in ITS OWN unit,
    and `kg` is filled in only where that unit says kilograms.

    Draft receipts are excluded: material nobody has accepted yet is not on the
    floor. `accepted_qty` is the quantity taken in, which is already net of
    whatever was rejected at inspection.
    """
    rows = (db.query(models.GRNItem, models.InventoryItem)
              .join(models.GoodsReceiptNote, models.GRNItem.grn_id == models.GoodsReceiptNote.id)
              .join(models.InventoryItem, models.GRNItem.item_id == models.InventoryItem.id)
              .filter(models.GRNItem.tenant_code == tenant,
                      models.GoodsReceiptNote.tenant_code == tenant,
                      models.InventoryItem.tenant_code == tenant,
                      models.GoodsReceiptNote.status != "Draft",
                      models.GoodsReceiptNote.created_at >= start,
                      models.GoodsReceiptNote.created_at < end)
              .all())
    totals = defaultdict(float)
    for line, item in rows:
        totals[(item.item_name, (item.unit or "").strip())] += float(line.accepted_qty or 0)
    out = []
    for (name, unit), qty in sorted(totals.items(), key=lambda kv: -kv[1]):
        is_kg = unit.lower() in KG_UNITS
        out.append({"material": name, "quantity": round(qty, 3), "unit": unit or "(no unit)",
                    "kg": round(qty, 3) if is_kg else None})
    return out


def _balance(consumed: list, added: list) -> list:
    """Added against consumed, per material, and honest about the join.

    The two come from different vocabularies: consumption is keyed by
    `PartSpec.material` (what the mould makes it from) and a receipt by
    `InventoryItem.item_name` (what the storekeeper books it as). They are
    matched on the name, case- and space-insensitively, and a material that
    appears on only one side still gets a row with the other side left None --
    never zero, because "nothing was received" and "received under a different
    name" are different facts and this join cannot tell them apart.
    """
    key = lambda s: " ".join((s or "").split()).lower()    # noqa: E731
    rows = {}
    for c in consumed:
        rows[key(c["material"])] = {"material": c["material"], "consumed_kg": c["kg"],
                                    "added_kg": None, "added_qty": None, "added_unit": None}
    for a in added:
        k = key(a["material"])
        row = rows.setdefault(k, {"material": a["material"], "consumed_kg": None,
                                  "added_kg": None, "added_qty": None, "added_unit": None})
        row["added_kg"] = a["kg"]
        row["added_qty"] = a["quantity"]
        row["added_unit"] = a["unit"]
    return sorted(rows.values(),
                  key=lambda r: -((r["consumed_kg"] or 0) + (r["added_kg"] or 0)))


def period(db, tenant, start: datetime, end: datetime, bucket: str = ""):
    """The board over ANY window, bucketed, plus the totals for that window.

    `day` answers one day in hours and `month` answered one month in totals;
    everything between -- a shift, a week, a quarter -- had no answer at all.
    This is the same arithmetic over an arbitrary [start, end) and one bucket
    size, so the screen can ask for the period a person actually means.

    THE SPEC IN FORCE IS STILL THE ONE FOR THE PERIOD READ, taken as of its last
    day, for the reason the module docstring gives: reading today's price onto
    last month's output restates a month already reported.
    """
    bucket = bucket if bucket in BUCKETS else auto_bucket(start, end)
    machines = {m.id: m for m in db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant).all()}
    records = _records(db, tenant, start, end)

    as_of = (end - timedelta(microseconds=1)).date()
    specs = {}
    for mid in machines:
        code, _ = part_on_machine(db, tenant, mid)
        specs[mid] = spec_for(db, tenant, code, as_of)

    by_part, by_material, by_machine = defaultdict(int), defaultdict(float), defaultdict(float)
    good_by_part = defaultdict(int)
    buckets = _edges(start, end, bucket)
    index = {b: i for i, b in enumerate(buckets)}
    series = [{"start": b.isoformat(), "parts": 0, "good": 0, "unassigned": 0,
               "kg": 0.0, "revenue": 0.0} for b in buckets]
    any_spec = any_price = False

    for r in records:
        spec = specs.get(r.machine_id)
        parts = int(r.total_count or 0)
        good = int(r.good_count or 0)
        slot = series[index[_floor(r.created_at, bucket)]] if r.created_at and \
            _floor(r.created_at, bucket) in index else None
        if slot is not None:
            slot["parts"] += parts
            slot["good"] += good
        if spec:
            any_spec = True
            by_part[spec.part_name] += parts
            good_by_part[spec.part_name] += good
            by_material[spec.material] += parts * float(spec.part_weight_g) / 1000.0
            price = float(spec.price_per_piece or 0)
            any_price = any_price or price > 0
            by_machine[r.machine_id] += parts * price
            if slot is not None:
                slot["kg"] += parts * float(spec.part_weight_g) / 1000.0
                slot["revenue"] += parts * price
        else:
            by_part["(no part assigned)"] += parts
            good_by_part["(no part assigned)"] += good
            if slot is not None:
                slot["unassigned"] += parts

    for s in series:
        # Nothing in the window has a spec (or a price), so there is no weight
        # (or value) to report -- and a flat line at zero would say there was.
        s["kg"] = round(s["kg"], 3) if any_spec else None
        s["revenue"] = round(s["revenue"], 2) if any_price else None

    hours = (end - start).total_seconds() / 3600.0
    machine_rates = [{"machine": machines[mid].name,
                      "revenue": round(rev, 2),
                      "rate_per_hour": round(rev / hours, 2) if hours else 0.0}
                     for mid, rev in sorted(by_machine.items(), key=lambda kv: -kv[1])]
    consumption = [{"material": mat, "kg": round(kg, 3)}
                   for mat, kg in sorted(by_material.items(), key=lambda kv: -kv[1])]
    received = rm_added(db, tenant, start, end)
    return {
        "from": start.isoformat(), "to": end.isoformat(), "bucket": bucket,
        "hours": round(hours, 2),
        "series": series,
        "itemwise_production": [{"part": p, "total": t, "good": good_by_part[p]}
                                for p, t in sorted(by_part.items(), key=lambda kv: -kv[1])],
        "rm_consumption": consumption,
        "rm_added": received,
        "rm_balance": _balance(consumption, received),
        "shift_rate_by_machine": machine_rates,
        "total_rate_per_hour": round(sum(r["rate_per_hour"] for r in machine_rates), 2),
        "total_revenue": round(sum(r["revenue"] for r in machine_rates), 2),
        "power": _unavailable("No energy meter is installed on any machine.",
                              "Fit a meter, or allocate an incomer total by runtime."),
        "packing": _unavailable("Nothing records packed quantities.",
                                "A packing entry screen or a weighing scale."),
    }


def month(db, tenant, year: int, mon: int):
    """The monthly tables: one calendar month of `period`, bucketed by day.

    Kept as its own entry point because the month is what the board opens on
    and what /analytics/plant-board/month has always returned; `year` and
    `month` stay in the payload so nothing that reads it has to change.
    """
    start = datetime(year, mon, 1)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1)
    out = period(db, tenant, start, end, "day")
    out["year"], out["month"] = year, mon
    return out
