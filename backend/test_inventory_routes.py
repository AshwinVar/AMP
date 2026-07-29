"""Inventory route registration test (ADR-0009).

Stock items + the transaction ledger + the low-stock escalation generator live
in inventory_routes.register(app), peeled out of main.py. Guards registration +
sole ownership, and that the InventoryLow event publish survived the move.

Note: /inventory/enterprise/* and the GMATS inventory paths are owned by their
own modules (enterprise_inventory_routes, gmats_inventory_routes) and are not
asserted here — this guards only the base /inventory CRUD paths.

Run:  python backend/test_inventory_routes.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
import models
import inventory_routes
import schemas
import tenancy as T
from database import Base

EXPECTED = {
    "/inventory/items",
    "/inventory/items/{item_id}",
    "/inventory/transactions",
    "/inventory/generate-low-stock-escalations",
}


def test_inventory_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"inventory_routes"}}
    assert not wrong, f"inventory paths not owned solely by inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} inventory paths owned by inventory_routes")


def test_low_stock_still_publishes_inventory_low():
    import inspect
    import inventory_routes
    src = inspect.getsource(inventory_routes)
    assert "InventoryLow(" in src, "InventoryLow publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS recording a transaction still publishes InventoryLow")


TENANT = "T1"


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_item(db, code, current_stock, reorder_level):
    item = models.InventoryItem(
        tenant_code=TENANT, item_code=code, item_name=code, category="Raw",
        unit="pcs", current_stock=current_stock, reorder_level=reorder_level,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _null_out(db, item_id, columns):
    # Force a real NULL — current_stock / reorder_level are Column(Integer,
    # default=0) so passing None through the ORM would apply the default. Raw SQL
    # is how the sibling null-safe tests (recommendations / costing / saas) plant a
    # NULL that a raw-SQL / migration / cleared write would leave in the column.
    sets = ", ".join(f"{c} = NULL" for c in columns)
    db.execute(text(f"UPDATE inventory_items SET {sets} WHERE id = :i"), {"i": item_id})
    db.commit()
    db.expire_all()


def _txn(db, item_id, ttype, qty):
    tok = T.set_current_tenant(TENANT)
    try:
        payload = schemas.InventoryTransactionCreate(
            item_id=item_id, transaction_type=ttype, quantity=qty,
        )
        return inventory_routes.create_inventory_transaction(
            payload, db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)


def _inventory_low_events(db):
    return db.query(models.EventLog).filter(models.EventLog.event_type == "InventoryLow").all()


def test_receive_on_null_stock_heals_to_a_concrete_number():
    # A NULL current_stock (raw-SQL / migration / cleared write) receiving 30 must
    # not 500 on `None + qty`. NULL is the column's own default of 0, so the base
    # is 0 and the ledger write stores a concrete 0 + 30 = 30 — the NULL is healed,
    # not propagated back into the row.
    db = _iso_session()
    item = _make_item(db, "P-NULL", current_stock=0, reorder_level=0)
    _null_out(db, item.id, ["current_stock", "reorder_level"])
    _txn(db, item.id, "Receive", 30)
    db.refresh(item)
    assert item.current_stock == 30, item.current_stock
    print("PASS receive on NULL stock heals to a concrete number (0 + 30 = 30)")


def test_issue_on_null_stock_is_insufficient_not_500():
    # A NULL current_stock issuing any positive quantity: base is 0, and 0 < qty,
    # so the honest answer is 400 Insufficient stock — never a TypeError-500 from
    # `None < qty`, and never a silent negative stock.
    db = _iso_session()
    item = _make_item(db, "P-ISS", current_stock=0, reorder_level=0)
    _null_out(db, item.id, ["current_stock", "reorder_level"])
    from fastapi import HTTPException
    try:
        _txn(db, item.id, "Issue", 5)
        assert False, "issuing from NULL/zero stock must raise, not succeed"
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    db.refresh(item)
    assert item.current_stock is None, "a rejected issue must not mutate the stock"
    print("PASS issue on NULL stock is a clean 400, not a 500")


def test_null_reorder_level_skips_crossing_event_without_crashing():
    # reorder_level NULL: `stock <= None` is undefined, so the crossing check is
    # skipped (a NULL level can't say whether the item is low) rather than crashing
    # or fabricating a crossing. The stock arithmetic still applies: 50 - 40 = 10.
    db = _iso_session()
    item = _make_item(db, "P-NR", current_stock=50, reorder_level=0)
    _null_out(db, item.id, ["reorder_level"])
    _txn(db, item.id, "Issue", 40)
    db.refresh(item)
    assert item.current_stock == 10, item.current_stock
    assert _inventory_low_events(db) == [], "a NULL reorder level must not raise InventoryLow"
    print("PASS NULL reorder level skips the crossing event without crashing")


def test_issue_crossing_reorder_still_publishes_inventory_low():
    # Regression guard on the preserved behaviour: an issue that drops stock to/below
    # a configured reorder level publishes exactly one InventoryLow. Numbers derived
    # independently: 50 - 35 = 15, and 15 <= 20 is the crossing (was 50 > 20 before).
    db = _iso_session()
    item = _make_item(db, "P-CROSS", current_stock=50, reorder_level=20)
    _txn(db, item.id, "Issue", 35)
    db.refresh(item)
    assert item.current_stock == 15, item.current_stock
    events = _inventory_low_events(db)
    assert len(events) == 1, f"expected one InventoryLow, got {len(events)}"
    print("PASS issue crossing the reorder level still publishes InventoryLow (50-35=15 <= 20)")


def test_receive_above_reorder_publishes_no_event():
    # Above-reorder-and-staying-above must not fire InventoryLow: 50 + 10 = 60, and
    # 60 <= 20 is false. Guards the crossing condition, not just "reorder present".
    db = _iso_session()
    item = _make_item(db, "P-UP", current_stock=50, reorder_level=20)
    _txn(db, item.id, "Receive", 10)
    db.refresh(item)
    assert item.current_stock == 60, item.current_stock
    assert _inventory_low_events(db) == [], "receiving above reorder must not raise InventoryLow"
    print("PASS receive that stays above reorder publishes no event (50+10=60)")


def test_list_serialises_legacy_null_row_without_500():
    # The bug this fix closes: GET /inventory/items returns raw ORM rows, and FastAPI
    # validates each through InventoryItemResponse (current_stock/reorder_level are
    # non-optional int). A legacy NULL row (raw-SQL / migration) never went through
    # the PATCH in-handler heal, so the raw-ORM list serializer was the one reader
    # left un-healed — a single such row raised ValidationError and 500-ed the WHOLE
    # list, hiding every good item. model_validate is exactly what the response layer
    # does; assert the NULL row heals to 0 and the good row is untouched.
    db = _iso_session()
    tok = T.set_current_tenant(TENANT)
    try:
        good = _make_item(db, "P-GOOD", current_stock=25, reorder_level=5)
        bad = _make_item(db, "P-LEGACY-NULL", current_stock=0, reorder_level=0)
        _null_out(db, bad.id, ["current_stock", "reorder_level"])
        rows = inventory_routes.get_inventory_items(db=db, current_user={"tenant": TENANT})
        serialised = [schemas.InventoryItemResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)
    by_code = {s.item_code: s for s in serialised}
    assert by_code["P-LEGACY-NULL"].current_stock == 0, by_code["P-LEGACY-NULL"].current_stock
    assert by_code["P-LEGACY-NULL"].reorder_level == 0, by_code["P-LEGACY-NULL"].reorder_level
    assert isinstance(by_code["P-LEGACY-NULL"].current_stock, int)
    # the good row's real values are untouched — the heal only rewrites None
    assert by_code["P-GOOD"].current_stock == 25 and by_code["P-GOOD"].reorder_level == 5
    print("PASS GET list serialises a legacy NULL row as 0 (not a 500 that hides every item)")


def _update_item(db, item_id, **fields):
    # Call the PATCH handler directly (the require_roles dependency isn't run on a
    # direct call, like _txn above). model_validate is how a JSON body with an
    # explicit `null` deserialises — exclude_unset then KEEPS that null, exactly the
    # client-injectable path the response-serialisation 500 lived on.
    tok = T.set_current_tenant(TENANT)
    try:
        payload = schemas.InventoryItemUpdate.model_validate(fields)
        return inventory_routes.update_inventory_item(
            item_id, payload, db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)


def test_update_explicit_null_stock_heals_not_500():
    # A schema-valid PATCH {"current_stock": null} used to blank the column to NULL
    # and 500 on the way out (InventoryItemResponse.current_stock is non-optional
    # int). The null is the column's own default of 0 — so it heals to a concrete 0
    # and the response serialises cleanly. reorder_level (untouched) is unchanged.
    db = _iso_session()
    item = _make_item(db, "P-UPD-NULL", current_stock=42, reorder_level=7)
    result = _update_item(db, item.id, current_stock=None)
    assert result.current_stock == 0, result.current_stock
    assert result.reorder_level == 7, result.reorder_level
    db.refresh(item)
    assert item.current_stock == 0 and item.reorder_level == 7
    print("PASS PATCH {current_stock: null} heals to 0, not a 500")


def test_update_legacy_null_row_serialises_zero():
    # A pre-existing NULL row (raw-SQL / migration) PATCHed on an UNRELATED field
    # (location) must not 500 on serialisation: the response coalesces both
    # nullable counts to their default of 0 while applying the real edit.
    db = _iso_session()
    item = _make_item(db, "P-UPD-LEGACY", current_stock=0, reorder_level=0)
    _null_out(db, item.id, ["current_stock", "reorder_level"])
    result = _update_item(db, item.id, location="Rack B")
    assert result.current_stock == 0, result.current_stock
    assert result.reorder_level == 0, result.reorder_level
    assert result.location == "Rack B", result.location
    print("PASS PATCH on a legacy NULL row serialises current_stock/reorder_level as 0")


def test_update_negative_stock_rejected_400():
    # A negative stock is physically impossible and corrupts the low-stock
    # read-model; the update rejects it with a clean 400 (parity with the create
    # ingests) and does NOT mutate the stored row (uncommitted session discards it).
    from fastapi import HTTPException
    db = _iso_session()
    item = _make_item(db, "P-UPD-NEG", current_stock=50, reorder_level=20)
    try:
        _update_item(db, item.id, current_stock=-5)
        assert False, "a negative current_stock must be rejected, not stored"
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    # Production rolls the request session back on close after the raise (nothing is
    # committed); mirror that here so the pending -5 is discarded before we re-read.
    db.rollback()
    db.refresh(item)
    assert item.current_stock == 50, "a rejected update must not mutate the stock"
    print("PASS PATCH {current_stock: -5} is a clean 400 and leaves the row unchanged")


def test_update_normal_edit_still_applies():
    # Sanity: a normal positive edit still writes through untouched by the guards
    # (a real 0 stays 0, a healthy value is stored verbatim, `or 0` never zeroes it).
    db = _iso_session()
    item = _make_item(db, "P-UPD-OK", current_stock=50, reorder_level=20)
    result = _update_item(db, item.id, current_stock=80, reorder_level=0)
    assert result.current_stock == 80, result.current_stock
    assert result.reorder_level == 0, result.reorder_level
    print("PASS PATCH normal edit applies (80) and a real 0 reorder level stays 0")


def test_low_stock_null_status_existing_escalation_is_deduped_not_duplicated():
    """generate_low_stock_escalations must treat a NULL-status EXISTING escalation
    as still open, matching the "NULL is not terminal" convention every
    open-escalation reader uses (#295/#403). status is String default="Open" (not
    NOT NULL), so a raw-SQL / migration / cleared write can hold a real NULL; SQL's
    `status != 'Resolved'` is NULL — not TRUE — for it, so the bare predicate MISSED
    the open escalation and raised a DUPLICATE, inflating the very open-escalation
    count the readers report. Regression guard for the or_(status IS NULL, ...) fix."""
    db = _iso_session()
    tok = T.set_current_tenant(TENANT)
    try:
        _make_item(db, "IC-1", current_stock=0, reorder_level=10)   # low stock
        title = "Low stock: IC-1 - IC-1"
        db.add(models.Escalation(
            tenant_code=TENANT, title=title, severity="High", owner="Stores",
            department="Inventory", status="Open", source="Inventory"))
        db.commit()
        db.execute(text("UPDATE escalations SET status = NULL WHERE title = :t"),
                   {"t": title})
        db.commit()
        db.expire_all()

        out = inventory_routes.generate_low_stock_escalations(
            db=db, current_user={"tenant": TENANT})
        assert out == {"created": 0}, out                 # NULL-status one is still open
        assert db.query(models.Escalation).count() == 1   # no duplicate raised
    finally:
        T.reset_current_tenant(tok)
    print("PASS low-stock: a NULL-status open escalation is deduped, not duplicated")


if __name__ == "__main__":
    test_inventory_paths_owned_by_module()
    test_low_stock_still_publishes_inventory_low()
    test_receive_on_null_stock_heals_to_a_concrete_number()
    test_issue_on_null_stock_is_insufficient_not_500()
    test_null_reorder_level_skips_crossing_event_without_crashing()
    test_issue_crossing_reorder_still_publishes_inventory_low()
    test_receive_above_reorder_publishes_no_event()
    test_list_serialises_legacy_null_row_without_500()
    test_update_explicit_null_stock_heals_not_500()
    test_update_legacy_null_row_serialises_zero()
    test_update_negative_stock_rejected_400()
    test_update_normal_edit_still_applies()
    test_low_stock_null_status_existing_escalation_is_deduped_not_duplicated()
    print("ALL INVENTORY ROUTE TESTS PASSED")
