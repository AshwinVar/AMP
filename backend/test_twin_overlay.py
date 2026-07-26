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


def test_overlay_cost_covers_every_machine_not_just_the_top_five():
    """Regression: the overlay must key cost off the FULL per-machine cost map, not
    build_cost_summary's TOP_N `by_machine` display page. With more cost-incurring
    machines than the cap, a machine ranked outside the top 5 must still carry its
    real cost on the map — not paint as £0. Both OEE and cost sides cover every
    machine that ran (one shared basis, rule 3)."""
    db = _fresh_session()
    # 7 machines, each with distinct decreasing downtime: M1 -> 70min/$840 ... M7 -> 10min/$120.
    expected_cost = {}
    for i in range(1, 8):
        db.add(models.Machine(id=i, name=f"M{i}", status="Running", utilization=80, line="SMT"))
        down_min = (8 - i) * 10
        db.add(models.ProductionRecord(machine_id=i, planned_minutes=480,
                                       runtime_minutes=480 - down_min, ideal_cycle_time_seconds=30,
                                       total_count=100, good_count=100, rejected_count=0))
        expected_cost[i] = down_min * 12   # DOWNTIME_COST_PER_MIN
    db.commit()

    o = twin.build_twin_overlay(db, "DEFAULT")
    by = {m["machine_id"]: m for m in o["machines"]}
    # Every machine that ran is on the map (not just the 5 costliest).
    assert set(by) == set(range(1, 8)), set(by)
    # Each machine carries its real cost — crucially the ones past the top-5 cap
    # (M6 -> $240, M7 -> $120) are NOT dropped to 0.
    for mid, c in expected_cost.items():
        assert by[mid]["cost"] == c, (mid, by[mid]["cost"], c)
    assert by[6]["cost"] == 240 and by[7]["cost"] == 120
    # And every machine still has its OEE, so both heat sources share one basis.
    assert all(isinstance(by[mid]["oee"], int) for mid in range(1, 8))


if __name__ == "__main__":
    test_overlay_keys_oee_and_cost_by_machine()
    test_overlay_cost_covers_every_machine_not_just_the_top_five()
    print("TWIN OVERLAY OK: per-machine OEE + cost of losses keyed by machine (full map, not top-N); empty-safe")
