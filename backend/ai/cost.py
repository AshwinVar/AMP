"""Cost of losses — what the plant's losses are costing, in money (ADR-0007).

Puts a number on the week's losses: unplanned downtime minutes and rejected
units, priced at standard rates, plus any costs actually logged against the
period rolled up by type. Ties the OEE story to the P&L. A read-model over
production_records + cost_records — auto-scoped to the tenant (ADR-0002); it
adds no storage. The rates are conservative SME defaults; a tenant cost policy
can replace them later.
"""
from collections import Counter
from datetime import datetime, timedelta

import models
from ai.twin import _recent_production

name = "cost"

WINDOW_DAYS = 7
TOP_N = 5
DOWNTIME_COST_PER_MIN = 12      # $ lost per minute of unplanned downtime
SCRAP_COST_PER_UNIT = 25        # $ lost per rejected / scrapped unit


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
    by_machine = sorted(
        (_row({"machine_id": mid, "name": names.get(mid, f"#{mid}")}, a) for mid, a in machine_agg.items()),
        key=lambda m: m["cost"], reverse=True,
    )[:TOP_N]

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
         "detail": f"{downtime_min:,} min at ${DOWNTIME_COST_PER_MIN}/min"},
        {"key": "scrap", "label": "Scrap", "cost": scrap_cost,
         "detail": f"{rejected:,} units at ${SCRAP_COST_PER_UNIT}/unit"},
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
        "daily": daily,
        "recorded_total": sum(by_type_amt.values()),
        "by_type": by_type,
    }
