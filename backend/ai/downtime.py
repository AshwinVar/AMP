"""Downtime summary — a fleet-wide read-model over downtime (ADR-0007).

Answers the first question a plant manager asks each morning: *what's stopping
my machines?* Over the last week it rolls up the total downtime events, the top
reasons (a Pareto), the machines losing the most time, and a day-by-day series.
A read-model over downtime_logs — auto-scoped to the tenant by the query layer
(ADR-0002) in a request context; it adds no storage.
"""
import re
from collections import Counter
from datetime import datetime, timedelta

import models

name = "downtime"

WINDOW_DAYS = 7
TOP_N = 5

_DIGITS = re.compile(r"\d+")


def _norm_reason(d) -> str:
    return (d.reason or "Unknown").strip() or "Unknown"


def _duration_minutes(duration) -> int:
    """Downtime durations are free-text strings ("120 min"); pull the leading
    number so the drill-down can total minutes lost. Unparseable -> 0."""
    if not duration:
        return 0
    m = _DIGITS.search(str(duration))
    return int(m.group()) if m else 0


def build_downtime_summary(db, tenant: str) -> dict:
    """Fleet downtime over the last 7 days: total, top reasons, worst machines,
    and a daily series. downtime_logs and machines are auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    logs = [
        d for d in db.query(models.DowntimeLog).all()
        if d.created_at and d.created_at.date() in window_set
    ]

    reasons = Counter(_norm_reason(d) for d in logs)
    top_reasons = [{"reason": r, "count": c} for r, c in reasons.most_common(TOP_N)]

    per_machine = Counter(d.machine_id for d in logs if d.machine_id is not None)
    names = {m.id: m.name for m in db.query(models.Machine).all()}
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), "count": c}
        for mid, c in per_machine.most_common(TOP_N)
    ]

    per_day = Counter(d.created_at.date() for d in logs)
    daily = [{"date": dd.isoformat(), "count": per_day.get(dd, 0)} for dd in window]

    return {
        "days": WINDOW_DAYS,
        "total_events": len(logs),
        "top_reasons": top_reasons,
        "by_machine": by_machine,
        "daily": daily,
    }


def build_downtime_reason(db, tenant: str, reason: str) -> dict:
    """Drill-down for a single downtime reason over the last 7 days: the totals
    (events and minutes lost), the machines it hits, a daily trend, and the most
    recent instances. Composes downtime_logs (auto-scoped, ADR-0002); adds no
    storage. Returns a zeroed shape when the reason has no events in the window."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    logs = [
        d for d in db.query(models.DowntimeLog).all()
        if d.created_at and d.created_at.date() in window_set and _norm_reason(d) == reason
    ]

    names = {m.id: m.name for m in db.query(models.Machine).all()}
    events = Counter(d.machine_id for d in logs if d.machine_id is not None)
    minutes_by_machine: dict = {}
    for d in logs:
        if d.machine_id is not None:
            minutes_by_machine[d.machine_id] = minutes_by_machine.get(d.machine_id, 0) + _duration_minutes(d.duration)
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), "count": c,
         "minutes": minutes_by_machine.get(mid, 0)}
        for mid, c in events.most_common(TOP_N)
    ]

    per_day = Counter(d.created_at.date() for d in logs)
    daily = [{"date": dd.isoformat(), "count": per_day.get(dd, 0)} for dd in window]

    recent = sorted(logs, key=lambda d: (d.created_at or datetime.min, d.id), reverse=True)[:10]
    instances = [{
        "id": d.id,
        "machine_id": d.machine_id,
        "machine": names.get(d.machine_id, f"#{d.machine_id}") if d.machine_id is not None else "—",
        "duration": d.duration,
        "minutes": _duration_minutes(d.duration),
        "notes": d.notes,
        "at": d.created_at.isoformat() if d.created_at else None,
    } for d in recent]

    return {
        "reason": reason,
        "days": WINDOW_DAYS,
        "total_events": len(logs),
        "total_minutes": sum(_duration_minutes(d.duration) for d in logs),
        "by_machine": by_machine,
        "daily": daily,
        "instances": instances,
    }
