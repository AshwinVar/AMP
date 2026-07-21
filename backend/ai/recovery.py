"""OEE recovery opportunity — the gap to world-class, in recoverable output.

`losses` attributes the OEE gap to availability / performance / quality in points
and physical units. This read-model takes the next step a plant manager actually
asks: how much MORE good output would we make if we closed the gap to the
world-class benchmark (85% OEE)? It reports the point gap, the recoverable good
units over the window and annualised, and the per-factor gap to each component's
world-class target so the biggest lever is obvious. A read-model over
production_records — auto-scoped to the tenant (ADR-0002), no storage; reuses the
shared pooled OEE so it agrees with every other surface.
"""
import models
from ai.twin import _recent_production
from analytics_engine import pooled_oee

name = "recovery"

WINDOW_DAYS = 7
WORLD_CLASS_OEE = 85
# Classic world-class component benchmarks: 0.90 x 0.95 x ~0.99 ~= 0.85 OEE.
WORLD_CLASS_COMPONENTS = {"availability": 90, "performance": 95, "quality": 99}

_EMPTY = {
    "has_data": False, "oee": 0, "world_class": WORLD_CLASS_OEE, "gap_points": 0,
    "at_world_class": False, "good_units_window": 0, "window_days": WINDOW_DAYS,
    "recoverable_units_window": 0, "recoverable_units_per_year": 0,
    "unit_value_gbp": None, "recoverable_value_window": None,
    "recoverable_value_per_year": None, "components": [], "biggest_lever": None,
}


def _unit_value(db, tenant: str):
    """The tenant's configured £ per good unit (TenantConfig.unit_value_gbp), or
    None if unset — in which case recovery reports units only, never a made-up £."""
    c = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == tenant).first()
    return c.unit_value_gbp if c else None


def build_recovery_summary(db, tenant: str) -> dict:
    """The recovery opportunity over the last 7 days: gap to world-class OEE and
    what closing it is worth in good units. production_records is auto-scoped."""
    records = _recent_production(db, days=WINDOW_DAYS)
    o = pooled_oee(records)
    if not o["has_data"] or o["oee"] <= 0:
        return dict(_EMPTY)

    good = sum(r.good_count or 0 for r in records)
    at_wc = o["oee"] >= WORLD_CLASS_OEE

    # First-order: with the same run time, good output scales with OEE. Extra good
    # units at world-class = current good x (target / current - 1).
    recoverable_window = 0 if at_wc else round(good * (WORLD_CLASS_OEE / o["oee"] - 1))
    recoverable_year = round(recoverable_window * 365 / WINDOW_DAYS)

    components = []
    for key, target in WORLD_CLASS_COMPONENTS.items():
        current = o[key]
        components.append({
            "key": key, "label": key.capitalize(),
            "current": current, "target": target,
            "gap_points": max(0, target - current),
        })
    biggest = max(components, key=lambda c: c["gap_points"])

    # Value the recoverable output in £ only when the tenant has set a per-unit
    # rate; otherwise leave the £ fields null and report units only.
    rate = _unit_value(db, tenant)
    value_window = round(recoverable_window * rate) if rate else None
    value_year = round(recoverable_year * rate) if rate else None

    return {
        "has_data": True,
        "oee": o["oee"],
        "world_class": WORLD_CLASS_OEE,
        "gap_points": max(0, WORLD_CLASS_OEE - o["oee"]),
        "at_world_class": at_wc,
        "good_units_window": good,
        "window_days": WINDOW_DAYS,
        "recoverable_units_window": recoverable_window,
        "recoverable_units_per_year": recoverable_year,
        "unit_value_gbp": rate,
        "recoverable_value_window": value_window,
        "recoverable_value_per_year": value_year,
        "components": components,
        "biggest_lever": biggest["key"] if biggest["gap_points"] > 0 else None,
    }
