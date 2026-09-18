"""Digital-twin overlay read-model tests (ADR-0007).

Per-machine OEE + losses (good units, and £ only at the tenant's unit value), keyed
by machine, for heating the floor map.
Run:  python backend/test_twin_overlay.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import twin


def _fresh_session(unit_value=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    if unit_value is not None:
        db.add(models.TenantConfig(tenant_code="DEFAULT", plan="Pro", unit_value_gbp=unit_value))
        db.commit()
    return db


def _two_machines(db):
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=80, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"))
    # machine 1: 40 min downtime at the pooled 190/920 a minute ≈ 8 units + 10 rejected = 18 units
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10))
    # machine 2: clean run -> 0 units
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=480, runtime_minutes=480,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0))
    db.commit()


def test_overlay_keys_oee_and_cost_by_machine():
    db = _fresh_session(unit_value=12.5)
    _two_machines(db)

    o = twin.build_twin_overlay(db, "DEFAULT")
    by = {m["machine_id"]: m for m in o["machines"]}
    assert set(by) == {1, 2} and o["priced"] is True
    assert by[1]["lost_units"] == 18 and by[1]["cost"] == 225       # 18 units x £12.50
    assert by[2]["lost_units"] == 0 and by[2]["cost"] == 0
    assert isinstance(by[1]["oee"], int) and isinstance(by[2]["oee"], int)

    # no unit value -> the map heats by lost units and carries no £ (ADR-0010)
    db = _fresh_session()
    _two_machines(db)
    o = twin.build_twin_overlay(db, "DEFAULT")
    by = {m["machine_id"]: m for m in o["machines"]}
    assert o["priced"] is False
    assert by[1]["lost_units"] == 18 and by[1]["cost"] is None and by[2]["cost"] is None

    # empty -> no machines, no crash
    empty = twin.build_twin_overlay(_fresh_session(), "DEFAULT")
    assert empty["machines"] == []


def test_overlay_cost_covers_every_machine_not_just_the_top_five():
    """Regression: the overlay must key cost off the FULL per-machine cost map, not
    build_cost_summary's TOP_N `by_machine` display page. With more cost-incurring
    machines than the cap, a machine ranked outside the top 5 must still carry its
    real cost on the map — not paint as £0. Both OEE and cost sides cover every
    machine that ran (one shared basis, rule 3)."""
    db = _fresh_session(unit_value=10)
    # 7 machines, each with distinct decreasing downtime, each making half a good unit
    # per run minute: M1 -> 70 min -> 35 units -> £350 ... M7 -> 10 min -> 5 units -> £50.
    expected_cost = {}
    for i in range(1, 8):
        db.add(models.Machine(id=i, name=f"M{i}", status="Running", utilization=80, line="SMT"))
        down_min = (8 - i) * 10
        runtime = 480 - down_min
        db.add(models.ProductionRecord(machine_id=i, planned_minutes=480,
                                       runtime_minutes=runtime, ideal_cycle_time_seconds=30,
                                       total_count=runtime // 2, good_count=runtime // 2, rejected_count=0))
        expected_cost[i] = down_min // 2 * 10
    db.commit()

    o = twin.build_twin_overlay(db, "DEFAULT")
    by = {m["machine_id"]: m for m in o["machines"]}
    # Every machine that ran is on the map (not just the 5 costliest).
    assert set(by) == set(range(1, 8)), set(by)
    # Each machine carries its real cost — crucially the ones past the top-5 cap
    # (M6 -> £100, M7 -> £50) are NOT dropped to 0.
    for mid, c in expected_cost.items():
        assert by[mid]["cost"] == c, (mid, by[mid]["cost"], c)
    assert by[6]["cost"] == 100 and by[7]["cost"] == 50
    # And every machine still has its OEE, so both heat sources share one basis.
    assert all(isinstance(by[mid]["oee"], int) for mid in range(1, 8))


if __name__ == "__main__":
    test_overlay_keys_oee_and_cost_by_machine()
    test_overlay_cost_covers_every_machine_not_just_the_top_five()
    print("TWIN OVERLAY OK: per-machine OEE + cost of losses keyed by machine (full map, not top-N); empty-safe")
