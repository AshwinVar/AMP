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
DOWNTIME_COST_PER_MIN = 12      # $ lost per minute of unplanned downtime
SCRAP_COST_PER_UNIT = 25        # $ lost per rejected / scrapped unit


def build_cost_summary(db, tenant: str) -> dict:
    """The cost of the week's losses — downtime and scrap priced at standard
    rates, biggest first — plus the costs actually recorded in the period rolled
    up by type. production_records and cost_records are auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS)
    planned = sum(r.planned_minutes or 0 for r in records)
    runtime = sum(r.runtime_minutes or 0 for r in records)
    downtime_min = max(0, planned - runtime)
    rejected = sum(r.rejected_count or 0 for r in records)

    downtime_cost = downtime_min * DOWNTIME_COST_PER_MIN
    scrap_cost = rejected * SCRAP_COST_PER_UNIT
    loss_cost = downtime_cost + scrap_cost

    # Same loss cost attributed to the production line each record ran on (SMT / IC).
    line_of = {m.id: (m.line or "") for m in db.query(models.Machine).all()}
    line_agg: dict = {}
    for r in records:
        ln = line_of.get(r.machine_id, "")
        if not ln:
            continue
        a = line_agg.setdefault(ln, {"downtime_min": 0, "rejected": 0})
        a["downtime_min"] += max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))
        a["rejected"] += r.rejected_count or 0
    by_line = [{
        "line": ln,
        "downtime_cost": a["downtime_min"] * DOWNTIME_COST_PER_MIN,
        "scrap_cost": a["rejected"] * SCRAP_COST_PER_UNIT,
        "cost": a["downtime_min"] * DOWNTIME_COST_PER_MIN + a["rejected"] * SCRAP_COST_PER_UNIT,
    } for ln, a in sorted(line_agg.items())]

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
    window_start = today - timedelta(days=WINDOW_DAYS - 1)
    recs = [c for c in db.query(models.CostRecord).all()
            if c.created_at and c.created_at.date() >= window_start]
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
        "daily": daily,
        "recorded_total": sum(by_type_amt.values()),
        "by_type": by_type,
    }
