"""Agent activity trend — the fleet's last-7-days daily action counts (ADR-0005 / ADR-0007).

Everything else in the platform answers "what's true now"; this read-model adds
the time dimension for the agents: a calendar day-by-day count of agent actions
over the last week, so the UI can draw a sparkline of how busy the fleet has been.
Reads agent_actions (its created_at is indexed), tenant-scoped explicitly.
"""
from collections import Counter
from datetime import datetime, timedelta

import models

name = "trends"

WINDOW_DAYS = 7


def build_agent_trend(db, tenant: str) -> dict:
    """A 7-entry daily series (oldest -> newest) of agent-action counts."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    counts = Counter(
        r.created_at.date()
        for r in db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant).all()
        if r.created_at and r.created_at.date() in window_set
    )
    daily = [{"date": d.isoformat(), "count": counts.get(d, 0)} for d in window]
    return {
        "days": WINDOW_DAYS,
        "total": sum(e["count"] for e in daily),
        "peak": max((e["count"] for e in daily), default=0),
        "daily": daily,
    }


def _series(window, records, day_fn, value_fn):
    """A daily {date, count} series over the window, summing value_fn per day."""
    per_day: dict = {}
    for r in records:
        d = day_fn(r)
        if d is not None:
            per_day[d] = per_day.get(d, 0) + value_fn(r)
    return [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in window]


def build_ops_trends(db, tenant: str) -> dict:
    """Last-7-days daily series across the four pillars — production output,
    downtime events, quality failures, and agent actions. A read-model over the
    tenant's tables (the core ones are auto-scoped; agent_actions is stamped, so
    it is filtered by tenant explicitly, ADR-0002). Adds no storage."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)

    def day(dt):
        return dt.date() if dt and dt.date() in window_set else None

    prod = db.query(models.ProductionRecord).all()
    down = db.query(models.DowntimeLog).all()
    qual = db.query(models.QualityInspection).all()
    acts = db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant).all()

    return {
        "days": WINDOW_DAYS,
        "production": _series(window, prod, lambda r: day(r.created_at), lambda r: r.good_count or 0),
        "downtime": _series(window, down, lambda r: day(r.created_at), lambda r: 1),
        "quality_failed": _series(window, qual, lambda r: day(r.created_at), lambda r: r.failed_quantity or 0),
        "agent_actions": _series(window, acts, lambda r: day(r.created_at), lambda r: 1),
    }
