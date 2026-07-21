"""OEE summary tests (ADR-0007).

The plant OEE read-model pools the week's production into one plant-level
Availability x Performance x Quality, flags the component dragging it down, and
ranks machines worst-first. Tenant-scoping is inherited from the query layer
(ADR-0002) and covered by the twin tests; here we pin the composition math.

Run:  python backend/test_oee.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import oee


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_empty_plant_reports_no_data():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Idle", utilization=0))
    db.commit()
    s = oee.build_oee_summary(db, "DEFAULT")
    assert s["plant"]["has_data"] is False and s["plant"]["oee"] == 0
    assert s["machine_count"] == 1 and s["machines_with_data"] == 0
    assert s["machines"] == [] and s["biggest_drag"] is None and s["by_line"] == []
    assert s["worst"] is None and s["best"] is None
    assert len(s["daily"]) == 7 and all(d["oee"] == 0 for d in s["daily"])   # flat-zero trend


def test_plant_oee_pools_machines_and_ranks_worst_first():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=90, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70, line="IC"))
    # PRESS-01 runs fast machines slowly: ideal 30s x 100 units = 3000 machine-s
    # against 440 min = 26,400 s of runtime -> a very low performance component.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90, rejected_count=10))
    # CNC-02: healthy throughput and near-clean quality.
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=480, runtime_minutes=400,
                                   ideal_cycle_time_seconds=55, total_count=400, good_count=380, rejected_count=20))
    db.commit()

    s = oee.build_oee_summary(db, "DEFAULT")
    assert s["plant"]["has_data"] and 0 <= s["plant"]["oee"] <= 100
    # Pooled quality = (90 + 380) / (100 + 400) = 470 / 500 = 94%.
    assert s["plant"]["quality"] == 94
    # Pooled performance = (30*100 + 55*400) / ((440+400)*60) = 25000 / 50400 = 50%.
    assert s["plant"]["performance"] == 50
    # ...so Performance is the lever holding the plant back, not Quality.
    assert s["biggest_drag"] == "performance"
    assert s["machine_count"] == 2 and s["machines_with_data"] == 2
    assert s["worst"]["oee"] <= s["best"]["oee"]                    # worst-first
    assert s["machines"][0]["machine_id"] == s["worst"]["machine_id"]
    assert s["machines"][-1]["machine_id"] == s["best"]["machine_id"]
    # 7-day trend: production is all today, so the last day equals the plant OEE
    # and the earlier (empty) days read 0.
    assert len(s["daily"]) == 7
    assert s["daily"][-1]["oee"] == s["plant"]["oee"]
    assert s["daily"][0]["oee"] == 0
    # per-line OEE: SMT (PRESS-01, quality 90/100) and IC (CNC-02, quality 380/400)
    by_line = {l["line"]: l for l in s["by_line"]}
    assert set(by_line) == {"SMT", "IC"}
    assert by_line["SMT"]["quality"] == 90 and by_line["IC"]["quality"] == 95
    assert s["machines"][0]["line"] in ("SMT", "IC")


def test_biggest_drag_is_gap_to_world_class_not_lowest_raw():
    # The live-demo case: Availability is the LOWEST raw component but sits AT its
    # 90% target, while Performance is 4 points short of 95. "Biggest drag" must be
    # the gap to world-class (Performance) — matching the recovery card's "biggest
    # lever" — not the lowest raw number (which would wrongly say Availability).
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=900,
                                   ideal_cycle_time_seconds=49, total_count=1000,
                                   good_count=990, rejected_count=10))
    db.commit()
    s = oee.build_oee_summary(db, "DEFAULT")
    assert s["plant"]["availability"] == 90      # lowest raw component...
    assert s["plant"]["performance"] == 91       # ...but this one carries the gap
    assert s["plant"]["quality"] == 99
    assert s["biggest_drag"] == "performance"
    print("PASS biggest drag is the gap to world-class, not the lowest raw component")


if __name__ == "__main__":
    test_empty_plant_reports_no_data()
    test_plant_oee_pools_machines_and_ranks_worst_first()
    test_biggest_drag_is_gap_to_world_class_not_lowest_raw()
    print("OEE OK: plant-level OEE pooled from production; biggest-drag component; machines ranked worst-first")
