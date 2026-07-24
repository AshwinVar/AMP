"""Executive scorecard — the plant on one line (ADR-0007).

One headline KPI per pillar — OEE, good rate, delivery reliability, and the cost
of losses — each with a tone (good / warn / bad) and a change vs the prior 7 days,
so the exec home leads with the numbers that matter and their direction, not a
wall of cards. Composes the pillar read-models only; auto-scoped to the tenant
(ADR-0002); it adds no storage.
"""
from datetime import datetime, timedelta

import models
from analytics_engine import oee_direction
from ai.oee import build_oee_summary
from ai.production import build_production_summary
from ai.delivery import build_delivery_summary
from ai.cost import build_cost_summary, downtime_minutes, DOWNTIME_COST_PER_MIN, SCRAP_COST_PER_UNIT
from ai.twin import _oee_from_records

name = "scorecard"

WINDOW_DAYS = 7


def _tone(value, good, warn, higher_is_better=True) -> str:
    """A KPI's health band. Higher-is-better metrics are good above `good`, warn
    above `warn`, else bad; the flag flips the comparison for cost-like metrics."""
    if value is None:
        return "none"
    if higher_is_better:
        return "good" if value >= good else "warn" if value >= warn else "bad"
    return "good" if value <= good else "warn" if value <= warn else "bad"


def _period_kpis(records) -> dict:
    """OEE, good rate and loss cost over a set of production records — the three
    windowed KPIs, computed the same way as the live cards so the current and
    prior periods compare like-for-like."""
    total = sum(r.total_count or 0 for r in records)
    good = sum(r.good_count or 0 for r in records)
    rejected = sum(r.rejected_count or 0 for r in records)
    # Same per-record downtime basis as the live cost card, so the current period
    # (from build_cost_summary) and this prior period compare like-for-like.
    downtime_min = downtime_minutes(records)
    return {
        "has": bool(records),
        "oee": _oee_from_records(records)["oee"],
        "good_rate": round(good / total * 100) if total else 0,
        "loss_cost": downtime_min * DOWNTIME_COST_PER_MIN + rejected * SCRAP_COST_PER_UNIT,
    }


def _prior_records(db):
    """Production records from the 7 days before the current window (7-13 days
    ago). Bounded in SQL — the table grows continuously."""
    today = datetime.utcnow().date()
    lo = datetime.combine(today - timedelta(days=2 * WINDOW_DAYS - 1), datetime.min.time())
    hi = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())
    return (db.query(models.ProductionRecord)
            .filter(models.ProductionRecord.created_at >= lo,
                    models.ProductionRecord.created_at < hi)
            .all())


def _delta(cur, prior, has_prior, lower_is_better=False):
    """Signed change vs the prior period and its tone (good/bad/flat), or
    (None, None) when there's no prior period to compare against."""
    if not has_prior or cur is None:
        return None, None
    d = cur - prior
    if d == 0:
        return 0, "flat"
    improved = (d < 0) if lower_is_better else (d > 0)
    return d, "good" if improved else "bad"


def build_scorecard(db, tenant: str) -> dict:
    """Four headline KPIs — OEE, good rate, delivery reliability, cost of losses —
    each with a tone and a change vs the prior 7 days, composed from the pillar
    read-models (ADR-0007). Delivery reliability is order-state based, so it has
    no weekly delta."""
    oee = build_oee_summary(db, tenant)["plant"]
    prod = build_production_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)
    cost = build_cost_summary(db, tenant)
    prior = _period_kpis(_prior_records(db))

    # Delivery reliability — of the orders that have come due (delivered or late),
    # the share actually delivered. Reuses the delivery read-model's own definition
    # so the scorecard, the delivery summary and the per-customer drill-down all
    # reconcile. This is NOT an on-time rate: customer_orders carries no dispatch
    # timestamp, so punctuality isn't computable, and not-yet-due (on-track /
    # at-risk) orders are held out of the denominator rather than counted as
    # successes. None when no order has come due yet -> the strip shows "—".
    reliability = delivery["reliability_rate"] if delivery["resolved"] else None
    # OEE week-over-week uses the SHARED direction (with its dead-band), so a small
    # move can't read "down"/red here while the recovery card's badge says "flat".
    if prior["has"] and oee["oee"] is not None:
        oee_d = oee["oee"] - prior["oee"]
        oee_dt = {"up": "good", "down": "bad", "flat": "flat"}[oee_direction(oee["oee"], prior["oee"])]
    else:
        oee_d, oee_dt = None, None
    good_d, good_dt = _delta(prod["good_rate"], prior["good_rate"], prior["has"])
    cost_d, cost_dt = _delta(cost["loss_cost"], prior["loss_cost"], prior["has"], lower_is_better=True)

    kpis = [
        {"key": "oee", "label": "Plant OEE", "value": oee["oee"], "unit": "%",
         "tone": _tone(oee["oee"], 85, 70), "delta": oee_d, "delta_tone": oee_dt},
        {"key": "good_rate", "label": "Good rate", "value": prod["good_rate"], "unit": "%",
         "tone": _tone(prod["good_rate"], 98, 95), "delta": good_d, "delta_tone": good_dt},
        # key stays "on_time" so the strip still drills into the orders view; the
        # displayed label and value are the honest delivery-reliability number.
        {"key": "on_time", "label": "Delivery reliability", "value": reliability, "unit": "%",
         "tone": _tone(reliability, 95, 85), "delta": None, "delta_tone": None},
        {"key": "loss_cost", "label": "Cost of losses", "value": cost["loss_cost"], "unit": "$",
         "tone": ("good" if cost["loss_cost"] == 0 else "warn"), "delta": cost_d, "delta_tone": cost_dt},
    ]
    return {
        "has_data": oee["has_data"] or prod["runs"] > 0 or delivery["total"] > 0,
        "kpis": kpis,
    }
