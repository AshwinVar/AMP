"""Executive scorecard — the plant on one line (ADR-0007).

One headline KPI per pillar — OEE, good rate, on-time orders, and the cost of
losses — each with a tone (good / warn / bad) so the exec home can lead with the
numbers that matter, not a wall of cards. Composes the pillar read-models only;
auto-scoped to the tenant (ADR-0002); it adds no storage.
"""
from ai.oee import build_oee_summary
from ai.production import build_production_summary
from ai.delivery import build_delivery_summary
from ai.cost import build_cost_summary

name = "scorecard"


def _tone(value, good, warn, higher_is_better=True) -> str:
    """A KPI's health band. Higher-is-better metrics are good above `good`, warn
    above `warn`, else bad; the flag flips the comparison for cost-like metrics."""
    if value is None:
        return "none"
    if higher_is_better:
        return "good" if value >= good else "warn" if value >= warn else "bad"
    return "good" if value <= good else "warn" if value <= warn else "bad"


def build_scorecard(db, tenant: str) -> dict:
    """Four headline KPIs — OEE, good rate, on-time orders, cost of losses — each
    with a tone, composed from the pillar read-models (ADR-0007)."""
    oee = build_oee_summary(db, tenant)["plant"]
    prod = build_production_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)
    cost = build_cost_summary(db, tenant)

    on_time = (round((delivery["total"] - delivery["late"]) / delivery["total"] * 100)
               if delivery["total"] else None)
    kpis = [
        {"key": "oee", "label": "Plant OEE", "value": oee["oee"], "unit": "%",
         "tone": _tone(oee["oee"], 85, 70)},
        {"key": "good_rate", "label": "Good rate", "value": prod["good_rate"], "unit": "%",
         "tone": _tone(prod["good_rate"], 98, 95)},
        {"key": "on_time", "label": "On-time orders", "value": on_time, "unit": "%",
         "tone": _tone(on_time, 95, 85)},
        {"key": "loss_cost", "label": "Cost of losses", "value": cost["loss_cost"], "unit": "$",
         "tone": ("good" if cost["loss_cost"] == 0 else "warn")},
    ]
    return {
        "has_data": oee["has_data"] or prod["runs"] > 0 or delivery["total"] > 0,
        "kpis": kpis,
    }
