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


def test_work_order_pressure_counts_only_open_orders():
    """The SQL bound in assess_from_db must produce IDENTICAL risk to a
    whole-table scan: a FINISHED order carries no outstanding demand, so its gap
    must not touch work_order_pressure.

    UPDATED in #583, and the update is the point. This test used to assert 650,
    excluding a Planned order with 9,000 units outstanding — because the engine
    bounded on ACTIVE_WORK_ORDER_STATUSES = ("Running", "Delayed"), two words AMP
    never writes. The test was pinning the whitelist, so it agreed with the
    engine and both were wrong about the plant: a Planned order that has produced
    nothing is the largest outstanding demand there is.

    "Open" is now work_order_status's complement — every status except the
    finished ones. Independently derived: Running (800-200)=600 + Delayed
    (100-50)=50 + Planned (9000-0)=9000 = 9,650. Only the Completed 5,000 is
    excluded."""
    db = _fresh_session()
    m = models.Machine(name="M1", status="Running", utilization=75, tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    db.add_all([
        _wo(m.id, "WO-RUN", "Running", 800, 200),      # +600  (unknown word -> open)
        _wo(m.id, "WO-DLY", "Delayed", 100, 50),        # +50   (unknown word -> open)
        _wo(m.id, "WO-PLAN", "Planned", 9000, 0),       # +9000 (real outstanding demand)
        _wo(m.id, "WO-DONE", "Completed", 5000, 0),     # excluded: finished
    ])
    db.commit()

    row = prediction.assess_from_db(db)[0]
    assert row["work_order_pressure"] == 9650, row["work_order_pressure"]
    # The Completed 5,000 is the one gap that must NOT count, and dropping it is
    # what makes this an assertion rather than "sum everything".
    assert "high active work-order load" in row["reasons"], row["reasons"]
    print("PASS work-order pressure counts every OPEN order and no finished one")


def test_no_open_work_orders_means_no_pressure():
    """A machine whose work is all FINISHED has zero pressure and no
    work-order-load reason.

    The Planned row that used to sit in this fixture moved out with #583: it is
    open work, so keeping it here would have asserted that 9,000 outstanding
    units are worth nothing. Cancelled joins Completed instead — a withdrawn
    order is off the board, the same call ai/supply makes for a cancelled PO."""
    db = _fresh_session()
    m = models.Machine(name="M1", status="Running", utilization=75, tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    db.add_all([
        _wo(m.id, "WO-DONE", "Completed", 5000, 0),
        _wo(m.id, "WO-CANC", "Cancelled", 9000, 0),
    ])
    db.commit()

    row = prediction.assess_from_db(db)[0]
    assert row["work_order_pressure"] == 0, row["work_order_pressure"]
    assert "high active work-order load" not in row["reasons"], row["reasons"]
    print("PASS finished work orders contribute no pressure")


if __name__ == "__main__":
    test_old_history_washes_out()
    test_recent_history_scores()
    test_current_state_is_not_windowed()
    test_work_order_pressure_counts_only_open_orders()
    test_no_open_work_orders_means_no_pressure()
    print("ALL PREDICTION TESTS PASSED")
