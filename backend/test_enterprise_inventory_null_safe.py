"""Enterprise-inventory NULL-stock safety + bounded variance report.

InventoryItem.current_stock / reorder_level are Column(Integer, default=0)
WITHOUT nullable=False, so a raw-SQL / migration / cleared-field write can store
a true NULL. Every mutation that reads on-hand stock (issue slip, GRN accept,
cycle count) and the variance report used those columns raw, so a single NULL
row raised TypeError -> an unhandled 500. These tests pin the coalesce (a NULL
stock is an empty shelf = 0, the codebase-wide convention) and check that the
variance report's per-item received/issued totals — now summed in SQL with one
GROUP BY instead of a whole-ledger Python scan — equal an independently derived
reference.

Run:  python backend/test_enterprise_inventory_null_safe.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import enterprise_inventory_routes as eir
from database import Base

_ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(db, item_id, code, stock, reorder=0):
    db.add(models.InventoryItem(
        id=item_id, item_code=code, item_name=f"Item {code}",
        category="Raw", unit="pc", current_stock=stock, reorder_level=reorder,
    ))


def test_issue_slip_null_stock_is_400_not_500():
    # NULL stock -> 0 available; a request for 5 fails cleanly as "insufficient"
    # (HTTP 400), not TypeError on `None < 5`.
    db = _sess()
    _item(db, 1, "IC-1", stock=None)
    db.add(models.MaterialIssueSlip(id=1, slip_no="MIS-1", item_id=1,
                                    requested_qty=5, requested_by="op", status="Approved"))
    db.commit()

    try:
        eir.issue_slip(1, db=db, current_user=_ADMIN)
        assert False, "issuing more than the (NULL->0) stock should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
        assert "0" in e.detail, e.detail          # reports 0 available, not "None"
    print("PASS issue_slip on NULL stock -> clean 400, no TypeError")


def test_issue_slip_null_stock_zero_request_heals_to_zero():
    # A zero-qty issue against a NULL-stock item exercises the subtraction path:
    # 0 - 0 = 0 (heals the NULL to a real 0), no crash.
    db = _sess()
    _item(db, 1, "IC-1", stock=None)
    db.add(models.MaterialIssueSlip(id=1, slip_no="MIS-0", item_id=1,
                                    requested_qty=0, requested_by="op", status="Approved"))
    db.commit()

    eir.issue_slip(1, db=db, current_user=_ADMIN)
    item = db.query(models.InventoryItem).filter(models.InventoryItem.id == 1).first()
    assert item.current_stock == 0, item.current_stock
    print("PASS issue_slip heals NULL stock to 0 on the deduction path")


def test_accept_grn_null_stock_adds_from_zero():
    # Draft GRN, item with NULL stock: accepting 10 makes stock 0 + 10 = 10, not a
    # TypeError on `None += 10`.
    db = _sess()
    _item(db, 1, "IC-1", stock=None)
    db.add(models.GoodsReceiptNote(id=1, grn_no="GRN-1", supplier_name="Acme",
                                   received_by="rx", status="Draft"))
    db.add(models.GRNItem(grn_id=1, item_id=1, received_qty=10, accepted_qty=10))
    db.commit()

    r = eir.accept_grn(1, db=db, current_user=_ADMIN)
    assert r["status"] == "Accepted", r
    item = db.query(models.InventoryItem).filter(models.InventoryItem.id == 1).first()
    assert item.current_stock == 10, item.current_stock
    print("PASS accept_grn on NULL stock adds from 0, no TypeError")


def test_create_cycle_count_null_stock_book_qty_zero():
    # Item with NULL stock: the count line records book_qty=0 (not NULL, which would
    # violate CycleCountItem.book_qty nullable=False) and variance = physical - 0.
    db = _sess()
    _item(db, 1, "IC-1", stock=None)
    db.commit()

    eir.create_cycle_count({"items": [{"item_id": 1, "physical_qty": 7}]},
                           db=db, current_user=_ADMIN)
    line = db.query(models.CycleCountItem).filter(models.CycleCountItem.item_id == 1).first()
    assert line is not None
    assert line.book_qty == 0, line.book_qty
    assert line.variance == 7, line.variance           # 7 - 0
    print("PASS create_cycle_count on NULL stock: book_qty=0, variance=physical, no crash")


def test_variance_report_null_stock_no_crash_and_honest_status():
    # A single NULL row used to 500 the whole report. Each item's status must be
    # correct with NULL treated as 0, and book_stock must reconcile with it.
    db = _sess()
    _item(db, 1, "A", stock=None, reorder=5)     # NULL stock -> 0 -> Stockout
    _item(db, 2, "B", stock=0, reorder=5)        # explicit 0 -> Stockout
    _item(db, 3, "C", stock=2, reorder=10)       # 0 < 2 <= 10 -> Low
    _item(db, 4, "D", stock=3, reorder=None)     # NULL reorder -> 0; 3 !=0, 3<=0 False -> OK
    _item(db, 5, "E", stock=20, reorder=10)      # OK
    db.commit()

    rows = eir.variance_report(db=db, current_user=_ADMIN)
    by_code = {r["item_code"]: r for r in rows}
    assert by_code["A"]["status"] == "Stockout" and by_code["A"]["book_stock"] == 0
    assert by_code["B"]["status"] == "Stockout"
    assert by_code["C"]["status"] == "Low"
    assert by_code["D"]["status"] == "OK" and by_code["D"]["book_stock"] == 3
    assert by_code["E"]["status"] == "OK"
    print("PASS variance_report survives NULL stock/reorder and reports honest status")


def test_variance_report_totals_summed_in_sql_match_reference():
    # Pin the SQL GROUP BY aggregate to independently-derived numbers:
    #   received = Receive + Return, issued = Issue ONLY. "Adjust" and "Scrap" are
    #   BOTH excluded from either column. Adjust is not a directional movement — its
    #   stored quantity is an absolute stock reset (inventory_routes) or abs(variance)
    #   (approve_cycle_count), so summing it as issued reports a figure the data can't
    #   support; the cycle-count correction is surfaced separately as last_variance.
    db = _sess()
    _item(db, 1, "A", stock=100, reorder=10)
    _item(db, 2, "B", stock=50, reorder=10)      # no transactions -> (0, 0)
    for tt, qty in [("Receive", 10), ("Return", 5), ("Issue", 3),
                    ("Adjust", 2), ("Scrap", 99)]:   # "Adjust" / "Scrap" -> neither column
        db.add(models.InventoryTransaction(item_id=1, transaction_type=tt, quantity=qty))
    db.commit()

    rows = {r["item_code"]: r for r in eir.variance_report(db=db, current_user=_ADMIN)}
    a = rows["A"]
    assert a["total_received"] == 15, a["total_received"]   # 10 + 5, "Scrap" excluded
    assert a["total_issued"] == 3, a["total_issued"]        # Issue only; "Adjust" excluded
    b = rows["B"]
    assert b["total_received"] == 0 and b["total_issued"] == 0   # item with no ledger
    print("PASS variance_report SQL totals match the reference (Receive+Return in / Issue out)")


if __name__ == "__main__":
    test_issue_slip_null_stock_is_400_not_500()
    test_issue_slip_null_stock_zero_request_heals_to_zero()
    test_accept_grn_null_stock_adds_from_zero()
    test_create_cycle_count_null_stock_book_qty_zero()
    test_variance_report_null_stock_no_crash_and_honest_status()
    test_variance_report_totals_summed_in_sql_match_reference()
    print("ENTERPRISE-INVENTORY NULL-SAFETY OK: mutations + variance report survive NULL stock")
