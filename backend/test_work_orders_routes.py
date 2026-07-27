"""Work-orders route registration + input-guard tests (ADR-0009).

The production-order lifecycle (list / create / update / delete) lives in
work_orders_routes.register(app), peeled out of main.py. Guards registration +
sole ownership, plus the non-negative-quantity boundary guard that keeps a
physically-impossible work order out of the read-models it feeds (parity with
the production-record / quality / order-PO / production-plan ingests).
Completing a work order still publishes ProductionCompleted (the BOM movement
is a subscriber) — that wiring is covered by the event tests; here we only
assert the routes moved and are owned by the module.

Run:  python backend/test_work_orders_routes.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
import schemas
import tenancy as T
import work_orders_routes as WO
from database import Base

EXPECTED = {"/work-orders", "/work-orders/{work_order_id}"}


def test_work_orders_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"work-orders paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"work_orders_routes"}}
    assert not wrong, f"work-orders paths not owned solely by work_orders_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} work-orders paths owned by work_orders_routes")


def test_completing_wo_still_publishes_production_completed():
    # Guard the event coupling survived the move: the module imports the event
    # symbol and references it in the update handler's source.
    import inspect
    import work_orders_routes
    src = inspect.getsource(work_orders_routes)
    assert "ProductionCompleted(" in src, "ProductionCompleted publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS work-orders completion still publishes ProductionCompleted")


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_machine(db, tenant):
    """A machine the work order can reference (create checks it exists)."""
    tok = T.set_current_tenant(tenant)
    try:
        machine = models.Machine(name="CNC-1", status="Running", utilization=80, downtime="0 min")
        db.add(machine)
        db.commit()
        db.refresh(machine)
    finally:
        T.reset_current_tenant(tok)
    return machine


def _wo(no, machine_id, target, actual):
    return schemas.WorkOrderCreate(
        work_order_no=no, part_number="P-1", batch_number="B-1",
        machine_id=machine_id, target_quantity=target, actual_quantity=actual,
        status="Planned",
    )


def _create_as(db, tenant, payload):
    tok = T.set_current_tenant(tenant)
    try:
        return WO.create_work_order(payload, db=db, current_user={"tenant": tenant})
    finally:
        T.reset_current_tenant(tok)


def test_create_work_order_happy_path_and_over_production_allowed():
    # A clean row persists. actual > target is INTENTIONALLY allowed (a run can
    # over-produce its order → achievement > 100%), so the guard must not reject it.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    ok = _create_as(db, "TA", _wo("WO-1", machine.id, 100, 0))
    assert ok.work_order_no == "WO-1" and ok.actual_quantity == 0, ok.work_order_no
    over = _create_as(db, "TA", _wo("WO-OVER", machine.id, 100, 150))
    assert over.actual_quantity == 150, over.actual_quantity  # over-production allowed
    tok = T.set_current_tenant("TA")
    assert {w.work_order_no for w in db.query(models.WorkOrder).all()} == {"WO-1", "WO-OVER"}
    T.reset_current_tenant(tok)
    print("PASS create work order: clean row persists, actual > target allowed (over-production)")


def test_create_work_order_rejects_negative_quantities():
    # Parity with the production-record (#266), quality (#324), order/PO (#346) and
    # production-plan (#351) ingests: target_quantity / actual_quantity carry no ge=0,
    # so a negative slips past validation and corrupts the work-order read-models
    # (achievement = actual/target, predictive pressure = target - actual) and — via a
    # completing WO — the inventory BOM movement. Both a negative target and a negative
    # actual must be a clean 400, and nothing may persist.
    db = _iso_session()
    machine = _seed_machine(db, "TA")

    try:
        _create_as(db, "TA", _wo("WO-N1", machine.id, 100, -5))
        assert False, "negative actual_quantity should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    try:
        _create_as(db, "TA", _wo("WO-N2", machine.id, -100, 0))
        assert False, "negative target_quantity should raise"
    except HTTPException as e:
        assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)

    ok = _create_as(db, "TA", _wo("WO-N3", machine.id, 100, 20))
    tok = T.set_current_tenant("TA")
    assert {w.work_order_no for w in db.query(models.WorkOrder).all()} == {"WO-N3"}, "rejected rows must not persist"
    T.reset_current_tenant(tok)
    assert ok.work_order_no == "WO-N3"
    print("PASS create work order rejects negative target/actual quantities -> 400, nothing persisted")


def test_update_work_order_rejects_negative_actual():
    # A negative actual_quantity PATCH is physically impossible and would corrupt the
    # read-models exactly as on create — a clean 400 checked BEFORE the value is
    # applied, so the stored row is untouched (not left negative, status intact).
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-U1", machine.id, 100, 10))
    tok = T.set_current_tenant("TA")
    try:
        try:
            WO.update_work_order(
                wo.id, schemas.WorkOrderUpdate(actual_quantity=-5),
                db=db, current_user={"tenant": "TA"})
            assert False, "negative actual PATCH should raise"
        except HTTPException as e:
            assert e.status_code == 400 and "non-negative" in e.detail, (e.status_code, e.detail)
        db.expire_all()
        row = db.query(models.WorkOrder).filter(models.WorkOrder.id == wo.id).first()
        assert row.actual_quantity == 10, row.actual_quantity   # unchanged, not negative
        assert row.status != "Completed", row.status
    finally:
        T.reset_current_tenant(tok)
    print("PASS update work order rejects a negative actual PATCH -> 400, row unchanged")


def test_update_work_order_completes_on_reaching_target():
    # The normal transition still fires: a non-negative actual that reaches target
    # flips status to Completed (guard runs, value applies). part_number "P-1" is not
    # in the BOM, so the ProductionCompleted publish is a safe no-op here.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-U2", machine.id, 100, 0))
    tok = T.set_current_tenant("TA")
    try:
        updated = WO.update_work_order(
            wo.id, schemas.WorkOrderUpdate(actual_quantity=100),
            db=db, current_user={"tenant": "TA"})
        assert updated.actual_quantity == 100, updated.actual_quantity
        assert updated.status == "Completed", updated.status
    finally:
        T.reset_current_tenant(tok)
    print("PASS update work order: actual reaching target -> Completed (value applied)")


if __name__ == "__main__":
    test_work_orders_paths_owned_by_module()
    test_completing_wo_still_publishes_production_completed()
    test_create_work_order_happy_path_and_over_production_allowed()
    test_create_work_order_rejects_negative_quantities()
    test_update_work_order_rejects_negative_actual()
    test_update_work_order_completes_on_reaching_target()
    print("ALL WORK-ORDERS ROUTE TESTS PASSED")
