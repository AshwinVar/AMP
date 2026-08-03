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
from sqlalchemy import create_engine, text
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


def _null_out_actual(db, work_order_id):
    """Force a legacy/raw-SQL NULL into the nullable-default-0 actual_quantity
    column — the exact state the ORM default never fills (it only covers an
    inserter that OMITS the field) but a migration or a cleared write can leave."""
    from sqlalchemy import text
    db.execute(
        text("UPDATE work_orders SET actual_quantity = NULL WHERE id = :id"),
        {"id": work_order_id},
    )
    db.commit()
    db.expire_all()


def test_get_work_orders_survives_a_null_actual_quantity_row():
    # A single row with actual_quantity = NULL used to 500 the WHOLE list:
    # WorkOrderResponse types actual_quantity as a non-optional int, so the NULL
    # raised ValidationError during response serialisation and hid every good row
    # behind it — the exact class already healed for GET /production-plans and the
    # inventory / orders / quality lists. The response model now coalesces a NULL to
    # the column's own default of 0 (honest: NULL = "no value recorded", declared
    # default is 0), while a real value is preserved untouched — here the real
    # target_quantity of 100 passes through the heal unchanged.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    _create_as(db, "TA", _wo("WO-OK", machine.id, 100, 42))
    legacy = _create_as(db, "TA", _wo("WO-NULL", machine.id, 100, 0))
    _null_out_actual(db, legacy.id)

    tok = T.set_current_tenant("TA")
    try:
        rows = WO.get_work_orders(db=db, current_user={"tenant": "TA"})
        # Serialise EXACTLY as FastAPI does (response_model + from_attributes); this
        # is the step that used to raise on the NULL row.
        serialised = {
            r.work_order_no: schemas.WorkOrderResponse.model_validate(r)
            for r in rows
        }
    finally:
        T.reset_current_tenant(tok)

    assert set(serialised) == {"WO-OK", "WO-NULL"}, set(serialised)
    # The NULL row heals to 0; the real value is preserved; target is untouched.
    assert serialised["WO-NULL"].actual_quantity == 0, serialised["WO-NULL"].actual_quantity
    assert serialised["WO-NULL"].target_quantity == 100, serialised["WO-NULL"].target_quantity
    assert serialised["WO-OK"].actual_quantity == 42, serialised["WO-OK"].actual_quantity
    print("PASS GET /work-orders survives a NULL actual_quantity row (heals to 0, real value kept)")


def test_status_only_patch_on_a_null_actual_row_serialises():
    # A status-only PATCH never touches actual_quantity (the actual_quantity block
    # is skipped), so on a legacy NULL row it returned the row unchanged and the
    # response serialisation 500ed. The heal keeps the status transition working and
    # returns actual_quantity as 0. A recorded 0 is real; the NULL row is distinct
    # only in that its ORM attribute is None until the response model heals it.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-SP", machine.id, 100, 0))
    _null_out_actual(db, wo.id)

    tok = T.set_current_tenant("TA")
    try:
        updated = WO.update_work_order(
            wo.id, schemas.WorkOrderUpdate(status="On Hold"),
            db=db, current_user={"tenant": "TA"})
        serialised = schemas.WorkOrderResponse.model_validate(updated)
    finally:
        T.reset_current_tenant(tok)

    assert serialised.status == "On Hold", serialised.status
    assert serialised.actual_quantity == 0, serialised.actual_quantity
    assert serialised.target_quantity == 100, serialised.target_quantity
    print("PASS status-only PATCH on a NULL actual_quantity work order serialises (heals to 0)")


def test_work_order_response_heals_only_null_not_a_real_zero():
    # Pin the heal's boundary: a genuinely recorded actual_quantity of 0 (a WO with
    # no output yet) is untouched — it is ALREADY 0 — and a real positive value is
    # passed through verbatim. The heal fires only for a NULL, never rewriting real
    # data. Independently derived: (target 50, actual 0) -> 0; (target 50, actual 50) -> 50.
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    zero = _create_as(db, "TA", _wo("WO-REAL0", machine.id, 50, 0))
    full = _create_as(db, "TA", _wo("WO-REAL50", machine.id, 50, 50))
    assert schemas.WorkOrderResponse.model_validate(zero).actual_quantity == 0
    assert schemas.WorkOrderResponse.model_validate(full).actual_quantity == 50
    print("PASS WorkOrderResponse heals only NULL; a real 0 and a real value pass through unchanged")


def test_update_work_order_survives_a_null_target_quantity_row():
    # The PATCH auto-complete crash: `actual_quantity >= target_quantity` on a legacy
    # NULL target_quantity ran `int >= None` -> TypeError -> unhandled 500, exactly
    # the class the sibling production_planning PATCH already guards
    # (production_planning_routes.py:104). Logging an actual against such a row must
    # NOT 500 — and, with no known target, must NOT fabricate a "Completed" (we
    # cannot say the order is done): the status stays "Planned" and the value applies.
    #
    # SQLite enforces the nullable=False NOT NULL a pre-existing row predates, so a
    # real on-disk NULL is impossible here; set_committed_value reproduces the exact
    # attribute state a loaded legacy-NULL row presents (target_quantity is None when
    # read) WITHOUT marking the instance dirty, so the handler's own query and commit
    # never try to persist the NULL — the same "reproduce the loaded state, don't
    # write it" approach test_production_planning_null_safe takes for planned_quantity.
    from sqlalchemy.orm.attributes import set_committed_value
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    wo = _create_as(db, "TA", _wo("WO-NT", machine.id, 100, 0))

    tok = T.set_current_tenant("TA")
    try:
        row = db.query(models.WorkOrder).filter(models.WorkOrder.id == wo.id).first()
        set_committed_value(row, "target_quantity", None)   # loaded legacy-NULL state
        updated = WO.update_work_order(
            wo.id, schemas.WorkOrderUpdate(actual_quantity=250),
            db=db, current_user={"tenant": "TA"})   # this line raised TypeError -> 500 before the guard
    finally:
        T.reset_current_tenant(tok)

    assert updated.actual_quantity == 250, updated.actual_quantity   # value applied
    assert updated.status == "Planned", updated.status               # unknown target -> not auto-completed
    print("PASS update work order survives a NULL target_quantity (no 500, no fabricated completion)")


def test_work_order_response_heals_null_target_quantity_not_500():
    # The GET-list twin of the actual_quantity heal: a single row with a NULL
    # target_quantity (nullable=False, but the constraint is not retro-applied to a
    # legacy / raw-SQL / migration row) used to 500 the WHOLE list — WorkOrderResponse
    # types target_quantity as a non-optional int, so the NULL raised ValidationError
    # during serialisation and hid every good order behind it (the exact class already
    # healed for GET /production-plans' nullable=False planned_quantity and the quality
    # list's nullable=False inspected_quantity #447). The response model now coalesces a
    # NULL target to the column's own "no value recorded" reading of 0, while a real
    # target is preserved untouched. Validated exactly as FastAPI serialises the
    # response_model (from_attributes); a SimpleNamespace reproduces the loaded-row
    # attribute state without needing an on-disk NULL SQLite would reject.
    from types import SimpleNamespace
    good = SimpleNamespace(
        id=1, work_order_no="WO-OK", part_number="P", batch_number="B", machine_id=1,
        target_quantity=100, actual_quantity=42, status="Running", material_state="RAW",
        planned_start=None, planned_end=None, created_at=None)
    legacy = SimpleNamespace(
        id=2, work_order_no="WO-NULLT", part_number="P", batch_number="B", machine_id=1,
        target_quantity=None, actual_quantity=None, status="Running", material_state="RAW",
        planned_start=None, planned_end=None, created_at=None)   # loaded legacy raw-SQL / migration row

    good_s = schemas.WorkOrderResponse.model_validate(good)     # must NOT raise
    bad_s = schemas.WorkOrderResponse.model_validate(legacy)    # was a 500

    # NULL -> 0 (a concrete int, not None); a real value passes through verbatim.
    assert bad_s.target_quantity == 0 and bad_s.actual_quantity == 0, (bad_s.target_quantity, bad_s.actual_quantity)
    assert isinstance(bad_s.target_quantity, int), type(bad_s.target_quantity)
    assert good_s.target_quantity == 100 and good_s.actual_quantity == 42, (good_s.target_quantity, good_s.actual_quantity)
    print("PASS WorkOrderResponse heals a NULL target_quantity to 0 (not a 500 that hides every order)")


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


def _bom_session():
    """A tenant with the SHAFT-001 recipe stocked: 2x RM-STEEL-001 -> FG-SHAFT-001."""
    db = _iso_session()
    machine = _seed_machine(db, "TA")
    tok = T.set_current_tenant("TA")
    try:
        db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                    category="Raw", unit="kg", current_stock=100, reorder_level=10))
        db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                    category="Finished", unit="pcs", current_stock=0, reorder_level=0))
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    return db, machine


def _patch_on_bom_bus(db, wo_id, **fields):
    """PATCH the work order with only the BOM subscriber listening."""
    import subscribers
    from events import EventBus
    bus = EventBus()
    subscribers.register(bus)
    original = WO.event_bus
    WO.event_bus = bus
    tok = T.set_current_tenant("TA")
    try:
        WO.update_work_order(wo_id, schemas.WorkOrderUpdate(**fields),
                             db=db, current_user={"tenant": "TA"})
        db.commit()
    finally:
        T.reset_current_tenant(tok)
        WO.event_bus = original


def _bom_state(db):
    tok = T.set_current_tenant("TA")
    try:
        db.expire_all()
        raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first().current_stock
        fg = db.query(models.InventoryItem).filter_by(item_code="FG-SHAFT-001").first().current_stock
        ledger = [(t.transaction_type, t.quantity) for t in
                  db.query(models.InventoryTransaction).order_by(models.InventoryTransaction.id).all()]
    finally:
        T.reset_current_tenant(tok)
    return raw, fg, ledger


def test_reopening_and_recompleting_does_not_move_the_bom_again():
    """THE BUG. The publish was gated on `prev_status != "Completed"`, but
    prev_status is read from the row at the top of the SAME request, so it only
    remembered one PATCH back. Reopening a finished order and finishing it again
    looked like a first completion.

    Measured before the fix, on a 10-unit SHAFT-001 order (2 kg steel per unit),
    driven entirely from the work-order table UI:

        PATCH actual_quantity=10       raw 100->80, fg 0->10   correct
        reopen (Running) -> Completed   raw 80->60,  fg 10->20
        reopen -> Completed again       raw 60->40,  fg 20->30

    40 kg written off and 30 units booked for 10 units of real production, and
    unbounded in the number of cycles. The event carries the CUMULATIVE
    actual_quantity and the subscriber applies it as an absolute move, so no
    delta semantics make a re-fire harmless.
    """
    db, machine = _bom_session()
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-RF", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=10, actual_quantity=0, status="Running"))

    _patch_on_bom_bus(db, wo.id, actual_quantity=10)          # completes on target
    assert _bom_state(db)[:2] == (80, 10), _bom_state(db)

    for _ in range(3):                                        # reopen / re-complete
        _patch_on_bom_bus(db, wo.id, status="Running")
        _patch_on_bom_bus(db, wo.id, status="Completed")

    raw, fg, ledger = _bom_state(db)
    assert (raw, fg) == (80, 10), (raw, fg)                   # NOT (40, 30)
    # One movement pair, so the ledger cannot corroborate a corrupted figure.
    assert ledger == [("Issue", 20), ("Receive", 10)], ledger
    print("PASS reopening and re-completing a work order moves the BOM only once")


def test_a_repeat_completed_patch_still_moves_nothing():
    """CONTROL, and it is what makes the test above meaningful.

    A repeat PATCH with status=Completed and NO intervening reopen never
    republished (prev_status was already "Completed"), and must still not. If this
    also changed, the test above could not tell the re-fire bug from a blanket
    "never publish twice" that had broken the first completion too.
    """
    db, machine = _bom_session()
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-RC", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=10, actual_quantity=0, status="Running"))
    _patch_on_bom_bus(db, wo.id, actual_quantity=10)
    before = _bom_state(db)
    _patch_on_bom_bus(db, wo.id, status="Completed")
    _patch_on_bom_bus(db, wo.id, status="Completed")
    assert _bom_state(db) == before, (before, _bom_state(db))
    print("PASS a repeat Completed PATCH with no reopen still moves nothing")


def test_completion_stamps_completed_at_once_and_never_rewrites_it():
    """The marker must be written on the first completion and then left alone —
    if a reopen cleared it, or a later PATCH refreshed it, the guard would fall
    open again."""
    db, machine = _bom_session()
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-TS", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=10, actual_quantity=0, status="Running"))
    tok = T.set_current_tenant("TA")
    try:
        assert db.query(models.WorkOrder).filter_by(id=wo.id).first().completed_at is None
    finally:
        T.reset_current_tenant(tok)

    _patch_on_bom_bus(db, wo.id, actual_quantity=10)
    tok = T.set_current_tenant("TA")
    try:
        first = db.query(models.WorkOrder).filter_by(id=wo.id).first().completed_at
    finally:
        T.reset_current_tenant(tok)
    assert first is not None, "first completion must stamp completed_at"

    _patch_on_bom_bus(db, wo.id, status="Running")
    _patch_on_bom_bus(db, wo.id, status="Completed")
    tok = T.set_current_tenant("TA")
    try:
        row = db.query(models.WorkOrder).filter_by(id=wo.id).first()
    finally:
        T.reset_current_tenant(tok)
    assert row.completed_at == first, (row.completed_at, first)   # survives a reopen
    print("PASS completed_at is stamped once and survives a reopen")


def test_an_order_created_already_completed_never_moves_the_bom():
    """Create does not publish ProductionCompleted, so an order posted directly as
    Completed has never moved its BOM — and must not start moving it on some later
    unrelated PATCH. That was the behaviour before this change (prev_status was
    already "Completed") and stamping completed_at on create preserves it."""
    db, machine = _bom_session()
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-BORN", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=10, actual_quantity=10, status="Completed"))
    assert _bom_state(db)[:2] == (100, 0), _bom_state(db)     # create moved nothing

    _patch_on_bom_bus(db, wo.id, status="Completed")
    _patch_on_bom_bus(db, wo.id, status="Running")
    _patch_on_bom_bus(db, wo.id, status="Completed")
    raw, fg, ledger = _bom_state(db)
    assert (raw, fg) == (100, 0), (raw, fg)
    assert ledger == [], ledger
    print("PASS an order created as Completed never moves the BOM on a later PATCH")


def test_the_backfill_stamps_existing_completed_orders():
    """Historic rows carry a NULL completed_at, so without the backfill the first
    PATCH that left such an order Completed would look like a first completion and
    move its BOM a second time — the bug, fired once per order at deploy."""
    db, machine = _bom_session()
    wo = _create_as(db, "TA", schemas.WorkOrderCreate(
        work_order_no="WO-OLD", part_number="SHAFT-001", batch_number="B-1",
        machine_id=machine.id, target_quantity=10, actual_quantity=10, status="Completed"))
    # Simulate a pre-migration row: Completed with no marker.
    tok = T.set_current_tenant("TA")
    try:
        db.execute(text("UPDATE work_orders SET completed_at = NULL WHERE id = :i"), {"i": wo.id})
        db.commit()
        db.expire_all()
        assert db.query(models.WorkOrder).filter_by(id=wo.id).first().completed_at is None
    finally:
        T.reset_current_tenant(tok)

    # Run the REAL migration helper, pointed at this test engine. Inlining a copy
    # of its UPDATE here would leave main._backfill_completed_at() itself untested —
    # deleting it would break nothing, which mutation testing showed.
    original_engine = main.engine
    main.engine = db.get_bind()
    try:
        main._backfill_completed_at()
    finally:
        main.engine = original_engine

    tok = T.set_current_tenant("TA")
    try:
        db.expire_all()
        assert db.query(models.WorkOrder).filter_by(id=wo.id).first().completed_at is not None
    finally:
        T.reset_current_tenant(tok)

    _patch_on_bom_bus(db, wo.id, status="Completed")
    assert _bom_state(db)[:2] == (100, 0), _bom_state(db)
    print("PASS the backfill stops a historic Completed order re-moving its BOM")


def test_the_backfill_is_actually_wired_at_startup():
    """The test above proves the FUNCTION works; this proves it RUNS.

    Deleting the module-level call would leave every historic row unstamped and
    the function still passing its own test — mutation testing found exactly that
    hole. Same source-level wiring guard this file already uses for the
    event_bus.publish coupling.
    """
    import inspect
    import re
    src = inspect.getsource(main)
    # A call at module level (column 0), not the def and not a nested reference.
    assert re.search(r"^_backfill_completed_at\(\)", src, re.M), (
        "main.py no longer calls _backfill_completed_at() at import time — historic "
        "Completed work orders would keep a NULL completed_at and re-move their BOM "
        "on the next PATCH.")
    print("PASS the completed_at backfill is wired at startup")


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
    test_get_work_orders_survives_a_null_actual_quantity_row()
    test_status_only_patch_on_a_null_actual_row_serialises()
    test_work_order_response_heals_only_null_not_a_real_zero()
    test_update_work_order_survives_a_null_target_quantity_row()
    test_work_order_response_heals_null_target_quantity_not_500()
    test_completion_reaching_target_publishes_actual_quantity()
    test_reopening_and_recompleting_does_not_move_the_bom_again()
    test_a_repeat_completed_patch_still_moves_nothing()
    test_completion_stamps_completed_at_once_and_never_rewrites_it()
    test_an_order_created_already_completed_never_moves_the_bom()
    test_the_backfill_stamps_existing_completed_orders()
    test_the_backfill_is_actually_wired_at_startup()
    print("ALL WORK-ORDERS ROUTE TESTS PASSED")
