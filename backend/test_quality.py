"""Quality summary read-model tests (ADR-0007).

First-pass yield, fail rate, a defect Pareto (desc), and the worst machines by
fail rate — over the tenant's quality inspections.

Run:  python backend/test_quality.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import quality


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _insp(no, machine_id, inspected, passed, failed, defect=None, rework=0, scrap=0):
    return models.QualityInspection(
        inspection_no=no, machine_id=machine_id, inspector="qa",
        inspected_quantity=inspected, passed_quantity=passed, failed_quantity=failed,
        defect_category=defect, rework_quantity=rework, scrap_quantity=scrap)


def test_quality_summary_rolls_up_yield_defects_and_machines():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60))
    db.add_all([
        _insp("QC-1", 1, inspected=100, passed=80, failed=20, defect="surface", rework=5, scrap=2),
        _insp("QC-2", 1, inspected=100, passed=95, failed=5, defect="surface"),
        _insp("QC-3", 2, inspected=100, passed=98, failed=2, defect="dimension"),
    ])
    db.commit()

    s = quality.build_quality_summary(db, "DEFAULT")
    assert s["inspections"] == 3
    assert s["inspected"] == 300 and s["passed"] == 273 and s["failed"] == 27
    assert s["first_pass_yield"] == 91                      # 273/300
    assert s["fail_rate"] == 9                              # 27/300
    assert s["rework"] == 5 and s["scrap"] == 2
    # defect Pareto: surface (20+5=25) before dimension (2)
    assert s["top_defects"][0] == {"category": "surface", "count": 25}
    assert s["top_defects"][1] == {"category": "dimension", "count": 2}
    # worst machine by fail rate: PRESS-01 (25/200 = 12.5% -> 12, banker's rounding) before CNC-02 (2/100 = 2%)
    assert s["by_machine"][0]["name"] == "PRESS-01" and s["by_machine"][0]["fail_rate"] == 12
    assert s["by_machine"][1]["name"] == "CNC-02" and s["by_machine"][1]["fail_rate"] == 2

    # no inspections -> zeros, no divide-by-zero
    empty = quality.build_quality_summary(_fresh_session(), "DEFAULT")
    assert empty["inspections"] == 0 and empty["first_pass_yield"] == 0 and empty["top_defects"] == []


if __name__ == "__main__":
    test_quality_summary_rolls_up_yield_defects_and_machines()
    print("QUALITY OK: first-pass yield + fail rate + defect Pareto + worst machines; empty-safe")
