"""Cost of losses — what the plant's losses are costing (ADR-0007, ADR-0010).

Puts a number on the week's losses: unplanned downtime and rejected units, plus
any costs actually logged against the period rolled up by type. Ties the OEE
story to the P&L. A read-model over production_records + cost_records —
auto-scoped to the tenant (ADR-0002); it adds no storage.

THE NUMBER IS GOOD UNITS NOT MADE, AND £ ONLY WITH THE TENANT'S OWN RATE.
This module used to price every tenant's downtime at a fixed £12 a minute and
every scrapped unit at a fixed £25, so a plant with a £2 margin and one with a
£400 margin were shown the same money, none of it from the customer. ADR-0010's
decision is one per-tenant margin per good unit, and "unset means units-only,
never a fabricated £". Losses are now measured in good units (loss_value.py:
scrap is one unit each; downtime is converted at the window's observed run rate)
and priced only by tenancy.tenant_unit_value. With no rate every £ field is None.
See test_loss_money_needs_the_tenant_rate.py.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import loss_value
import models
import tenancy
from ai.downtime import MIN_MOVE_MINUTES as DOWNTIME_MOVE_MINUTES
from ai.twin import _recent_production
from currency import money, unit_rate

name = "cost"

WINDOW_DAYS = 7
TOP_N = 5

# Trend window — two WINDOW_DAYS halves, so "this week vs last week" over the same
# 7-day window the cost summary already reports. Mirrors ai.downtime's trend.
TREND_HALVES = 2
TREND_WINDOW_DAYS = WINDOW_DAYS * TREND_HALVES
# A week-to-week swing smaller than what DOWNTIME_MOVE_MINUTES of downtime loses at
# the plant's own run rate is noise, not a trend, and it is the floor a machine must
# clear to be named a mover. It is the downtime trend's floor (ai.downtime) in units,
# so the two trend cards agree on what counts as a move. The old floor was £300 of
# the fixed tariff, which meant nothing once the tariff was gone.
# The move is driven by fewer loss-making production records than this across BOTH
# halves -> a single bad job swings the whole number; report it but don't judge it.
MIN_TREND_RECORDS = 4


def downtime_minutes(records) -> int:
    """Unplanned downtime over a set of production records: each record's own
    shortfall (planned - runtime), floored at 0 PER RECORD, then summed.

    This is the ONE basis the headline, the per-line / per-machine / daily
    breakdowns AND the scorecard's prior-period comparison all share. Flooring
    per record (not once on the aggregate net) is the honest choice: a job that
    runs OVER its planned minutes must not silently cancel a real stoppage on
    another job — max(0, sum(planned) - sum(runtime)) would let it, so the
    headline could read less than the drill-down it sits above (they must
    reconcile, and this matches OEE's per-record availability cap of 100%)."""
    return sum(_down(r) for r in records)


def _down(r) -> int:
    return max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))


def loss_totals(records, unit_value) -> dict:
    """The loss over a set of records, in good units and (with a rate) in £.

    The run rate is pooled over these same records, so each window is valued at
    what the line produced in that window. The scorecard's prior period calls this
    too, so "vs last week" is computed exactly like the headline."""
    down = downtime_minutes(records)
    rejected = sum(r.rejected_count or 0 for r in records)
    rate = loss_value.run_rate(sum(r.good_count or 0 for r in records),
                               sum(r.runtime_minutes or 0 for r in records))
    down_units = loss_value.whole(loss_value.downtime_units(down, rate))
    downtime_cost = loss_value.value(down_units, unit_value)
    scrap_cost = loss_value.value(rejected, unit_value)
    return {
        "downtime_minutes": down,
        "rejected_units": rejected,
        "run_rate": rate,
        "downtime_lost_units": down_units,
        "lost_units": None if down_units is None else down_units + rejected,
        "downtime_cost": downtime_cost,
        "scrap_cost": scrap_cost,
        "loss_cost": None if downtime_cost is None or scrap_cost is None else downtime_cost + scrap_cost,
    }


def _breakdown(groups, totals, unit_value) -> dict:
    """Split `totals` across groups so every group's units and £ sum to the headline.

    `groups` maps a key to that group's {"down", "rejected"} and must cover every
    record (callers keep a hidden bucket for records with no machine / line). Each
    column is apportioned from the same floats the headline was rounded from."""
    rate = totals["run_rate"]
    down_units = totals["downtime_lost_units"]
    if down_units is None:
        units = {k: None for k in groups}
    else:
        units = loss_value.apportion(down_units, {k: g["down"] * rate if rate else 0.0
                                                  for k, g in groups.items()})
    if unit_value is None:
        down_cost = {k: None for k in groups}
        scrap_cost = {k: None for k in groups}
    else:
        scrap_cost = loss_value.apportion(totals["scrap_cost"],
                                          {k: g["rejected"] * unit_value for k, g in groups.items()})
        down_cost = ({k: None for k in groups} if down_units is None else
                     loss_value.apportion(totals["downtime_cost"],
                                          {k: units[k] * unit_value for k in groups}))
    rows = {}
    for k, g in groups.items():
        dc, sc = down_cost[k], scrap_cost[k]
        rows[k] = {
            "downtime_minutes": g["down"],
            "rejected_units": g["rejected"],
            "downtime_lost_units": units[k],
            "lost_units": None if units[k] is None else units[k] + g["rejected"],
            "downtime_cost": dc,
            "scrap_cost": sc,
            "cost": None if dc is None or sc is None else dc + sc,
        }
    return rows


def _group(records, key):
    groups: dict = {}
    for r in records:
        g = groups.setdefault(key(r), {"down": 0, "rejected": 0})
        g["down"] += _down(r)
        g["rejected"] += r.rejected_count or 0
    return groups


def _loss_order(row):
    """Biggest loss first: lost units when known, else downtime minutes then scrap."""
    units = row["lost_units"]
    return (units is not None, units or 0, row["downtime_minutes"], row["rejected_units"])


def build_cost_summary(db, tenant: str, now=None) -> dict:
    """The week's losses in good units, and in £ when the tenant has set its rate,
    biggest first, plus the costs actually recorded in the period rolled up by
    type. production_records and cost_records are auto-scoped (ADR-0002)."""
    # ONE anchor for the request: the headline, the daily bars and the recorded
    # costs beside them must all be the same seven days. They were three
    # different bases under one "days": 7 label (#587).
    import oee_contract
    window = oee_contract.OeeWindow(WINDOW_DAYS, now=now)
    records = _recent_production(db, days=WINDOW_DAYS, now=window.end)
    unit_value = tenancy.tenant_unit_value(db, tenant)
    totals = loss_totals(records, unit_value)

    # Loss attributed to the line each record ran on (SMT / IC) and to the
    # individual machine (biggest first, for triage). Records with no line or no
    # machine stay in a hidden bucket so the published rows still sum to the headline.
    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}
    by_line_rows = _breakdown(_group(records, lambda r: line_of.get(r.machine_id, "")), totals, unit_value)
    by_line = [{"line": ln, **row} for ln, row in sorted(by_line_rows.items()) if ln]
    machine_rows = _breakdown(_group(records, lambda r: r.machine_id), totals, unit_value)
    ranked = sorted(({"machine_id": mid, "name": names.get(mid, f"#{mid}"), **row}
                     for mid, row in machine_rows.items() if mid is not None),
                    key=_loss_order, reverse=True)
    # by_machine is the biggest-first DISPLAY list, capped at TOP_N. machine_cost /
    # machine_lost_units are the FULL, uncapped maps over the same records — the twin
    # floor-map overlay needs every machine's real figure, not just the top few, or
    # a machine ranked outside the top-N paints as nothing on the map despite real
    # losses. Both come from the same breakdown, so they can never disagree (rule 1).
    by_machine = ranked[:TOP_N]
    machine_cost = {m["machine_id"]: m["cost"] for m in ranked}
    machine_lost_units = {m["machine_id"]: m["lost_units"] for m in ranked}

    # Daily loss across the window (oldest -> newest), for the trend.
    #
    # The buckets are derived from the WINDOW, not from a day count. A rolling
    # 7x24h window that opens mid-day touches EIGHT calendar dates, and this
    # series used to draw only [today-6 ... today] -- so a costly run on the
    # partial eighth date was in the headline and appeared in no bar.
    # Measured: headline 30,760 with every bar at 0, a chart summing to ZERO
    # under a five-figure figure. Same shape as the OEE trend (#586).
    start_date = window.start.date()
    end_date = (window.end - timedelta(microseconds=1)).date()
    span = [start_date + timedelta(days=i)
            for i in range((end_date - start_date).days + 1)]
    opens_mid_day = window.start.time() != datetime.min.time()
    day_groups = {d: {"down": 0, "rejected": 0} for d in span}
    for key, g in _group(records, lambda r: r.created_at.date() if r.created_at else None).items():
        day_groups.setdefault(key, {"down": 0, "rejected": 0})
        day_groups[key]["down"] += g["down"]
        day_groups[key]["rejected"] += g["rejected"]
    day_rows = _breakdown(day_groups, totals, unit_value)
    daily = [{"date": d.isoformat(), "cost": day_rows[d]["cost"], "lost_units": day_rows[d]["lost_units"],
              **({"partial": True} if (i == 0 and opens_mid_day) else {})}
             for i, d in enumerate(span)]

    # Costs actually logged in the window, grouped by type (worst first). These
    # are the tenant's own recorded amounts, so they are money whatever the rate.
    # Windowed in SQL -- the table grows as costs are logged.
    #
    # BOUNDED AT BOTH ENDS, against the same window as everything above. This
    # used to be `>= midnight(today-6)` with NO UPPER BOUND, which put money not
    # yet spent inside a 7-day card: a cost record dated three days in the future
    # was published as part of this week's costs, on a third basis from the two
    # figures beside it.
    recs = (db.query(models.CostRecord)
            .filter(models.CostRecord.created_at >= window.start,
                    models.CostRecord.created_at < window.end).all())
    by_type_amt: Counter = Counter()
    for c in recs:
        by_type_amt[c.cost_type or "Other"] += c.amount or 0
    by_type = [{"type": t, "amount": a} for t, a in by_type_amt.most_common()]

    down, rejected = totals["downtime_minutes"], totals["rejected_units"]
    down_units = totals["downtime_lost_units"]
    if down_units is None:
        down_detail = f"{down:,} min down with no run time to convert into units"
    elif unit_value is None:
        down_detail = f"{down:,} min ≈ {down_units:,} good units not made"
    else:
        down_detail = f"{down:,} min ≈ {down_units:,} good units at {unit_rate(unit_value)}/unit"
    scrap_detail = (f"{rejected:,} units scrapped" if unit_value is None
                    else f"{rejected:,} units at {unit_rate(unit_value)}/unit")
    losses = [
        {"key": "downtime", "label": "Downtime", "units": down_units,
         "cost": totals["downtime_cost"], "detail": down_detail},
        {"key": "scrap", "label": "Scrap", "units": rejected,
         "cost": totals["scrap_cost"], "detail": scrap_detail},
    ]
    # Biggest by units: the rate is one number, so the £ order is the unit order.
    # Unknown when downtime could not be converted and there is any scrap to compare.
    if down_units is None:
        biggest = "downtime" if rejected == 0 and down > 0 else None
    elif down_units + rejected == 0:
        biggest = None
    else:
        biggest = "downtime" if down_units >= rejected else "scrap"

    return {
        "has_data": bool(records) or bool(recs),
        "days": WINDOW_DAYS,
        "unit_value_gbp": unit_value,
        "priced": unit_value is not None,
        "run_rate": None if totals["run_rate"] is None else round(totals["run_rate"], 4),
        "loss_cost": totals["loss_cost"],
        "downtime_cost": totals["downtime_cost"],
        "scrap_cost": totals["scrap_cost"],
        "downtime_minutes": down,
        "rejected_units": rejected,
        "downtime_lost_units": down_units,
        "lost_units": totals["lost_units"],
        "losses": losses,
        "biggest": biggest,
        "by_line": by_line,
        "by_machine": by_machine,
        "machine_cost": machine_cost,
        "machine_lost_units": machine_lost_units,
        "daily": daily,
        "recorded_total": sum(by_type_amt.values()),
        "by_type": by_type,
    }


def _half_of(day, today):
    """Which half a day falls in: 'current' = the last WINDOW_DAYS including today,
    'prior' = the WINDOW_DAYS before that, None = outside the window. Mirrors
    ai.downtime._half_of so the trend cards split their windows the same way."""
    age = (today - day).days
    if 0 <= age < WINDOW_DAYS:
        return "current"
    if WINDOW_DAYS <= age < TREND_WINDOW_DAYS:
        return "prior"
    return None


def _amount(units, cost, priced):
    """A loss as the card reads it: £ with a rate, good units without."""
    if priced:
        return money(cost)
    return f"{units:,} good unit{'s' if units != 1 else ''}"


def build_cost_trend(db, tenant: str) -> dict:
    """Which way are the losses going, and who moved it? Compares the last 7 days
    of lost good units (downtime + scrap, on the SAME per-record basis as
    build_cost_summary, each week at its own run rate) against the 7 before, and
    attributes the swing to machines and to the two drivers. £ figures ride along
    when the tenant has set its rate. A read-model over production_records (+
    machines for labels), auto-scoped to the tenant (ADR-0002); no storage.

    Every in-window record lands in exactly one day and one half, and each half's
    days and machines are apportioned from that half's totals, so the daily series
    sums back to the half totals (rule 3). A swing built on one or two loss-making
    jobs is reported but not judged."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=n) for n in range(TREND_WINDOW_DAYS - 1, -1, -1)]
    records = _recent_production(db, days=TREND_WINDOW_DAYS)
    names = {m.id: m.name for m in db.query(models.Machine).all()}
    unit_value = tenancy.tenant_unit_value(db, tenant)
    priced = unit_value is not None

    in_half = {"current": [], "prior": []}
    for r in records:
        half = _half_of(r.created_at.date(), today) if r.created_at else None
        if half is not None:
            in_half[half].append(r)

    halves, day_rows, machine_rows = {}, {}, {}
    for half, recs in in_half.items():
        totals = loss_totals(recs, unit_value)
        halves[half] = {
            "lost_units": totals["lost_units"],
            "downtime_lost_units": totals["downtime_lost_units"],
            "scrap_units": totals["rejected_units"],
            "downtime_minutes": totals["downtime_minutes"],
            "cost": totals["loss_cost"],
            "downtime_cost": totals["downtime_cost"],
            "scrap_cost": totals["scrap_cost"],
            "records": len(recs),
            "loss_records": sum(1 for r in recs if _down(r) or (r.rejected_count or 0)),
        }
        day_rows.update(_breakdown(_group(recs, lambda r: r.created_at.date()), totals, unit_value))
        machine_rows[half] = _breakdown(_group(recs, lambda r: r.machine_id), totals, unit_value)

    blank = {"lost_units": 0, "downtime_lost_units": 0, "rejected_units": 0,
             "cost": 0 if priced else None, "downtime_cost": 0 if priced else None,
             "scrap_cost": 0 if priced else None}
    series = []
    for d in window:
        row = day_rows.get(d, blank)
        series.append({"date": d.isoformat(), "lost_units": row["lost_units"],
                       "downtime_lost_units": row["downtime_lost_units"],
                       "scrap_units": row["rejected_units"], "cost": row["cost"],
                       "downtime_cost": row["downtime_cost"], "scrap_cost": row["scrap_cost"]})
    current, prior = halves["current"], halves["prior"]

    known = current["lost_units"] is not None and prior["lost_units"] is not None
    delta_units = current["lost_units"] - prior["lost_units"] if known else None
    delta_cost = current["cost"] - prior["cost"] if known and priced else None
    shown_now, shown_prior = (current["cost"], prior["cost"]) if priced else (current["lost_units"], prior["lost_units"])
    shown_delta = delta_cost if priced else delta_units
    # Percentage move is only meaningful against a non-zero prior week; a jump from
    # zero is a real worsening but has no finite percentage.
    delta_pct = round(shown_delta / shown_prior * 100) if known and shown_prior else None

    # The noise floor, in units at the fortnight's pooled run rate (see the constant).
    pooled_rate = loss_value.run_rate(sum(r.good_count or 0 for r in records),
                                      sum(r.runtime_minutes or 0 for r in records))
    floor = max(1, loss_value.whole(DOWNTIME_MOVE_MINUTES * pooled_rate)) if pooled_rate else 1

    total_loss_records = current["loss_records"] + prior["loss_records"]
    if not known:
        direction = "unknown"
    elif current["lost_units"] == 0 and prior["lost_units"] == 0:
        direction = "none"
    elif delta_units >= floor:
        direction = "worsening"
    elif delta_units <= -floor:
        direction = "improving"
    else:
        direction = "steady"

    # A swing built on one or two loss-making jobs is arithmetic, not a trend.
    thin = 0 < total_loss_records < MIN_TREND_RECORDS

    machines = []
    for mid in set(machine_rows["current"]) | set(machine_rows["prior"]):
        if mid is None:
            continue
        cur = machine_rows["current"].get(mid, blank)
        pri = machine_rows["prior"].get(mid, blank)
        if cur["lost_units"] is None or pri["lost_units"] is None:
            continue
        machines.append({
            "machine_id": mid, "name": names.get(mid, f"#{mid}"),
            "lost_units": cur["lost_units"], "prior_lost_units": pri["lost_units"],
            "delta_units": cur["lost_units"] - pri["lost_units"],
            "cost": cur["cost"], "prior_cost": pri["cost"],
            "delta_cost": cur["cost"] - pri["cost"] if priced else None,
        })
    worsening_machines = sorted((m for m in machines if m["delta_units"] >= floor),
                                key=lambda m: m["delta_units"], reverse=True)[:TOP_N]
    improving_machines = sorted((m for m in machines if m["delta_units"] <= -floor),
                                key=lambda m: m["delta_units"])[:TOP_N]

    drivers = []
    if known:
        for key, units_key, cost_key in (("downtime", "downtime_lost_units", "downtime_cost"),
                                         ("scrap", "scrap_units", "scrap_cost")):
            if current[units_key] or prior[units_key]:
                drivers.append({
                    "key": key, "label": key.capitalize(),
                    "lost_units": current[units_key], "prior_lost_units": prior[units_key],
                    "delta_units": current[units_key] - prior[units_key],
                    "cost": current[cost_key], "prior_cost": prior[cost_key],
                    "delta_cost": current[cost_key] - prior[cost_key] if priced else None,
                })
        drivers.sort(key=lambda d: d["delta_units"], reverse=True)

    worst = worsening_machines[0] if worsening_machines else None
    pct_txt = f" ({abs(delta_pct)}%)" if delta_pct is not None else ""
    if direction == "unknown":
        verdict, tone = ("Downtime with no run time in one of the two weeks, so its lost units "
                         "cannot be counted and the weeks cannot be compared.", "warn")
    elif direction == "none":
        verdict, tone = "No losses in the last 14 days.", "good"
    else:
        now_txt = _amount(shown_now, shown_now, priced)
        move_txt = _amount(abs(shown_delta), abs(shown_delta), priced)
        if thin:
            sign = "+" if shown_delta >= 0 else "-"
            verdict, tone = (f"Losses moved {sign}{move_txt} to {now_txt} this week, but on "
                             f"{total_loss_records} loss-making record{'s' if total_loss_records != 1 else ''} "
                             "in 14 days — too little to call a trend.", "warn")
        elif direction == "worsening":
            blame = ""
            if worst:
                w = worst["delta_cost"] if priced else worst["delta_units"]
                blame = f" — {worst['name']} drove it (+{_amount(w, w, priced)})"
            verdict, tone = f"Losses up {move_txt}{pct_txt} to {now_txt} week on week{blame}.", "bad"
        elif direction == "improving":
            verdict, tone = f"Losses down {move_txt}{pct_txt} to {now_txt} week on week.", "good"
        else:
            verdict, tone = f"Losses steady at {now_txt} (a move of {move_txt} week on week).", "good"

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "unit_value_gbp": unit_value,
        "priced": priced,
        "current": current,
        "prior": prior,
        "delta_units": delta_units,
        "delta_cost": delta_cost,
        "delta_pct": delta_pct,
        "direction": direction,
        "thin_sample": thin,
        "move_threshold_units": floor,
        "series": series,
        "worsening_machines": worsening_machines,
        "improving_machines": improving_machines,
        "drivers": drivers,
        "verdict": verdict,
        "tone": tone,
    }
