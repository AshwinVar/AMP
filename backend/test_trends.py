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


def test_ops_trends_four_pillar_daily_series():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=60))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="30 min", created_at=now))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=50, passed_quantity=45, failed_quantity=5, created_at=now))
    db.add(_action("maintenance", now))
    # a 9-days-ago production run must be excluded from the window
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0, created_at=now - timedelta(days=9)))
    db.commit()

    t = trends.build_ops_trends(db, "DEFAULT")
    assert t["days"] == 7
    for key in ("production", "downtime", "quality_failed", "agent_actions"):
        assert len(t[key]) == 7                       # all four pillars, 7 entries each
    assert t["production"][-1]["count"] == 90         # today's good units
    assert t["downtime"][-1]["count"] == 1
    assert t["quality_failed"][-1]["count"] == 5
    assert t["agent_actions"][-1]["count"] == 1
    assert sum(e["count"] for e in t["production"]) == 90   # 9-days-ago run excluded


if __name__ == "__main__":
    test_agent_trend_daily_window_is_scoped_and_bounded()
    test_ops_trends_four_pillar_daily_series()
    print("TRENDS OK: agent-action series + four-pillar ops trends; calendar-windowed; tenant-scoped; empty-safe")
