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


if __name__ == "__main__":
    test_downtime_summary_rolls_up_reasons_machines_and_days()
    print("DOWNTIME OK: total + Pareto reasons + worst machines (named) + 7-day series; windowed; empty-safe")
