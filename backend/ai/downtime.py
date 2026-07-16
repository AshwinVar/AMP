"""Downtime summary — a fleet-wide read-model over downtime (ADR-0007).

Answers the first question a plant manager asks each morning: *what's stopping
my machines?* Over the last week it rolls up the total downtime events, the top
reasons (a Pareto), the machines losing the most time, and a day-by-day series.
A read-model over downtime_logs — auto-scoped to the tenant by the query layer
(ADR-0002) in a request context; it adds no storage.
"""
from collections import Counter
from datetime import datetime, timedelta

import models

name = "downtime"

WINDOW_DAYS = 7
TOP_N = 5


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

    reasons = Counter((d.reason or "Unknown").strip() or "Unknown" for d in logs)
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
