"""Cost of losses — what the plant's losses are costing, in money (ADR-0007).

Puts a number on the week's losses: unplanned downtime minutes and rejected
units, priced at standard rates, plus any costs actually logged against the
period rolled up by type. Ties the OEE story to the P&L. A read-model over
production_records + cost_records — auto-scoped to the tenant (ADR-0002); it
adds no storage. The rates are conservative SME defaults; a tenant cost policy
can replace them later.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import models
from ai.twin import _recent_production
from currency import CURRENCY, money, signed_money

name = "cost"

WINDOW_DAYS = 7
TOP_N = 5
# Rates are in the platform currency (see currency.py — GBP per ADR-0010). These
# comments used to say "$" while the tenant's own rate column is unit_value_gbp,
# which is how the product ended up printing one plant's money two ways.
DOWNTIME_COST_PER_MIN = 12      # lost per minute of unplanned downtime
SCRAP_COST_PER_UNIT = 25        # lost per rejected / scrapped unit

# Trend window — two WINDOW_DAYS halves, so "this week vs last week" over the same
# 7-day window the cost summary already reports. Mirrors ai.downtime's trend.
TREND_HALVES = 2
TREND_WINDOW_DAYS = WINDOW_DAYS * TREND_HALVES
# A week-to-week swing in loss cost below this is ordinary noise, not a trend —
# and the floor a machine must clear to be named a mover. 300 = ~25 downtime min
# or 12 scrapped units, the same order as ai.downtime's 30-minute floor.
MIN_MOVE_COST = 300
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
    return sum(max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0)) for r in records)


def build_cost_summary(db, tenant: str) -> dict:
    """The cost of the week's losses — downtime and scrap priced at standard
    rates, biggest first — plus the costs actually recorded in the period rolled
    up by type. production_records and cost_records are auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS)
    downtime_min = downtime_minutes(records)
    rejected = sum(r.rejected_count or 0 for r in records)

    downtime_cost = downtime_min * DOWNTIME_COST_PER_MIN
    scrap_cost = rejected * SCRAP_COST_PER_UNIT
    loss_cost = downtime_cost + scrap_cost

    # Loss cost attributed to the line each record ran on (SMT / IC) and to the
    # individual machine (costliest first, for triage).
    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}
    line_agg: dict = {}
    machine_agg: dict = {}
    for r in records:
        dm = max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))
        rej = r.rejected_count or 0
        ln = line_of.get(r.machine_id, "")
        if ln:
            a = line_agg.setdefault(ln, {"downtime_min": 0, "rejected": 0})
            a["downtime_min"] += dm
            a["rejected"] += rej
        if r.machine_id is not None:
            b = machine_agg.setdefault(r.machine_id, {"downtime_min": 0, "rejected": 0})
            b["downtime_min"] += dm
            b["rejected"] += rej

    def _row(extra, a):
        return {**extra,
                "downtime_cost": a["downtime_min"] * DOWNTIME_COST_PER_MIN,
                "scrap_cost": a["rejected"] * SCRAP_COST_PER_UNIT,
                "cost": a["downtime_min"] * DOWNTIME_COST_PER_MIN + a["rejected"] * SCRAP_COST_PER_UNIT}

    by_line = [_row({"line": ln}, a) for ln, a in sorted(line_agg.items())]
    machine_rows = sorted(
        (_row({"machine_id": mid, "name": names.get(mid, f"#{mid}")}, a) for mid, a in machine_agg.items()),
        key=lambda m: m["cost"], reverse=True,
    )
    # by_machine is the costliest-first DISPLAY list, capped at TOP_N. machine_cost
    # is the FULL, uncapped {machine_id: cost} map over the same records — the twin
    # floor-map overlay needs every machine's real figure, not just the top few, or
    # a machine ranked outside the top-N paints as £0 on the map despite real losses.
    # Both come from the same `_row` cost formula, so they can never disagree (rule 1).
    by_machine = machine_rows[:TOP_N]
    machine_cost = {m["machine_id"]: m["cost"] for m in machine_rows}

    # Daily loss cost across the window (oldest -> newest), for the trend.
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    day_agg = {d: {"downtime_min": 0, "rejected": 0} for d in window}
    for r in records:
        d = r.created_at.date() if r.created_at else None
        if d in day_agg:
            day_agg[d]["downtime_min"] += max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))
            day_agg[d]["rejected"] += r.rejected_count or 0
    daily = [{"date": d.isoformat(),
              "cost": day_agg[d]["downtime_min"] * DOWNTIME_COST_PER_MIN + day_agg[d]["rejected"] * SCRAP_COST_PER_UNIT}
             for d in window]

    # Costs actually logged in the window, grouped by type (worst first).
    # Windowed in SQL — the table grows as costs are logged.
    window_start = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())
    recs = (db.query(models.CostRecord)
            .filter(models.CostRecord.created_at >= window_start).all())
    by_type_amt: Counter = Counter()
    for c in recs:
        by_type_amt[c.cost_type or "Other"] += c.amount or 0
    by_type = [{"type": t, "amount": a} for t, a in by_type_amt.most_common()]

    losses = [
        {"key": "downtime", "label": "Downtime", "cost": downtime_cost,
         "detail": f"{downtime_min:,} min at {money(DOWNTIME_COST_PER_MIN)}/min"},
        {"key": "scrap", "label": "Scrap", "cost": scrap_cost,
         "detail": f"{rejected:,} units at {money(SCRAP_COST_PER_UNIT)}/unit"},
    ]
    biggest = max(losses, key=lambda l: l["cost"]) if loss_cost > 0 else None

    return {
        "has_data": bool(records) or bool(recs),
        "days": WINDOW_DAYS,
        "loss_cost": loss_cost,
        "downtime_cost": downtime_cost,
        "scrap_cost": scrap_cost,
        "downtime_minutes": downtime_min,
        "rejected_units": rejected,
        "losses": losses,
        "biggest": biggest["key"] if biggest else None,
        "by_line": by_line,
        "by_machine": by_machine,
        "machine_cost": machine_cost,
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


def build_cost_trend(db, tenant: str) -> dict:
    """Which way is the cost of losses going, and who moved it? Compares the last
    7 days of loss cost (downtime + scrap priced at the standard rates) against the
    7 before — on the SAME per-record basis as build_cost_summary (downtime floored
    per record, never on the net) — and attributes the swing to machines and to the
    two drivers (downtime vs scrap). A read-model over production_records (+ machines
    for labels), auto-scoped to the tenant (ADR-0002); it adds no storage.

    Cost is the metric that moves here, the money twin of ai.downtime's minutes and
    ai.quality's scrap trend, so an owner sees losses creeping up while it is still a
    few hundred dollars rather than after a bad month. Every in-window record lands
    in exactly one day and one half, so the daily series sums back to the half totals
    (rule 3). A swing built on one or two loss-making jobs is reported but not judged."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=n) for n in range(TREND_WINDOW_DAYS - 1, -1, -1)]
    records = _recent_production(db, days=TREND_WINDOW_DAYS)
    names = {m.id: m.name for m in db.query(models.Machine).all()}

    def _blank():
        return {"cost": 0, "downtime_cost": 0, "scrap_cost": 0, "records": 0, "loss_records": 0}

    daily = {d: {"cost": 0, "downtime_cost": 0, "scrap_cost": 0} for d in window}
    halves = {"current": _blank(), "prior": _blank()}
    per_machine: dict = defaultdict(lambda: {"current": 0, "prior": 0})
    drivers = {"downtime": {"current": 0, "prior": 0}, "scrap": {"current": 0, "prior": 0}}

    for r in records:
        day = r.created_at.date() if r.created_at else None
        half = _half_of(day, today) if day else None
        if half is None:
            continue
        dm = max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))
        rej = r.rejected_count or 0
        dt_cost = dm * DOWNTIME_COST_PER_MIN
        sc_cost = rej * SCRAP_COST_PER_UNIT
        cost = dt_cost + sc_cost

        daily[day]["cost"] += cost
        daily[day]["downtime_cost"] += dt_cost
        daily[day]["scrap_cost"] += sc_cost

        h = halves[half]
        h["cost"] += cost
        h["downtime_cost"] += dt_cost
        h["scrap_cost"] += sc_cost
        h["records"] += 1
        if cost > 0:
            h["loss_records"] += 1

        if r.machine_id is not None:
            per_machine[r.machine_id][half] += cost
        drivers["downtime"][half] += dt_cost
        drivers["scrap"][half] += sc_cost

    series = [{"date": d.isoformat(), "cost": daily[d]["cost"],
               "downtime_cost": daily[d]["downtime_cost"], "scrap_cost": daily[d]["scrap_cost"]}
              for d in window]
    current, prior = halves["current"], halves["prior"]

    delta_cost = current["cost"] - prior["cost"]
    # Percentage move is only meaningful against a non-zero prior week; a jump from
    # zero cost is a real worsening but has no finite percentage.
    delta_pct = round(delta_cost / prior["cost"] * 100) if prior["cost"] else None

    total_loss_records = current["loss_records"] + prior["loss_records"]
    if current["cost"] == 0 and prior["cost"] == 0:
        direction = "none"
    elif delta_cost > MIN_MOVE_COST:
        direction = "worsening"
    elif delta_cost < -MIN_MOVE_COST:
        direction = "improving"
    else:
        direction = "steady"

    # A swing built on one or two loss-making jobs is arithmetic, not a trend.
    thin = 0 < total_loss_records < MIN_TREND_RECORDS

    machines = []
    for mid, a in per_machine.items():
        d_cost = a["current"] - a["prior"]
        machines.append({
            "machine_id": mid, "name": names.get(mid, f"#{mid}"),
            "cost": a["current"], "prior_cost": a["prior"], "delta_cost": d_cost,
        })
    worsening_machines = sorted((m for m in machines if m["delta_cost"] >= MIN_MOVE_COST),
                                key=lambda m: m["delta_cost"], reverse=True)[:TOP_N]
    improving_machines = sorted((m for m in machines if m["delta_cost"] <= -MIN_MOVE_COST),
                                key=lambda m: m["delta_cost"])[:TOP_N]

    driver_rows = sorted(
        ({"key": k, "label": k.capitalize(), "cost": v["current"],
          "prior_cost": v["prior"], "delta_cost": v["current"] - v["prior"]}
         for k, v in drivers.items() if v["current"] or v["prior"]),
        key=lambda d: d["delta_cost"], reverse=True,
    )

    now_cost = current["cost"]
    worst = worsening_machines[0] if worsening_machines else None
    pct_txt = f" ({abs(delta_pct)}%)" if delta_pct is not None else ""
    if direction == "none":
        verdict, tone = "No cost of losses in the last 14 days.", "good"
    elif thin:
        verdict, tone = (f"Cost of losses moved {signed_money(delta_cost)} to {money(now_cost)} this week, but on "
                         f"{total_loss_records} loss-making record{'s' if total_loss_records != 1 else ''} "
                         "in 14 days — too little to call a trend.", "warn")
    elif direction == "worsening":
        blame = f" — {worst['name']} drove it (+{money(worst['delta_cost'])})" if worst else ""
        verdict, tone = (f"Cost of losses up {money(delta_cost)}{pct_txt} to {money(now_cost)} "
                         f"week on week{blame}.", "bad")
    elif direction == "improving":
        verdict, tone = (f"Cost of losses down {money(abs(delta_cost))}{pct_txt} to {money(now_cost)} "
                         "week on week.", "good")
    else:
        verdict, tone = (f"Cost of losses steady at {money(now_cost)} ({signed_money(delta_cost)} week on week).", "good")

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "current": current,
        "prior": prior,
        "delta_cost": delta_cost,
        "delta_pct": delta_pct,
        "direction": direction,
        "thin_sample": thin,
        "move_threshold_cost": MIN_MOVE_COST,
        "downtime_cost_per_min": DOWNTIME_COST_PER_MIN,
        "scrap_cost_per_unit": SCRAP_COST_PER_UNIT,
        "series": series,
        "worsening_machines": worsening_machines,
        "improving_machines": improving_machines,
        "drivers": driver_rows,
        "verdict": verdict,
        "tone": tone,
    }
