"""Production summary read-model tests (ADR-0007).

Throughput and output quality over the last 7 days: units good/rejected, good
rate, top producing machines, and a daily good-count series. Out-of-window
records excluded.

Run:  python backend/test_production.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import production


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _rec(machine_id, total, good, rejected, when):
    return models.ProductionRecord(
        machine_id=machine_id, planned_minutes=480, runtime_minutes=440,
        ideal_cycle_time_seconds=30, total_count=total, good_count=good,
        rejected_count=rejected, created_at=when)


def test_production_summary_rolls_up_throughput_and_producers():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    db.add_all([
        _rec(1, total=100, good=95, rejected=5, when=now),
        _rec(1, total=100, good=90, rejected=10, when=now - timedelta(days=2)),
        _rec(2, total=100, good=99, rejected=1, when=now),
        _rec(1, total=100, good=100, rejected=0, when=now - timedelta(days=9)),  # outside window
    ])
    db.commit()

    s = production.build_production_summary(db, "DEFAULT")
    assert s["runs"] == 3                                    # 9-days-ago excluded
    assert s["total"] == 300 and s["good"] == 284 and s["rejected"] == 16
    assert s["good_rate"] == 95                              # 284/300 = 94.67 -> 95
    # top producers by good count: PRESS-01 (95+90=185) before CNC-02 (99)
    assert s["by_machine"][0]["name"] == "PRESS-01" and s["by_machine"][0]["good"] == 185
    assert s["by_machine"][1]["name"] == "CNC-02" and s["by_machine"][1]["good"] == 99
    # daily series: 7 entries oldest->newest, today = 95 + 99 = 194
    assert len(s["daily"]) == 7 and s["daily"][-1]["count"] == 194
    assert sum(e["count"] for e in s["daily"]) == 284        # matches total good in window

    # empty -> zeros, no divide-by-zero
    empty = production.build_production_summary(_fresh_session(), "DEFAULT")
    assert empty["runs"] == 0 and empty["good_rate"] == 0 and len(empty["daily"]) == 7


if __name__ == "__main__":
    test_production_summary_rolls_up_throughput_and_producers()
    print("PRODUCTION OK: throughput + good rate + top producers + 7-day series; windowed; empty-safe")
