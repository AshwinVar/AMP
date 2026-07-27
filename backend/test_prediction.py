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


def _wo(machine_id, no, status, target, actual):
    return models.WorkOrder(
        work_order_no=no, part_number="P1", batch_number="B1",
        machine_id=machine_id, target_quantity=target, actual_quantity=actual,
        status=status, tenant_code="DEFAULT",
    )


def test_work_order_pressure_counts_only_active_orders():
    """The SQL bound in assess_from_db (status IN active) must produce IDENTICAL
    risk to the old whole-table scan: Completed/Planned orders are never used, so
    their outstanding gap must not touch work_order_pressure. Independently
    derived: Running (800-200)=600 + Delayed (100-50)=50 = 650, and the huge
    Completed/Planned gaps (5000, 9000) are excluded."""
    db = _fresh_session()
    m = models.Machine(name="M1", status="Running", utilization=75, tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    db.add_all([
        _wo(m.id, "WO-RUN", "Running", 800, 200),      # +600 pressure
        _wo(m.id, "WO-DLY", "Delayed", 100, 50),        # +50 pressure
        _wo(m.id, "WO-DONE", "Completed", 5000, 0),     # excluded
        _wo(m.id, "WO-PLAN", "Planned", 9000, 0),       # excluded
    ])
    db.commit()

    row = prediction.assess_from_db(db)[0]
    assert row["work_order_pressure"] == 650, row["work_order_pressure"]
    # 650 >= 500, so the high-load reason fires — and it fires from the active
    # orders alone, not the (much larger) excluded Completed/Planned gaps.
    assert "high active work-order load" in row["reasons"], row["reasons"]
    print("PASS work-order pressure counts only active orders (SQL bound is behaviour-preserving)")


def test_no_active_work_orders_means_no_pressure():
    """A machine whose only work orders are Completed/Planned has zero pressure
    and no work-order-load reason — the excluded rows contribute nothing."""
    db = _fresh_session()
    m = models.Machine(name="M1", status="Running", utilization=75, tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    db.add_all([
        _wo(m.id, "WO-DONE", "Completed", 5000, 0),
        _wo(m.id, "WO-PLAN", "Planned", 9000, 0),
    ])
    db.commit()

    row = prediction.assess_from_db(db)[0]
    assert row["work_order_pressure"] == 0, row["work_order_pressure"]
    assert "high active work-order load" not in row["reasons"], row["reasons"]
    print("PASS inactive work orders contribute no pressure")


if __name__ == "__main__":
    test_old_history_washes_out()
    test_recent_history_scores()
    test_current_state_is_not_windowed()
    test_work_order_pressure_counts_only_active_orders()
    test_no_active_work_orders_means_no_pressure()
    print("ALL PREDICTION TESTS PASSED")
