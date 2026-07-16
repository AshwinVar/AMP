"""OEE summary — the plant's headline metric as a read-model (ADR-0007).

Answers "how effective is the plant right now?": it rolls the last week of
production into one plant-level OEE (Availability x Performance x Quality) with
its three components, flags the component dragging OEE down, and ranks every
machine that has production so the worst is first for triage. A read-model over
production_records + machines — auto-scoped to the tenant by the query layer
(ADR-0002); it reuses the per-machine OEE math from the twin (ai/twin.py) and
adds no storage.
"""
import models
from ai.twin import _oee_from_records, _oee_by_machine, _recent_production

name = "oee"

WINDOW_DAYS = 7
WORLD_CLASS = 85  # the classic world-class OEE benchmark
_COMPONENTS = ("availability", "performance", "quality")


def build_oee_summary(db, tenant: str) -> dict:
    """Plant-level OEE over the last 7 days plus a worst-first per-machine
    breakdown. The plant figure pools every machine's minutes and counts (so it
    weights by real output, not a naive average of averages). production_records
    and machines are auto-scoped (ADR-0002)."""
    plant = _oee_from_records(_recent_production(db, days=WINDOW_DAYS))

    names = {m.id: m.name for m in db.query(models.Machine).all()}
    machines = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), **o}
        for mid, o in _oee_by_machine(db, days=WINDOW_DAYS).items()
        if o["has_data"]
    ]
    machines.sort(key=lambda m: m["oee"])  # worst OEE first, for triage

    # Which of the three levers is holding the plant back — the story a manager
    # wants first ("we're losing OEE to Performance, not Quality").
    drag = min(_COMPONENTS, key=lambda c: plant[c]) if plant["has_data"] else None

    return {
        "days": WINDOW_DAYS,
        "world_class": WORLD_CLASS,
        "plant": plant,                       # {oee, availability, performance, quality, has_data}
        "machine_count": len(names),
        "machines_with_data": len(machines),
        "biggest_drag": drag,                 # the component pulling plant OEE down
        "worst": machines[0] if machines else None,
        "best": machines[-1] if machines else None,
        "machines": machines,
    }
