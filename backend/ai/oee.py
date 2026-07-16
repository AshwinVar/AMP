"""OEE summary — the plant's headline metric as a read-model (ADR-0007).

Answers "how effective is the plant right now?": it rolls the last week of
production into one plant-level OEE (Availability x Performance x Quality) with
its three components, flags the component dragging OEE down, and ranks every
machine that has production so the worst is first for triage. A read-model over
production_records + machines — auto-scoped to the tenant by the query layer
(ADR-0002); it reuses the per-machine OEE math from the twin (ai/twin.py) and
adds no storage.
"""
from datetime import datetime, timedelta

import models
from ai.twin import _oee_from_records, _oee_by_machine, _recent_production

name = "oee"

WINDOW_DAYS = 7
WORLD_CLASS = 85  # the classic world-class OEE benchmark
_COMPONENTS = ("availability", "performance", "quality")


def _daily_oee(records, days: int) -> list:
    """Plant OEE per calendar day over the window (oldest -> newest), so the card
    can draw a trend. Each day pools that day's records; a day with no production
    reads 0 (nothing ran)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    by_day: dict = {}
    for r in records:
        if r.created_at:
            by_day.setdefault(r.created_at.date(), []).append(r)
    return [{"date": d.isoformat(), "oee": _oee_from_records(by_day[d])["oee"] if d in by_day else 0}
            for d in window]


def build_oee_summary(db, tenant: str) -> dict:
    """Plant-level OEE over the last 7 days, a daily trend, and a worst-first
    per-machine breakdown. The plant figure pools every machine's minutes and
    counts (so it weights by real output, not a naive average of averages).
    production_records and machines are auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS)
    plant = _oee_from_records(records)

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
        "daily": _daily_oee(records, WINDOW_DAYS),  # 7-day OEE trend
        "worst": machines[0] if machines else None,
        "best": machines[-1] if machines else None,
        "machines": machines,
    }
