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
