"""The simulator's inspection and operator-job numbers are never issued twice.

tick_quality numbered inspections ``f"QI-{7000 + count + 1}"`` and tick_operator
numbered jobs ``f"EXE-{9000 + count + 1}"``, counting the tenant's rows. Both
tables are UNIQUE (tenant_code, number), and both have an Admin delete route
(DELETE /quality/inspections/{id}, DELETE /operator/executions/{id}). Delete one
inspection in a simulated tenant and the count drops by one, so every later
tick_quality builds a number that is still on a live row and hits the constraint.
The insert fails, so the count never grows and the next tick fails the same way.

In main._simulation_loop each tenant's ticks share one try block, so the failure
also rolls back that tick's shift entry, operator job and utilization drift.
tick_quality runs on half of all ticks. One deleted inspection froze half of the
demo factory's live activity, for good, with only an INFO log line to show for it.

The fix is the rule the request handlers already follow: doc_numbers.allocate,
keyed on the tenant being ticked.

Run:  python backend/test_sim_numbers_never_reused.py     (exit 0 = pass)
"""
import random

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import tenancy
from database import Base
from factory_simulator import tick_operator, tick_quality


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def _factory(db, tenant):
    token = tenancy.set_current_tenant(tenant)
    try:
        m = models.Machine(name=f"{tenant}-M1", status="Running", utilization=80, tenant_code=tenant)
        db.add(m)
        db.commit()
        db.add(models.WorkOrder(work_order_no=f"{tenant}-WO-1", part_number="P", batch_number="B",
                                machine_id=m.id, target_quantity=1000, actual_quantity=0,
                                status="In Progress", tenant_code=tenant))
        db.commit()
    finally:
        tenancy.reset_current_tenant(token)


def _as(tenant, fn, db):
    token = tenancy.set_current_tenant(tenant)
    try:
        return fn(db)
    finally:
        tenancy.reset_current_tenant(token)


def _numbers(db, model, column, tenant):
    token = tenancy.set_current_tenant(tenant)
    try:
        return sorted(getattr(r, column) for r in db.query(model).all())
    finally:
        tenancy.reset_current_tenant(token)


def test_a_deleted_inspection_does_not_jam_the_quality_tick():
    random.seed(7)
    db = _db()
    _factory(db, "SIM_A")
    _as("SIM_A", tick_quality, db)
    _as("SIM_A", tick_quality, db)
    assert _numbers(db, models.QualityInspection, "inspection_no", "SIM_A") == ["QI-7001", "QI-7002"]

    # An Admin deletes the first inspection (DELETE /quality/inspections/{id}).
    first = db.query(models.QualityInspection).filter(models.QualityInspection.inspection_no == "QI-7001").one()
    db.delete(first)
    db.commit()

    _as("SIM_A", tick_quality, db)          # used to raise IntegrityError: QI-7002 already exists
    live = _numbers(db, models.QualityInspection, "inspection_no", "SIM_A")
    assert live == ["QI-7002", "QI-7003"], live
    print("PASS after a deleted inspection the next tick issues QI-7003 instead of failing")


def test_a_deleted_job_does_not_jam_the_operator_tick():
    random.seed(11)
    db = _db()
    _factory(db, "SIM_A")

    def new_job(db):
        # tick_operator only starts a job when none is In Progress.
        for j in db.query(models.OperatorJobExecution).all():
            j.job_status = "Completed"
        db.commit()
        tick_operator(db)

    _as("SIM_A", new_job, db)
    _as("SIM_A", new_job, db)
    assert _numbers(db, models.OperatorJobExecution, "execution_no", "SIM_A") == ["EXE-9001", "EXE-9002"]

    first = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.execution_no == "EXE-9001").one()
    db.delete(first)
    db.commit()

    _as("SIM_A", new_job, db)
    live = _numbers(db, models.OperatorJobExecution, "execution_no", "SIM_A")
    assert live == ["EXE-9002", "EXE-9003"], live
    print("PASS after a deleted job the next tick issues EXE-9003 instead of failing")


def test_each_simulated_tenant_keeps_its_own_series():
    random.seed(3)
    db = _db()
    _factory(db, "SIM_A")
    _factory(db, "SIM_B")
    for _ in range(3):
        _as("SIM_A", tick_quality, db)
    _as("SIM_B", tick_quality, db)
    assert _numbers(db, models.QualityInspection, "inspection_no", "SIM_B") == ["QI-7001"]
    _as("SIM_A", tick_quality, db)
    assert _numbers(db, models.QualityInspection, "inspection_no", "SIM_A")[-1] == "QI-7004"
    print("PASS SIM_B starts at QI-7001 while SIM_A continues to QI-7004")


def test_the_series_continues_above_seeded_numbers():
    random.seed(5)
    db = _db()
    _factory(db, "SIM_A")
    token = tenancy.set_current_tenant("SIM_A")
    try:
        wo = db.query(models.WorkOrder).one()
        # The seeders number QI-{7000 + i}; a gap sits below the highest.
        for no in ("QI-7000", "QI-7004"):
            db.add(models.QualityInspection(inspection_no=no, work_order_id=wo.id, machine_id=wo.machine_id,
                                            inspector="QA", inspected_quantity=10, passed_quantity=10,
                                            failed_quantity=0, status="Passed", tenant_code="SIM_A"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(token)
    _as("SIM_A", tick_quality, db)
    assert _numbers(db, models.QualityInspection, "inspection_no", "SIM_A")[-1] == "QI-7005"
    print("PASS the first live inspection continues above the seeded QI-7004")


if __name__ == "__main__":
    test_a_deleted_inspection_does_not_jam_the_quality_tick()
    test_a_deleted_job_does_not_jam_the_operator_tick()
    test_each_simulated_tenant_keeps_its_own_series()
    test_the_series_continues_above_seeded_numbers()
    print("ALL SIMULATOR NUMBER TESTS PASSED")
