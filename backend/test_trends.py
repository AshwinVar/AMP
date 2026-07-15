"""Agent activity trend read-model tests (ADR-0005 / ADR-0007).

A 7-entry daily series (oldest -> newest) of agent-action counts, calendar-day
based, tenant-scoped, excluding anything outside the window.

Run:  python backend/test_trends.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import trends


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, when, tenant="DEFAULT"):
    return models.AgentAction(tenant_code=tenant, agent=agent, action_type="x", summary="x",
                              ref_kind="maintenance_task", ref_id=1, status="Proposed", created_at=when)


def test_agent_trend_daily_window_is_scoped_and_bounded():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add_all([
        _action("maintenance", now),                     # today
        _action("reorder", now),                         # today
        _action("quality", now - timedelta(days=2)),     # 2 days ago
        _action("escalation", now - timedelta(days=8)),  # outside the 7-day window
        _action("reorder", now, tenant="GMATS"),         # other tenant
    ])
    db.commit()

    t = trends.build_agent_trend(db, "DEFAULT")
    assert t["days"] == 7 and len(t["daily"]) == 7
    dates = [e["date"] for e in t["daily"]]
    assert dates == sorted(dates)                          # oldest -> newest
    assert t["daily"][-1]["date"] == now.date().isoformat() and t["daily"][-1]["count"] == 2  # today
    assert t["daily"][-3]["count"] == 1                    # 2 days ago
    assert t["total"] == 3                                 # 8-days-ago + GMATS both excluded
    assert t["peak"] == 2

    # empty tenant -> a flat 7-day series, no crash
    empty = trends.build_agent_trend(db, "NOBODY")
    assert empty["total"] == 0 and empty["peak"] == 0 and len(empty["daily"]) == 7


if __name__ == "__main__":
    test_agent_trend_daily_window_is_scoped_and_bounded()
    print("TRENDS OK: 7-day daily agent-action series; calendar-windowed; tenant-scoped; empty-safe")
