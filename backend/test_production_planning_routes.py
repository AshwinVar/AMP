"""Production-planning route registration + input-guard tests (ADR-0009).

Production plans + schedules (plain CRUD) live in
production_planning_routes.register(app), peeled out of main.py. Guards
registration + sole ownership by the module, plus the non-negative-quantity
boundary guard that keeps a physically-impossible plan out of the attainment
read-model (parity with the production-record / quality / order-PO ingests).

Run:  python backend/test_production_planning_routes.py     (exit 0 = pass)
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import schemas as _schemas_mod  # noqa: F401  (response-model heal lives in schemas)

import main
import models
import production_planning_routes as PP
import schemas
import tenancy as T
from database import Base

EXPECTED = {
    "/production-plans",
    "/production-plans/{plan_id}",
    "/production-schedules",
    "/production-schedules/{schedule_id}",
}


def test_planning_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"planning paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"production_planning_routes"}}
    assert not wrong, f"planning paths not owned solely by production_planning_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} planning paths owned by production_planning_routes")


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_machine_and_wo(db, tenant):
    """A machine + work order the plan can reference (the create-time existence
    checks require both to exist for the caller's tenant)."""
    tok = T.set_current_tenant(tenant)
    try:
        machine = models.Machine(name="CNC-1", status="Running", utilization=80, downtime="0 min")
        db.add(machine)
        db.commit()
        db.refresh(machine)
        wo = models.WorkOrder(
            work_order_no="WO-1", part_number="P-1", batch_number="B-1",
            machine_id=machine.id, target_quantity=100, actual_quantity=0,
            status="In Progress", material_state="RAW",
        )
        db.add(wo)
        db.commit()
        db.refresh(wo)
    finally:
        T.reset_current_tenant(tok)
    return machine, wo


def _today():
    """The same "today" the read-models use.

    ai/schedule.py windows plans against ``datetime.utcnow().date()`` and asks
    ``p.plan_date == today``. Seeding from the LOCAL ``date.today()`` makes the
    row a FUTURE plan for any timezone ahead of UTC in the hours after local
    midnight, so it drops out of the window entirely. Same trap that broke the
    overdue reconciliation in test_orders_routes.py.
    """
    return datetime.utcnow().date()


def _plan(no, machine_id, wo_id, planned, actual):
    return schemas.ProductionPlanCreate(
        plan_no=no, work_order_id=wo_id, machine_id=machine_id,
        planned_quantity=planned, actual_quantity=actual,
        plan_date=_today(), shift_name="A",
    )


def _create_as(db, tenant, payload):
    tok = T.set_current_tenant(tenant)
    try:
        return PP.create_production_plan(payload, db=db, current_user={"tenant": tenant})
    finally:
        T.reset_current_tenant(tok)


def test_create_production_plan_happy_path_and_over_attainment_allowed():
    # A clean plan persists. And actual > planned is INTENTIONALLY allowed (a shift
    # can over-produce its plan → attainment > 100%), unlike an order that can't
    # dispatch more than ordered — so the guard must NOT reject it.
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")
    ok = _create_as(db, "TA", _plan("PLN-1", machine.id, wo.id, 100, 0))
    assert ok.plan_no == "PLN-1" and ok.actual_quantity == 0, ok.plan_no
    over = _create_as(db, "TA", _plan("PLN-OVER", machine.id, wo.id, 100, 150))
    assert over.actual_quantity == 150, over.actual_quantity  # over-production allowed
    tok = T.set_current_tenant("TA")
    assert {p.plan_no for p in db.query(models.ProductionPlan).all()} == {"PLN-1", "PLN-OVER"}
    T.reset_current_tenant(tok)
    print("PASS create production plan: clean row persists, actual > planned allowed (over-attainment)")


def test_create_production_plan_rejects_negative_quantities():
    # Parity with the production-record (#266), quality (#324) and order/PO ingests:
    # planned_quantity / actual_quantity are plain ints (no ge=0), so a negative
    # slips past validation and corrupts the attainment read-model (actual/planned).
    # Both a negative actual and a negative planned must be a clean 400, and nothing
    # may persist.
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")

    try:
        _create_as(db, "TA", _plan("PLN-N1", machine.id, wo.id, 100, -5))
        assert False, "negative actual_quantity should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    try:
        _create_as(db, "TA", _plan("PLN-N2", machine.id, wo.id, -100, 0))
        assert False, "negative planned_quantity should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    # A clean plan still works after the rejections, and only it persisted.
    ok = _create_as(db, "TA", _plan("PLN-N3", machine.id, wo.id, 100, 20))
    tok = T.set_current_tenant("TA")
    assert {p.plan_no for p in db.query(models.ProductionPlan).all()} == {"PLN-N3"}, "rejected rows must not persist"
    T.reset_current_tenant(tok)
    assert ok.plan_no == "PLN-N3"
    print("PASS create production plan rejects negative planned/actual quantities -> 400, nothing persisted")


def test_update_production_plan_rejects_negative_actual():
    # A negative actual_quantity PATCH is physically impossible and would corrupt the
    # attainment window exactly as on create — a clean 400 checked BEFORE the value
    # is applied, so the stored row is untouched (not left negative, status intact).
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")
    plan = _create_as(db, "TA", _plan("PLN-U1", machine.id, wo.id, 100, 10))
    tok = T.set_current_tenant("TA")
    try:
        try:
            PP.update_production_plan(
                plan.id, schemas.ProductionPlanUpdate(actual_quantity=-5),
                db=db, current_user={"tenant": "TA"})
            assert False, "negative actual PATCH should raise"
        except HTTPException as e:
            assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)
        db.expire_all()
        row = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan.id).first()
        assert row.actual_quantity == 10, row.actual_quantity   # unchanged, not negative
        assert row.status != "Completed", row.status
    finally:
        T.reset_current_tenant(tok)
    print("PASS update production plan rejects a negative actual PATCH -> 400, row unchanged")


def test_update_production_plan_completes_on_reaching_plan():
    # The normal transition still fires: a non-negative actual that reaches planned
    # flips status to Completed (guard runs, value applies).
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")
    plan = _create_as(db, "TA", _plan("PLN-U2", machine.id, wo.id, 100, 0))
    tok = T.set_current_tenant("TA")
    try:
        updated = PP.update_production_plan(
            plan.id, schemas.ProductionPlanUpdate(actual_quantity=100),
            db=db, current_user={"tenant": "TA"})
        assert updated.actual_quantity == 100, updated.actual_quantity
        assert updated.status == "Completed", updated.status
    finally:
        T.reset_current_tenant(tok)
    print("PASS update production plan: actual reaching planned -> Completed (value applied)")


def _null_out_actual(db, plan_id):
    """Force a legacy/raw-SQL NULL into the nullable-default-0 actual_quantity
    column — the exact state the ORM default never fills (it only covers an
    inserter that OMITS the field) but a migration or a cleared write can leave."""
    db.execute(
        text("UPDATE production_plans SET actual_quantity = NULL WHERE id = :id"),
        {"id": plan_id},
    )
    db.commit()
    db.expire_all()


def test_get_production_plans_survives_a_null_actual_quantity_row():
    # A single row with actual_quantity = NULL used to 500 the WHOLE list:
    # ProductionPlanResponse types actual_quantity as a non-optional int, so the
    # NULL raised ValidationError during response serialisation and hid every good
    # row behind it. The response model now coalesces a NULL to the column's own
    # default of 0 (honest: NULL = "no value recorded", declared default is 0).
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")
    good = _create_as(db, "TA", _plan("PLN-OK", machine.id, wo.id, 100, 42))
    legacy = _create_as(db, "TA", _plan("PLN-NULL", machine.id, wo.id, 100, 0))
    _null_out_actual(db, legacy.id)

    tok = T.set_current_tenant("TA")
    try:
        rows = PP.get_production_plans(db=db, current_user={"tenant": "TA"})
        # Serialise EXACTLY as FastAPI does (response_model + from_attributes); this
        # is the step that used to raise on the NULL row.
        serialised = {
            r.plan_no: schemas.ProductionPlanResponse.model_validate(r)
            for r in rows
        }
    finally:
        T.reset_current_tenant(tok)

    assert set(serialised) == {"PLN-OK", "PLN-NULL"}, set(serialised)
    # The NULL row heals to 0, the real value is preserved untouched.
    assert serialised["PLN-NULL"].actual_quantity == 0, serialised["PLN-NULL"].actual_quantity
    assert serialised["PLN-OK"].actual_quantity == 42, serialised["PLN-OK"].actual_quantity
    print("PASS GET /production-plans survives a NULL actual_quantity row (heals to 0, real value kept)")


def test_status_only_patch_on_a_null_row_does_not_500():
    # A status-only PATCH never touches actual_quantity (the actual_quantity block
    # is skipped), so on a legacy NULL row it returned the row unchanged and the
    # response serialisation 500ed. The heal keeps the status transition working
    # and returns actual_quantity as 0.
    db = _iso_session()
    machine, wo = _seed_machine_and_wo(db, "TA")
    plan = _create_as(db, "TA", _plan("PLN-SP", machine.id, wo.id, 100, 0))
    _null_out_actual(db, plan.id)

    tok = T.set_current_tenant("TA")
    try:
        updated = PP.update_production_plan(
            plan.id, schemas.ProductionPlanUpdate(status="On Hold"),
            db=db, current_user={"tenant": "TA"})
        serialised = schemas.ProductionPlanResponse.model_validate(updated)
    finally:
        T.reset_current_tenant(tok)

    assert serialised.status == "On Hold", serialised.status
    assert serialised.actual_quantity == 0, serialised.actual_quantity
    print("PASS status-only PATCH on a NULL actual_quantity row succeeds (heals to 0)")


def _schedule(no, machine_id, planned, minutes):
    return schemas.ProductionScheduleCreate(
        schedule_no=no, machine_id=machine_id, shift_name="A",
        scheduled_date=_today(), planned_quantity=planned, estimated_minutes=minutes,
    )


def _create_schedule_as(db, tenant, payload):
    tok = T.set_current_tenant(tenant)
    try:
        return PP.create_production_schedule(payload, db=db, current_user={"tenant": tenant})
    finally:
        T.reset_current_tenant(tok)


def test_create_production_schedule_happy_path():
    # A clean schedule persists with both quantities intact.
    db = _iso_session()
    machine, _ = _seed_machine_and_wo(db, "TA")
    ok = _create_schedule_as(db, "TA", _schedule("SCH-1", machine.id, 200, 480))
    assert ok.schedule_no == "SCH-1", ok.schedule_no
    assert ok.planned_quantity == 200 and ok.estimated_minutes == 480, (ok.planned_quantity, ok.estimated_minutes)
    print("PASS create production schedule: clean row persists with both quantities")


def test_create_production_schedule_rejects_negative_quantities():
    # The one counted ingest that was missing the boundary guard. planned_quantity /
    # estimated_minutes are plain ints (no ge=0), so a negative slips past validation
    # and corrupts the forward schedule-load board (ai/schedule_load.py sums
    # estimated_minutes) and the /analytics/production-schedules SQL rollup. A
    # negative in EITHER field must be a clean 400, and nothing may persist.
    db = _iso_session()
    machine, _ = _seed_machine_and_wo(db, "TA")

    try:
        _create_schedule_as(db, "TA", _schedule("SCH-N1", machine.id, 200, -60))
        assert False, "negative estimated_minutes should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    try:
        _create_schedule_as(db, "TA", _schedule("SCH-N2", machine.id, -200, 480))
        assert False, "negative planned_quantity should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    # A clean schedule still works after the rejections, and only it persisted.
    ok = _create_schedule_as(db, "TA", _schedule("SCH-N3", machine.id, 200, 480))
    tok = T.set_current_tenant("TA")
    assert {s.schedule_no for s in db.query(models.ProductionSchedule).all()} == {"SCH-N3"}, "rejected rows must not persist"
    T.reset_current_tenant(tok)
    assert ok.schedule_no == "SCH-N3"
    print("PASS create production schedule rejects negative planned/estimated -> 400, nothing persisted")


def test_update_production_schedule_rejects_negative_quantities():
    # A negative planned_quantity / estimated_minutes PATCH is physically impossible
    # and would corrupt the schedule-load board exactly as on create — a clean 400
    # checked BEFORE the value is applied, so the stored row is untouched.
    db = _iso_session()
    machine, _ = _seed_machine_and_wo(db, "TA")
    sch = _create_schedule_as(db, "TA", _schedule("SCH-U1", machine.id, 200, 480))
    tok = T.set_current_tenant("TA")
    try:
        for bad in (schemas.ProductionScheduleUpdate(estimated_minutes=-30),
                    schemas.ProductionScheduleUpdate(planned_quantity=-10)):
            try:
                PP.update_production_schedule(sch.id, bad, db=db, current_user={"tenant": "TA"})
                assert False, "negative PATCH should raise"
            except HTTPException as e:
                assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)
        db.expire_all()
        row = db.query(models.ProductionSchedule).filter(models.ProductionSchedule.id == sch.id).first()
        # unchanged, not negative
        assert row.planned_quantity == 200 and row.estimated_minutes == 480, (row.planned_quantity, row.estimated_minutes)
    finally:
        T.reset_current_tenant(tok)
    print("PASS update production schedule rejects a negative PATCH -> 400, row unchanged")


def test_update_production_schedule_applies_valid_change():
    # The normal transition still fires: a non-negative PATCH applies as before.
    db = _iso_session()
    machine, _ = _seed_machine_and_wo(db, "TA")
    sch = _create_schedule_as(db, "TA", _schedule("SCH-U2", machine.id, 200, 480))
    tok = T.set_current_tenant("TA")
    try:
        updated = PP.update_production_schedule(
            sch.id, schemas.ProductionScheduleUpdate(estimated_minutes=360, status="In Progress"),
            db=db, current_user={"tenant": "TA"})
        assert updated.estimated_minutes == 360, updated.estimated_minutes
        assert updated.status == "In Progress", updated.status
    finally:
        T.reset_current_tenant(tok)
    print("PASS update production schedule: valid change applied")


if __name__ == "__main__":
    test_planning_paths_owned_by_module()
    test_create_production_plan_happy_path_and_over_attainment_allowed()
    test_create_production_plan_rejects_negative_quantities()
    test_update_production_plan_rejects_negative_actual()
    test_update_production_plan_completes_on_reaching_plan()
    test_get_production_plans_survives_a_null_actual_quantity_row()
    test_status_only_patch_on_a_null_row_does_not_500()
    test_create_production_schedule_happy_path()
    test_create_production_schedule_rejects_negative_quantities()
    test_update_production_schedule_rejects_negative_quantities()
    test_update_production_schedule_applies_valid_change()
    print("ALL PRODUCTION-PLANNING ROUTE TESTS PASSED")
