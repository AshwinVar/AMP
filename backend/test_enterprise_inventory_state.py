"""Enterprise-inventory state guards: mutations move stock at most once.

accept_grn adds each line's accepted_qty to stock; approve->issue deducts stock;
approve_cycle_count applies a counted variance. Each must be guarded by the
record's status, or a repeat call silently inflates (GRN), double-deducts (issue
slip) or destroys (cycle count) inventory.

The cycle-count guard was missing while the other two had it — this file tested
two of the three, which is how it went unnoticed.

Run:  python backend/test_enterprise_inventory_state.py
"""
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import enterprise_inventory_routes as eir
from database import Base

_ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _stock(db, item_id=1):
    return db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first().current_stock


def test_accept_grn_is_idempotent_no_double_stock():
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=0))
    db.add(models.GoodsReceiptNote(id=1, grn_no="GRN-1", supplier_name="Acme",
                                   received_by="rx", status="Draft"))
    db.add(models.GRNItem(grn_id=1, item_id=1, received_qty=10, accepted_qty=10))
    db.commit()

    r = eir.accept_grn(1, db=db, current_user=_ADMIN)
    assert r["status"] == "Accepted"
    assert _stock(db) == 10

    try:
        eir.accept_grn(1, db=db, current_user=_ADMIN)          # already processed
        assert False, "re-accepting a processed GRN should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db) == 10                                     # NOT 20
    assert db.query(models.InventoryTransaction).count() == 1   # one Receive, not two
    print("PASS accept_grn is idempotent — stock added once, not per call")


def test_issued_slip_cannot_be_re_approved_and_re_issued():
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=100))
    db.add(models.MaterialIssueSlip(id=1, slip_no="MIS-1", item_id=1, requested_qty=10,
                                    requested_by="op", status="Pending"))
    db.commit()

    eir.approve_issue_slip(1, db=db, current_user=_ADMIN)
    eir.issue_slip(1, db=db, current_user=_ADMIN)
    assert _stock(db) == 90                                     # issued once

    try:
        eir.approve_issue_slip(1, db=db, current_user=_ADMIN)  # Issued -> cannot re-approve
        assert False, "re-approving an Issued slip should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db) == 90                                     # NOT 80
    print("PASS an Issued slip can't be re-approved -> no double stock deduction")


def _count(db, physical, book=100, no="CC-1"):
    """A Draft cycle count over item 1. variance is stored once, at creation."""
    db.add(models.CycleCount(id=1, count_no=no, status="Draft", counted_by="ops"))
    db.commit()
    db.add(models.CycleCountItem(count_id=1, item_id=1, book_qty=book,
                                 physical_qty=physical, variance=physical - book))
    db.commit()


def _ledger(db):
    return [(t.transaction_type, t.quantity) for t in
            db.query(models.InventoryTransaction).order_by(models.InventoryTransaction.id).all()]


def test_approving_a_cycle_count_twice_cannot_destroy_stock():
    """THE BUG. approve_cycle_count never read c.status before setting it to
    "Approved", and variance is written once at creation and never cleared — so
    `variance != 0` was still true on a repeat call and the whole body re-fired.

    Measured before the fix, on an item counted at 90 and then genuinely
    receipted by 500:

        approve #1 -> 90    receipt -> 590    approve #2 -> 90

    500 units destroyed, plus a second "Adjust 10" ledger row that makes the
    trail corroborate the corrupted figure rather than expose it.
    """
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=100))
    db.commit()
    _count(db, physical=90)

    eir.approve_cycle_count(1, db=db, current_user=_ADMIN)
    assert _stock(db) == 90, _stock(db)
    assert _ledger(db) == [("Adjust", 10)], _ledger(db)

    # A real receipt lands after the count is approved.
    db.query(models.InventoryItem).filter(models.InventoryItem.id == 1).first().current_stock = 590
    db.commit()

    try:
        eir.approve_cycle_count(1, db=db, current_user=_ADMIN)
        assert False, "re-approving an Approved cycle count should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code

    assert _stock(db) == 590, _stock(db)                    # NOT 90
    assert _ledger(db) == [("Adjust", 10)], _ledger(db)     # no second adjustment
    print("PASS an Approved cycle count can't be re-approved -> no stock destroyed")


def test_a_count_approved_later_keeps_what_was_booked_in_between():
    """The other half, and it needs no double-click at all.

    A count is taken on the floor and approved later — that is what the
    Draft -> Approved workflow is FOR. Writing `item.current_stock =
    line.physical_qty` applies a count-time figure at approve time, silently
    reverting everything received or issued in between. One legitimate approve,
    same 500-unit loss.

    Applying the variance as a delta is also what this block's own ledger row
    already claims: it records quantity=abs(variance) and "Variance: -10", never
    "set to 90".
    """
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=100))
    db.commit()
    _count(db, physical=90)                                  # 09:00, book 100, shelf 90

    # 09:30 — a genuine receipt of 500 while the count sits in Draft.
    db.query(models.InventoryItem).filter(models.InventoryItem.id == 1).first().current_stock = 600
    db.commit()

    # 17:00 — approved once, by the book.
    eir.approve_cycle_count(1, db=db, current_user=_ADMIN)

    # 90 counted + 500 received. The old absolute set wrote 90.
    assert _stock(db) == 590, _stock(db)
    assert _ledger(db) == [("Adjust", 10)], _ledger(db)
    print("PASS a delayed approve corrects by the variance, keeping later movement")


def test_a_zero_variance_count_still_closes_without_touching_stock():
    """A count that agrees with the book moves nothing and writes no ledger row,
    but must still leave the record Approved — otherwise it stays re-approvable
    for ever and the guard above never engages."""
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=100))
    db.commit()
    _count(db, physical=100)                                 # variance 0

    eir.approve_cycle_count(1, db=db, current_user=_ADMIN)
    assert _stock(db) == 100
    assert _ledger(db) == []
    assert db.query(models.CycleCount).filter(models.CycleCount.id == 1).first().status == "Approved"

    try:
        eir.approve_cycle_count(1, db=db, current_user=_ADMIN)
        assert False, "a closed zero-variance count should still refuse re-approval"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    print("PASS a zero-variance count closes cleanly and stays closed")


def test_a_count_on_an_empty_shelf_does_not_crash():
    """current_stock is a nullable Integer, so the delta needs the same coalesce
    accept_grn uses, or `None + 7` raises TypeError -> an unhandled 500.

    The NULL has to be written in raw SQL. `current_stock = Column(Integer,
    default=0)` is a PYTHON-side default, so passing current_stock=None to the
    model stores 0 and the case under test never exists — mutation testing caught
    that: with a None-valued fixture, removing the coalesce broke nothing,
    because the row held 0 all along. Verified directly:

        passing current_stock=None to the model  -> 0
        after a raw SQL UPDATE ... = NULL        -> None
    """
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=0))
    db.commit()
    db.execute(text("UPDATE inventory_items SET current_stock = NULL WHERE id = 1"))
    db.commit()
    db.expire_all()
    assert _stock(db) is None, "fixture failed to build a real NULL"

    _count(db, physical=7, book=0)                           # variance +7
    eir.approve_cycle_count(1, db=db, current_user=_ADMIN)
    assert _stock(db) == 7, _stock(db)
    print("PASS a NULL current_stock coalesces to 0 rather than raising")


if __name__ == "__main__":
    test_accept_grn_is_idempotent_no_double_stock()
    test_issued_slip_cannot_be_re_approved_and_re_issued()
    test_approving_a_cycle_count_twice_cannot_destroy_stock()
    test_a_count_approved_later_keeps_what_was_booked_in_between()
    test_a_zero_variance_count_still_closes_without_touching_stock()
    test_a_count_on_an_empty_shelf_does_not_crash()
    print("ENTERPRISE-INVENTORY STATE OK: GRN accept + slip approve + cycle-count approve "
          "move stock at most once")
