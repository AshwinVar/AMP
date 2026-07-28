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


class _CapturingBus:
    """A stand-in event bus that records what the handler published, so a test can
    assert the QUANTITY the completion decided without wiring real subscribers."""

    def __init__(self):
        self.published = []

    def publish(self, event, db=None):
        self.published.append(event)


def _complete_via_status_capturing(db, tenant, work_order_id):
    """PATCH status->Completed and capture the published ProductionCompleted."""
    bus = _CapturingBus()
    original = WO.event_bus
    WO.event_bus = bus
    tok = T.set_current_tenant(tenant)
    try:
        updated = WO.update_work_order(
            work_order_id, schemas.WorkOrderUpdate(status="Completed"),
            db=db, current_user={"tenant": tenant})
    finally:
        T.reset_current_tenant(tok)
        WO.event_bus = original
    return updated, bus.published


def test_status_only_completion_with_zero_actual_publishes_zero_not_target():
    # The falsy-zero bug: a work order marked Completed via a status-only PATCH,
    # with nothing produced yet (actual_quantity defaults to 0), must publish
    # ProductionCompleted with quantity 0 — the recorded actual — NOT the order's
    # target. `actual_quantity or target_quantity` treated a real 0 as "missing"
    # and fell through to target, so the BOM subscriber moved the whole order's
    # material as if every unit had been made. Independently derived: target 100,
    # actual 0 -> honest completed quantity is 0, not 100.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-Z1", machine.id, 100, 0))
    updated, published = _complete_via_status_capturing(db, "TA", wo.id)
    assert updated.status == "Completed", updated.status
    assert len(published) == 1, published
    assert published[0].quantity == 0, published[0].quantity   # actual (0), not target (100)
    print("PASS status-only completion with actual 0 publishes quantity 0 (not the target)")


def test_zero_actual_completion_does_not_fabricate_bom_movement():
    # The end-to-end consequence through the real BOM subscriber: a completion
    # with actual 0 must move ZERO bill-of-materials, not the full target's worth.
    # SHAFT-001 recipe: 2x RM-STEEL-001 -> FG-SHAFT-001 (bom.PART_BOM). Numbers
    # derived independently: target 100 but actual 0 -> honest movement is 0, so
    # raw 100 stays 100 and finished 5 stays 5; the bug would have consumed
    # 100*2 = 200 raw (clamped to the 100 on hand) and received 100 finished.
    import subscribers
    from events import EventBus
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    tok = T.set_current_tenant("TA")
    try:
        db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                    category="Raw", unit="kg", current_stock=100, reorder_level=10))
        db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                    category="Finished", unit="pcs", current_stock=5, reorder_level=0))
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-Z2", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=100, actual_quantity=0, status="Planned"))
    # Route the completion through an isolated bus carrying only the BOM subscriber,
    # so the assertion is about THIS handler's quantity decision, not other listeners.
    bus = EventBus()
    subscribers.register(bus)
    original = WO.event_bus
    WO.event_bus = bus
    tok = T.set_current_tenant("TA")
    try:
        updated = WO.update_work_order(
            wo.id, schemas.WorkOrderUpdate(status="Completed"),
            db=db, current_user={"tenant": "TA"})
        db.commit()
    finally:
        T.reset_current_tenant(tok)
        WO.event_bus = original
    assert updated.status == "Completed", updated.status
    tok = T.set_current_tenant("TA")
    try:
        raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
        fg = db.query(models.InventoryItem).filter_by(item_code="FG-SHAFT-001").first()
        issues = db.query(models.InventoryTransaction).filter_by(transaction_type="Issue").all()
    finally:
        T.reset_current_tenant(tok)
    assert raw.current_stock == 100, raw.current_stock   # nothing produced -> nothing consumed
    assert fg.current_stock == 5, fg.current_stock       # nothing produced -> nothing received
    assert all(t.quantity == 0 for t in issues), [t.quantity for t in issues]
    print("PASS zero-actual completion moves 0 BOM (no fabricated full-target consumption)")


def test_null_actual_completion_falls_back_to_target():
    # The ONE legitimate fallback is preserved: a raw NULL actual_quantity (a
    # raw-SQL / migration / cleared-field write, distinct from a recorded 0) has no
    # output recorded at all, so completing it still falls back to the order's
    # target quantity. Force a genuine NULL past the ORM's default(0), then complete
    # via status and assert the published quantity is the target (100), not 0.
    from sqlalchemy import text
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-N0", machine.id, 100, 0))
    db.execute(text("UPDATE work_orders SET actual_quantity=NULL WHERE id=:i"), {"i": wo.id})
    db.commit()
    db.expire_all()
    updated, published = _complete_via_status_capturing(db, "TA", wo.id)
    assert updated.status == "Completed", updated.status
    assert len(published) == 1, published
    assert published[0].quantity == 100, published[0].quantity   # NULL -> target fallback
    print("PASS NULL actual_quantity still falls back to the target on completion")


def test_completion_reaching_target_publishes_actual_quantity():
    # Regression: the normal path is unchanged. Logging an actual that reaches the
    # target flips status to Completed and publishes that ACTUAL quantity (120),
    # not the target — over-production still moves the produced amount.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-T1", machine.id, 100, 0))
    bus = _CapturingBus()
    original = WO.event_bus
    WO.event_bus = bus
    tok = T.set_current_tenant("TA")
    try:
        updated = WO.update_work_order(
            wo.id, schemas.WorkOrderUpdate(actual_quantity=120),
            db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)
        WO.event_bus = original
    assert updated.status == "Completed", updated.status
    assert len(bus.published) == 1, bus.published
    assert bus.published[0].quantity == 120, bus.published[0].quantity
    print("PASS completion via reaching target publishes the actual produced quantity")


if __name__ == "__main__":
    test_work_orders_paths_owned_by_module()
    test_completing_wo_still_publishes_production_completed()
    test_create_work_order_happy_path_and_over_production_allowed()
    test_create_work_order_rejects_negative_quantities()
    test_update_work_order_rejects_negative_actual()
    test_update_work_order_completes_on_reaching_target()
    test_status_only_completion_with_zero_actual_publishes_zero_not_target()
    test_zero_actual_completion_does_not_fabricate_bom_movement()
    test_null_actual_completion_falls_back_to_target()
    test_completion_reaching_target_publishes_actual_quantity()
    print("ALL WORK-ORDERS ROUTE TESTS PASSED")
