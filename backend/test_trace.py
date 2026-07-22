"""Work-order traceability read-model tests (ADR-0007).

One job's genealogy: the plans it ran under, the materials issued against it and
the goods received from it, what quality found, the downtime on its machine while
it was live, the merged timeline, and the traceability gaps in that record.
Run:  python backend/test_trace.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import trace


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, id_, name, line="SMT"):
    db.add(models.Machine(id=id_, name=name, status="Running", line=line))


def _wo(db, no, machine_id, target, actual, status="Running", hours_ago=48,
        planned_end_offset_hours=None, part="PCB-A", batch="B-1"):
    db.add(models.WorkOrder(
        work_order_no=no, part_number=part, batch_number=batch, machine_id=machine_id,
        target_quantity=target, actual_quantity=actual, status=status,
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
        planned_start=datetime.utcnow() - timedelta(hours=hours_ago),
        planned_end=(datetime.utcnow() + timedelta(hours=planned_end_offset_hours)
                     if planned_end_offset_hours is not None else None),
    ))


def _plan(db, no, wo_id, machine_id, planned, actual, day_offset, shift="Day"):
    db.add(models.ProductionPlan(
        plan_no=no, work_order_id=wo_id, machine_id=machine_id,
        planned_quantity=planned, actual_quantity=actual, shift_name=shift,
        plan_date=(datetime.utcnow().date() + timedelta(days=day_offset)),
        created_at=datetime.utcnow() - timedelta(days=abs(day_offset)),
    ))


def _inspection(db, no, wo_id, machine_id, inspected, passed, failed,
                defect=None, scrap=0, rework=0, hours_ago=2):
    db.add(models.QualityInspection(
        inspection_no=no, work_order_id=wo_id, machine_id=machine_id, inspector="QA-1",
        inspected_quantity=inspected, passed_quantity=passed, failed_quantity=failed,
        defect_category=defect, scrap_quantity=scrap, rework_quantity=rework,
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
    ))


def _item(db, id_, code, name, category="Raw", unit="pcs"):
    db.add(models.InventoryItem(id=id_, item_code=code, item_name=name, category=category,
                                unit=unit, current_stock=100, reorder_level=10))


def _txn(db, item_id, ttype, qty, reference, hours_ago=6, notes=None):
    db.add(models.InventoryTransaction(
        item_id=item_id, transaction_type=ttype, quantity=qty, reference=reference,
        notes=notes, created_at=datetime.utcnow() - timedelta(hours=hours_ago),
    ))


def _downtime(db, machine_id, reason, duration, hours_ago=5):
    db.add(models.DowntimeLog(
        machine_id=machine_id, reason=reason, duration=duration,
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
    ))


def _fully_traced_job():
    """A job with a complete record: plans, materials in and out, inspections and
    a stoppage — the happy path every section reads from."""
    db = _fresh_session()
    _machine(db, 1, "SMT-Placer-01")
    _machine(db, 2, "IC-Test-01", line="IC")
    _wo(db, "WO-100", 1, target=500, actual=460, hours_ago=48)
    _item(db, 11, "RAW-PCB", "Bare PCB")
    _item(db, 12, "FG-BOARD", "Assembled board", category="Finished")
    db.commit()
    wo = db.query(models.WorkOrder).filter_by(work_order_no="WO-100").first()

    _plan(db, "P-1", wo.id, 1, 250, 250, -2, shift="Day")
    _plan(db, "P-2", wo.id, 1, 250, 210, -1, shift="Night")
    _inspection(db, "QI-1", wo.id, 1, inspected=200, passed=195, failed=5,
                defect="Solder bridge", scrap=3, rework=2, hours_ago=20)
    _inspection(db, "QI-2", wo.id, 2, inspected=260, passed=250, failed=10,
                defect="Misalignment", scrap=4, rework=6, hours_ago=3)
    _txn(db, 11, "Issue", 500, "WO-100", hours_ago=40, notes="Auto-issued for WO WO-100")
    _txn(db, 11, "OUT", 20, "WO-100", hours_ago=30)          # simulator dialect
    _txn(db, 12, "Receive", 460, "WO-100", hours_ago=2)
    _txn(db, 11, "Issue", 30, "WO-999", hours_ago=10)        # another job — must not leak in
    _downtime(db, 1, "Feeder jam", "1 hr 30 min", hours_ago=25)
    _downtime(db, 1, "Nozzle change", "20 min", hours_ago=10)
    _downtime(db, 2, "Calibration", "45 min", hours_ago=10)   # other machine — excluded
    _downtime(db, 1, "Ancient stoppage", "3 hrs", hours_ago=200)  # before the job — excluded
    db.commit()
    return db


def test_trace_composes_the_full_record_of_one_job():
    db = _fully_traced_job()
    t = trace.build_work_order_trace(db, "DEFAULT", "WO-100")

    # Header: the job and how far it got.
    assert t["found"] is True and t["work_order_no"] == "WO-100"
    assert t["part_number"] == "PCB-A" and t["batch_number"] == "B-1"
    assert t["machine"] == "SMT-Placer-01" and t["line"] == "SMT"
    assert t["target"] == 500 and t["actual"] == 460
    assert t["shortfall"] == 40 and t["progress_rate"] == 92
    assert t["closed"] is False and t["days_late"] == 0

    # Scheduled: both plans, pooled attainment 460/500.
    assert t["plans"]["count"] == 2 and t["plans"]["planned"] == 500
    assert t["plans"]["actual"] == 460 and t["plans"]["attainment_rate"] == 92
    assert t["shifts"] == ["Day", "Night"]
    assert t["plans"]["rows"][0]["plan_no"] == "P-1"        # oldest plan first
    assert t["plans"]["rows"][1]["shortfall"] == 40

    # Inspected: pooled over both inspections; the defect Pareto is biggest first.
    q = t["quality"]
    assert q["inspections"] == 2 and q["inspected"] == 460
    assert q["passed"] == 445 and q["failed"] == 15
    assert q["scrap"] == 7 and q["rework"] == 8
    assert q["first_pass_yield"] == 97 and q["fail_rate"] == 3
    assert [d["category"] for d in q["defects"]] == ["Misalignment", "Solder bridge"]
    assert q["rows"][0]["inspection_no"] == "QI-2"          # newest inspection first

    # Material genealogy: both consumption dialects summed, receipts kept apart,
    # and another job's movement never counted.
    m = t["materials"]
    assert m["consumed"] == 520 and m["received"] == 460
    assert [r["item_code"] for r in m["rows"]] == ["RAW-PCB", "FG-BOARD"]
    assert m["rows"][0]["consumed"] == 520 and m["rows"][0]["movements"] == 2
    assert m["rows"][1]["received"] == 460 and m["rows"][1]["item_name"] == "Assembled board"

    # Interrupted: only this machine, only inside the job's live window.
    d = t["downtime"]
    assert d["events"] == 2 and d["minutes"] == 110          # 90 + 20
    assert d["rows"][0]["reason"] == "Nozzle change"         # newest stoppage first

    # The merged record, newest first, across all four sources.
    kinds = {e["kind"] for e in t["timeline"]}
    assert kinds == {"plan", "inspection", "material", "downtime"}
    ats = [e["at"] for e in t["timeline"]]
    assert ats == sorted(ats, reverse=True)


def test_trace_flags_a_shortfall_explained_by_downtime():
    db = _fully_traced_job()
    t = trace.build_work_order_trace(db, "DEFAULT", "WO-100")
    msgs = " ".join(g["message"] for g in t["gaps"])
    assert "110 minutes of downtime" in msgs and "40-unit shortfall" in msgs
    # Fully recorded otherwise: no "unverified", no missing-material gap.
    assert "unverified" not in msgs and "no record of what" not in msgs


def test_trace_names_an_untraceable_batch():
    # Units booked as made with no inspection and no material issued: the two
    # gaps that break traceability outright.
    db = _fresh_session()
    _machine(db, 1, "SMT-Placer-01")
    _wo(db, "WO-200", 1, target=100, actual=100, status="Completed")
    db.commit()

    t = trace.build_work_order_trace(db, "DEFAULT", "WO-200")
    assert t["found"] is True and t["closed"] is True
    assert t["quality"]["inspections"] == 0 and t["materials"]["consumed"] == 0
    assert t["plans"]["count"] == 0 and t["downtime"]["events"] == 0
    assert t["timeline"] == []

    high = [g["message"] for g in t["gaps"] if g["severity"] == "high"]
    assert any("no quality inspection was recorded" in m for m in high)
    assert any("no record of what" in m for m in high)
    assert any("ran unscheduled" in g["message"] for g in t["gaps"])
    # High-severity gaps sort ahead of the medium ones.
    assert t["gaps"][0]["severity"] == "high"


def test_trace_flags_scrap_late_running_and_uncategorised_failures():
    db = _fresh_session()
    _machine(db, 1, "SMT-Placer-01")
    # Open, 2 days past its planned end.
    _wo(db, "WO-300", 1, target=200, actual=120, status="Running",
        hours_ago=96, planned_end_offset_hours=-48)
    _item(db, 11, "RAW-PCB", "Bare PCB")
    db.commit()
    wo = db.query(models.WorkOrder).filter_by(work_order_no="WO-300").first()
    _txn(db, 11, "Issue", 130, "WO-300")
    # 12 of 120 inspected scrapped (10%), and the failures name no defect.
    _inspection(db, "QI-9", wo.id, 1, inspected=120, passed=105, failed=15, scrap=12)
    db.commit()

    t = trace.build_work_order_trace(db, "DEFAULT", "WO-300")
    assert t["days_late"] == 2 and t["progress_rate"] == 60
    msgs = " ".join(g["message"] for g in t["gaps"])
    assert "12 of 120 inspected units were scrapped (10%)" in msgs
    assert "Still open 2 days past its planned end" in msgs
    assert "no defect category recorded" in msgs


def test_trace_handles_a_work_order_that_is_not_there():
    t = trace.build_work_order_trace(_fresh_session(), "DEFAULT", "WO-NOPE")
    assert t["found"] is False and t["work_order_no"] == "WO-NOPE"
    assert t["plans"]["rows"] == [] and t["quality"]["rows"] == []
    assert t["materials"]["rows"] == [] and t["downtime"]["rows"] == []
    assert t["timeline"] == [] and t["gaps"] == []


if __name__ == "__main__":
    test_trace_composes_the_full_record_of_one_job()
    test_trace_flags_a_shortfall_explained_by_downtime()
    test_trace_names_an_untraceable_batch()
    test_trace_flags_scrap_late_running_and_uncategorised_failures()
    test_trace_handles_a_work_order_that_is_not_there()
    print("TRACE OK: work-order genealogy — plans, material in/out (both dialects, no cross-job leak), "
          "quality rollup + defect Pareto, in-window downtime on its own machine, merged timeline, "
          "and the traceability gaps (unverified batch, no material record, scrap, late, unscheduled)")
