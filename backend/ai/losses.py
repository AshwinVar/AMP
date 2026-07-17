"""OEE losses — where the plant is losing OEE, in real terms (ADR-0007).

OEE = Availability x Performance x Quality; this read-model attributes the gap
from 100% to each factor using the standard cascading split (which sums exactly
to 100 - OEE), and attaches the concrete cost of each: downtime minutes, units
lost to slow cycles, and units rejected. A read-model over production_records —
auto-scoped to the tenant (ADR-0002); it reuses the twin's OEE math, adds no
storage.
"""
from ai.twin import _oee_from_records, _recent_production

name = "losses"

WINDOW_DAYS = 7

_EMPTY = {"has_data": False, "oee": 0, "total_loss": 0, "losses": [], "biggest": None}


def build_losses_summary(db, tenant: str) -> dict:
    """The OEE loss breakdown over the last 7 days: availability / performance /
    quality each as OEE points lost plus its concrete cost, biggest first.
    production_records is auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS)
    o = _oee_from_records(records)
    if not o["has_data"]:
        return dict(_EMPTY)

    a, p, q = o["availability"] / 100, o["performance"] / 100, o["quality"] / 100

    planned = sum(r.planned_minutes or 0 for r in records)
    runtime = sum(r.runtime_minutes or 0 for r in records)
    rejected = sum(r.rejected_count or 0 for r in records)
    # units the line could have made at ideal speed during runtime, minus what it did
    perf_units = 0
    for r in records:
        ideal = r.ideal_cycle_time_seconds or 0
        if ideal > 0:
            theoretical = int((r.runtime_minutes or 0) * 60 / ideal)
            perf_units += max(0, theoretical - (r.total_count or 0))

    # Cascading OEE-point attribution: these sum to 100 - OEE.
    losses = [
        {"key": "availability", "label": "Availability", "points": round(100 * (1 - a)),
         "detail": f"{max(0, planned - runtime):,} min of downtime"},
        {"key": "performance", "label": "Performance", "points": round(100 * a * (1 - p)),
         "detail": f"{perf_units:,} units lost to slow cycles"},
        {"key": "quality", "label": "Quality", "points": round(100 * a * p * (1 - q)),
         "detail": f"{rejected:,} units rejected"},
    ]
    biggest = max(losses, key=lambda l: l["points"])
    return {
        "has_data": True,
        "oee": o["oee"],
        "total_loss": 100 - o["oee"],
        "losses": losses,
        "biggest": biggest["key"] if biggest["points"] > 0 else None,
    }
