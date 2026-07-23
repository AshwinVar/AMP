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
from datetime import datetime, timedelta

import models
from ai.twin import _recent_production
# World-class benchmarks + the shared "component to focus on" definition live in
# analytics_engine (ADR-0010), so recovery's "biggest lever" and the OEE summary's
# "biggest drag" are literally the same rule.
from analytics_engine import (
    pooled_oee, biggest_lever, oee_direction, WORLD_CLASS_OEE, WORLD_CLASS_COMPONENTS,
)
# The single per-tenant £ rate lives in tenancy (shared with the management
# summary); aliased as _unit_value so the recovery tests can stub it.
from tenancy import tenant_unit_value as _unit_value

name = "recovery"

WINDOW_DAYS = 7

# The one concrete move that closes each lever's gap — what the owner actually does.
LEVER_ACTIONS = {
    "availability": "Cut unplanned downtime — attack the machine losing the most runtime.",
    "performance": "Close the speed loss — bring cycle time back to the ideal rate.",
    "quality": "Reduce scrap and rework — fix the top recurring defect.",
}

_EMPTY = {
    "has_data": False, "oee": 0, "world_class": WORLD_CLASS_OEE, "gap_points": 0,
    "at_world_class": False, "good_units_window": 0, "window_days": WINDOW_DAYS,
    "recoverable_units_window": 0, "recoverable_units_per_year": 0,
    "unit_value_gbp": None, "recoverable_value_window": None,
    "recoverable_value_per_year": None, "components": [], "biggest_lever": None,
    "lever_label": None, "lever_action": None,
    "lever_recoverable_units_per_year": 0, "lever_recoverable_value_per_year": None,
    "oee_trend": "new", "prior_oee": None, "oee_points_delta": None,
}

MINUTES_PER_DAY = 24 * 60


def _prior_production(db, days: int = WINDOW_DAYS):
    """Production in the window BEFORE the current one (days..2*days ago) — last
    week — so the opportunity can be trended. Auto-scoped like every query here
    (ADR-0002)."""
    today = datetime.utcnow().date()
    start = datetime.combine(today - timedelta(days=2 * days - 1), datetime.min.time())
    end = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    return (db.query(models.ProductionRecord)
            .filter(models.ProductionRecord.created_at >= start,
                    models.ProductionRecord.created_at < end)
            .all())


def _physical_good(records, good: int, days: int) -> int:
    """Good output capped at what the machines could physically make in the window.

    We annualise the window x365/days, so the window's good count must reflect at
    most a real week. A real tenant never exceeds this — a machine can't run more
    than 24h/day — so this is a no-op on real data. It only tames a simulator that
    piled more machine-minutes into the window than physically exist (long uptime,
    parallel workers), which would otherwise annualise into an absurd figure."""
    machines = len({getattr(r, "machine_id", None) for r in records
                    if getattr(r, "machine_id", None) is not None}) or 1
    ceiling = machines * days * MINUTES_PER_DAY          # planned minutes a plant can run
    planned = sum((getattr(r, "planned_minutes", 0) or 0) for r in records)
    if planned <= ceiling or planned <= 0:
        return good
    return round(good * ceiling / planned)


def build_recovery_summary(db, tenant: str) -> dict:
    """The recovery opportunity over the last 7 days: gap to world-class OEE and
    what closing it is worth in good units. production_records is auto-scoped."""
    records = _recent_production(db, days=WINDOW_DAYS)
    o = pooled_oee(records)
    if not o["has_data"] or o["oee"] <= 0:
        return dict(_EMPTY)

    # Cap the window's good output at physical capacity before annualising, so a
    # simulator that over-produced can't blow the per-year figure up (no-op on
    # real data). OEE is a ratio of sums, so it's unaffected by this.
    good = _physical_good(records, sum(r.good_count or 0 for r in records), WINDOW_DAYS)
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
    # The one lever to fix first — shared definition (biggest gap to world-class),
    # identical to the OEE summary's "biggest drag".
    lever_key = biggest_lever(o)
    biggest = next((c for c in components if c["key"] == lever_key), None)

    # Value the recoverable output in £ only when the tenant has SET a per-unit
    # rate (None = unset -> units only). A configured rate of 0 is a real £0 margin,
    # so it yields £0, matching build_management_summary — `is not None`, not
    # truthiness, so the two money surfaces agree on a £0 rate.
    rate = _unit_value(db, tenant)
    value_window = round(recoverable_window * rate) if rate is not None else None
    value_year = round(recoverable_year * rate) if rate is not None else None

    # "Fix this first": the prize for closing JUST the biggest lever's gap.
    # Closing one component from current -> target scales good output by
    # target/current (same run time), so it's a real, isolated £/units figure.
    lever_units_year = 0
    if lever_key and biggest["current"] > 0:
        lever_window = round(good * (biggest["target"] / biggest["current"] - 1))
        lever_units_year = round(lever_window * 365 / WINDOW_DAYS)
    lever_value_year = round(lever_units_year * rate) if (rate is not None and lever_units_year) else None

    # Trend: is the plant closing the gap? Compare this window's OEE to last
    # week's, via the shared week-over-week direction (one dead-band, so this badge
    # and the scorecard's OEE delta can't disagree). "new" until there's a prior week.
    po = pooled_oee(_prior_production(db, days=WINDOW_DAYS))
    if po["has_data"] and po["oee"] > 0:
        prior_oee = po["oee"]
        delta = o["oee"] - prior_oee
        oee_trend = {"up": "improving", "down": "worsening",
                     "flat": "flat"}[oee_direction(o["oee"], prior_oee)]
    else:
        prior_oee, delta, oee_trend = None, None, "new"

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
        "biggest_lever": lever_key,
        "lever_label": biggest["label"] if lever_key else None,
        "lever_action": LEVER_ACTIONS.get(lever_key) if lever_key else None,
        "lever_recoverable_units_per_year": lever_units_year,
        "lever_recoverable_value_per_year": lever_value_year,
        "oee_trend": oee_trend,
        "prior_oee": prior_oee,
        "oee_points_delta": delta,
    }
