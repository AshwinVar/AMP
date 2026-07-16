"""Downtime summary read-model tests (ADR-0007).

Fleet downtime over the last 7 days: total, top reasons (Pareto, desc), worst
machines (with names), and a 7-entry daily series. Out-of-window events excluded.

Run:  python backend/test_downtime.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import downtime


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _dt(machine_id, reason, when):
    return models.DowntimeLog(machine_id=machine_id, reason=reason, duration="30 min", created_at=when)


def test_downtime_summary_rolls_up_reasons_machines_and_days():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60))
    db.add_all([
        _dt(1, "Breakdown", now),
        _dt(1, "Breakdown", now - timedelta(days=1)),
        _dt(1, "Tooling", now - timedelta(days=2)),
        _dt(2, "Breakdown", now),
        _dt(2, "Changeover", now - timedelta(days=3)),
        _dt(1, "Breakdown", now - timedelta(days=9)),   # outside the 7-day window
    ])
    db.commit()

    s = downtime.build_downtime_summary(db, "DEFAULT")
    assert s["total_events"] == 5                                    # 9-days-ago excluded
    # Pareto: Breakdown(3) leads, then Tooling/Changeover(1)
    assert s["top_reasons"][0] == {"reason": "Breakdown", "count": 3}
    assert {r["reason"] for r in s["top_reasons"]} == {"Breakdown", "Tooling", "Changeover"}
    # worst machine: PRESS-01 (3) before CNC-02 (2), with names resolved
    assert s["by_machine"][0]["name"] == "PRESS-01" and s["by_machine"][0]["count"] == 3
    assert s["by_machine"][1]["name"] == "CNC-02" and s["by_machine"][1]["count"] == 2
    # daily series: 7 entries oldest->newest, today has 2 (one per machine)
    assert len(s["daily"]) == 7 and s["daily"][-1]["count"] == 2
    assert sum(e["count"] for e in s["daily"]) == 5

    # empty factory -> zeros, no crash
    empty = downtime.build_downtime_summary(_fresh_session(), "DEFAULT")
    assert empty["total_events"] == 0 and empty["top_reasons"] == [] and len(empty["daily"]) == 7


def test_downtime_reason_drilldown_totals_minutes_machines_and_instances():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60))
    db.add_all([
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="120 min", created_at=now),
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="30 min", created_at=now - timedelta(days=1)),
        models.DowntimeLog(machine_id=2, reason="Breakdown", duration="45 min", created_at=now - timedelta(days=2)),
        models.DowntimeLog(machine_id=1, reason="Tooling", duration="15 min", created_at=now),           # other reason
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="99 min", created_at=now - timedelta(days=9)),  # out of window
    ])
    db.commit()

    r = downtime.build_downtime_reason(db, "DEFAULT", "Breakdown")
    assert r["reason"] == "Breakdown"
    assert r["total_events"] == 3                                    # 9-days-ago excluded, Tooling excluded
    assert r["total_minutes"] == 195                                 # 120 + 30 + 45, minutes parsed from strings
    # PRESS-01 leads: 2 events / 150 min; CNC-02: 1 event / 45 min
    assert r["by_machine"][0]["name"] == "PRESS-01" and r["by_machine"][0]["count"] == 2
    assert r["by_machine"][0]["minutes"] == 150
    assert r["by_machine"][1]["name"] == "CNC-02" and r["by_machine"][1]["minutes"] == 45
    assert len(r["daily"]) == 7 and r["daily"][-1]["count"] == 1     # today: one Breakdown
    assert len(r["instances"]) == 3                                  # most-recent first (by time)
    assert r["instances"][0]["minutes"] == 120 and r["instances"][0]["machine"] == "PRESS-01"  # today's
    assert r["instances"][-1]["minutes"] == 45                       # oldest in window (2 days ago)

    # a reason with no events in the window -> zeroed, no crash
    none = downtime.build_downtime_reason(db, "DEFAULT", "Nonexistent")
    assert none["total_events"] == 0 and none["total_minutes"] == 0 and none["instances"] == []


if __name__ == "__main__":
    test_downtime_summary_rolls_up_reasons_machines_and_days()
    test_downtime_reason_drilldown_totals_minutes_machines_and_instances()
    print("DOWNTIME OK: total + Pareto reasons + worst machines (named) + 7-day series; windowed; empty-safe; "
          "reason drill-down (minutes lost, machines, instances)")
