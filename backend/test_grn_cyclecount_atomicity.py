"""Goods-receipt and cycle-count creation must be atomic.

Both handlers used to commit the header row and THEN parse the line items::

    db.add(g); db.commit(); db.refresh(g)
    for line in payload.get("items", []):
        ... int(line["accepted_qty"]) ...

Neither takes a Pydantic schema — the body arrives as a raw ``payload: dict`` — so
a missing key, a blank string, or a JSON null went straight into ``int()`` and
raised. FastAPI turns that into a bare HTTP 500, but the header was already
committed, leaving a **zero-line GRN in the receipt history**. That phantom was
not inert: ``accept_grn`` would still mark it "Accepted", putting a goods receipt
on record against which no goods ever moved, and every retry minted another one.
The cycle-count path had the identical shape, where ``approve_cycle_count`` would
mark a count "Approved" having adjusted no stock.

So the failure was not just a 500 — it corrupted the warehouse ledger, which is
the one thing an inventory module exists to keep straight.

Lines are now validated before anything is written, and the header is flushed
rather than committed, so a bad line rolls the whole request back.

Run:  python backend/test_grn_cyclecount_atomicity.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import models
from database import Base

ADMIN = {"sub": "admin", "tenant": "DEFAULT", "role": "Admin"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.InventoryItem(item_code="RM-1", item_name="Steel", category="Raw",
                                unit="kg", current_stock=100))
    db.commit()
    return db


def _counts(db):
    return (db.query(models.GoodsReceiptNote).count(), db.query(models.GRNItem).count(),
            db.query(models.CycleCount).count(), db.query(models.CycleCountItem).count())


# Every shape a real client actually sends for a half-filled quantity field.
BAD_QUANTITIES = [
    ({"item_id": 1, "received_qty": 5}, "missing key"),
    ({"item_id": 1, "received_qty": 5, "accepted_qty": ""}, "blank string from an empty input"),
    ({"item_id": 1, "received_qty": 5, "accepted_qty": None}, "JSON null (undefined/NaN)"),
    ({"item_id": 1, "received_qty": 5, "accepted_qty": "abc"}, "non-numeric text"),
    ({"item_id": 1, "received_qty": 5, "accepted_qty": -3}, "negative quantity"),
    ({"item_id": 1, "received_qty": 5, "accepted_qty": 1e23}, "overflow from Number(long digits)"),
]


def test_a_bad_grn_line_leaves_no_phantom_receipt():
    """The regression this file exists for: one bad line must abort the whole
    request, not commit a header the rest of the app treats as a real receipt."""
    for bad_line, label in BAD_QUANTITIES:
        db = _fresh_session()
        payload = {"supplier_name": "ACME", "items": [
            {"item_id": 1, "received_qty": 10, "accepted_qty": 10},   # a VALID line first
            bad_line,
        ]}
        try:
            eir.create_grn(payload, db=db, current_user=ADMIN)
            assert False, f"{label}: expected a 400, got a successful create"
        except HTTPException as e:
            assert e.status_code == 400, f"{label}: got {e.status_code}, want 400 (500 = the old bug)"
            assert "Line 2" in e.detail, f"{label}: the message must name the offending line: {e.detail}"

        grns, grn_lines, _, _ = _counts(db)
        assert grns == 0, f"{label}: a phantom GRN header survived the failure"
        assert grn_lines == 0, f"{label}: orphan line items survived the failure"
    print(f"PASS all {len(BAD_QUANTITIES)} bad-line shapes 400 with no phantom GRN")


def test_a_bad_cycle_count_line_leaves_no_phantom_count():
    for bad_value, label in [("", "blank"), (None, "null"), ("abc", "text"), (-1, "negative")]:
        db = _fresh_session()
        payload = {"counted_by": "Ravi", "items": [
            {"item_id": 1, "physical_qty": 90},          # a VALID line first
            {"item_id": 1, "physical_qty": bad_value},
        ]}
        try:
            eir.create_cycle_count(payload, db=db, current_user=ADMIN)
            assert False, f"{label}: expected a 400"
        except HTTPException as e:
            assert e.status_code == 400, f"{label}: got {e.status_code}, want 400"

        _, _, counts, count_lines = _counts(db)
        assert counts == 0, f"{label}: a phantom cycle count survived the failure"
        assert count_lines == 0, f"{label}: orphan count lines survived the failure"
    print("PASS bad cycle-count lines 400 with no phantom count")


def test_the_phantom_could_be_accepted_which_is_why_this_matters():
    """Pin the consequence, not just the mechanism: a zero-line GRN accepted as a
    real receipt is a ledger entry with no goods behind it. Proving the phantom
    cannot be created is what keeps accept_grn honest."""
    db = _fresh_session()
    try:
        eir.create_grn({"supplier_name": "ACME",
                        "items": [{"item_id": 1, "received_qty": 5}]},
                       db=db, current_user=ADMIN)
    except HTTPException:
        pass
    # There is no GRN to accept, so the phantom-accept path is unreachable.
    assert db.query(models.GoodsReceiptNote).count() == 0
    try:
        eir.accept_grn(gid=1, db=db, current_user=ADMIN)
        assert False, "there should be nothing to accept"
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
    assert db.query(models.InventoryItem).first().current_stock == 100, "stock must be untouched"
    print("PASS no phantom exists to accept, so stock cannot move for a failed receipt")


def test_missing_supplier_name_is_a_400_not_a_500():
    """payload["supplier_name"] used to KeyError straight into a 500."""
    db = _fresh_session()
    for payload in ({"items": []}, {"supplier_name": "   ", "items": []}):
        try:
            eir.create_grn(payload, db=db, current_user=ADMIN)
            assert False, "expected a 400"
        except HTTPException as e:
            assert e.status_code == 400 and "supplier_name" in e.detail, e.detail
    assert db.query(models.GoodsReceiptNote).count() == 0
    print("PASS a missing supplier_name is a 400 naming the field")


def test_the_valid_path_is_unchanged():
    """The fix must not cost the working case — receipt, accept, stock movement."""
    db = _fresh_session()
    r = eir.create_grn({"supplier_name": "ACME", "purchase_order_ref": "PO-9", "items": [
        {"item_id": 1, "received_qty": 10, "accepted_qty": 8, "rejected_qty": 2, "lot_no": "L1"},
    ]}, db=db, current_user=ADMIN)
    assert r["grn_no"] == "GRN-3001", r
    assert db.query(models.GRNItem).count() == 1

    line = db.query(models.GRNItem).first()
    assert (line.received_qty, line.accepted_qty, line.rejected_qty) == (10, 8, 2)
    assert line.ordered_qty == 0, "an omitted optional quantity still defaults to 0"
    assert line.lot_no == "L1"

    accepted = eir.accept_grn(gid=r["id"], db=db, current_user=ADMIN)
    assert accepted["status"] == "Partial", "8 accepted of 10 received is a partial receipt"
    assert db.query(models.InventoryItem).first().current_stock == 108
    print("PASS a valid GRN still creates, accepts, and moves stock exactly as before")


def test_a_valid_cycle_count_still_records_variance():
    db = _fresh_session()
    r = eir.create_cycle_count({"counted_by": "Ravi", "items": [{"item_id": 1, "physical_qty": 90}]},
                               db=db, current_user=ADMIN)
    assert r["count_no"] == "CC-2001", r
    line = db.query(models.CycleCountItem).first()
    assert (line.book_qty, line.physical_qty, line.variance) == (100, 90, -10)

    # zero is a legitimate physical count (an empty shelf), not a missing value
    db2 = _fresh_session()
    eir.create_cycle_count({"items": [{"item_id": 1, "physical_qty": 0}]}, db=db2, current_user=ADMIN)
    assert db2.query(models.CycleCountItem).first().physical_qty == 0
    print("PASS a valid cycle count records variance, and 0 counts as a real reading")


def test_an_empty_items_list_still_works():
    """Creating a header with no lines yet is a legitimate draft — only a
    *malformed* line is an error."""
    db = _fresh_session()
    r = eir.create_grn({"supplier_name": "ACME", "items": []}, db=db, current_user=ADMIN)
    assert r["grn_no"] == "GRN-3001"
    assert db.query(models.GoodsReceiptNote).count() == 1
    print("PASS an intentionally empty GRN is still allowed")


if __name__ == "__main__":
    test_a_bad_grn_line_leaves_no_phantom_receipt()
    test_a_bad_cycle_count_line_leaves_no_phantom_count()
    test_the_phantom_could_be_accepted_which_is_why_this_matters()
    test_missing_supplier_name_is_a_400_not_a_500()
    test_the_valid_path_is_unchanged()
    test_a_valid_cycle_count_still_records_variance()
    test_an_empty_items_list_still_works()
    print("\nALL GRN / CYCLE-COUNT ATOMICITY TESTS PASSED")
