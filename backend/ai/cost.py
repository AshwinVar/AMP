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

    # Costs actually logged in the window, grouped by type (worst first).
    window_start = datetime.utcnow().date() - timedelta(days=WINDOW_DAYS - 1)
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
        "recorded_total": sum(by_type_amt.values()),
        "by_type": by_type,
    }
