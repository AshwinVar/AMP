"""Prediction risk-window tests.

Risk describes the machine's RECENT condition: history older than
RISK_WINDOW_DAYS must not keep a recovered machine scored as risky, while the
same history inside the window must. Current state (status/utilization) and
open work-order pressure are point-in-time and unaffected by the window.

Run:  python backend/test_prediction.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import prediction


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db, days_ago):
    """A neutral running machine with a genuinely bad week `days_ago` days back:
    180 downtime minutes over 6 events, 10% rejects."""
    when = datetime.utcnow() - timedelta(days=days_ago)
    m = models.Machine(name="M1", status="Running", utilization=75, tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    for _ in range(6):
        db.add(models.DowntimeLog(machine_id=m.id, reason="Breakdown", duration="30 min",
                                  created_at=when))
    db.add(models.ProductionRecord(machine_id=m.id, planned_minutes=480, runtime_minutes=400,
                                   ideal_cycle_time_seconds=45, total_count=1000,
                                   good_count=900, rejected_count=100, created_at=when))
    db.commit()
    return m


def test_old_history_washes_out():
    db = _fresh_session()
    _seed(db, days_ago=prediction.RISK_WINDOW_DAYS + 10)
    row = prediction.assess_from_db(db)[0]
    assert row["downtime_minutes"] == 0, row
    assert row["downtime_events"] == 0
    assert row["reject_rate"] == 0
    assert row["risk_level"] == "Low", row
    assert "no major risk indicators detected" in row["reasons"]
    print("PASS history outside the window washes out")


def test_recent_history_scores():
    db = _fresh_session()
    _seed(db, days_ago=5)
    row = prediction.assess_from_db(db)[0]
    assert row["downtime_minutes"] == 180
    assert row["downtime_events"] == 6
    assert row["reject_rate"] == 10.0
    assert row["risk_level"] in ("High", "Critical"), row
    assert any("downtime" in r for r in row["reasons"])
    print("PASS recent history scores as risky")


def test_current_state_is_not_windowed():
    db = _fresh_session()
    db.add(models.Machine(name="M2", status="Breakdown", utilization=0, tenant_code="DEFAULT"))
    db.commit()
    row = prediction.assess_from_db(db)[0]
    assert "machine currently in breakdown" in row["reasons"]
    print("PASS current state scores regardless of the window")


if __name__ == "__main__":
    test_old_history_washes_out()
    test_recent_history_scores()
    test_current_state_is_not_windowed()
    print("ALL PREDICTION TESTS PASSED")
