"""Digital-twin overlay read-model tests (ADR-0007).

Per-machine OEE + cost-of-losses, keyed by machine, for heating the floor map.
Run:  python backend/test_twin_overlay.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import twin


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_overlay_keys_oee_and_cost_by_machine():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=80, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"))
    # machine 1: 40 min downtime + 10 rejected -> cost 480 + 250 = 730
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10))
    # machine 2: clean run -> cost 0
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=480, runtime_minutes=480,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0))
    db.commit()

    o = twin.build_twin_overlay(db, "DEFAULT")
    by = {m["machine_id"]: m for m in o["machines"]}
    assert set(by) == {1, 2}
    assert by[1]["cost"] == 730 and by[2]["cost"] == 0
    assert isinstance(by[1]["oee"], int) and isinstance(by[2]["oee"], int)

    # empty -> no machines, no crash
    empty = twin.build_twin_overlay(_fresh_session(), "DEFAULT")
    assert empty["machines"] == []


if __name__ == "__main__":
    test_overlay_keys_oee_and_cost_by_machine()
    print("TWIN OVERLAY OK: per-machine OEE + cost of losses keyed by machine; empty-safe")
