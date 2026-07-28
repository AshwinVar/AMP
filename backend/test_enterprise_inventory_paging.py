"""GRN / cycle-count history: linear grouping and bounded pages.

Both endpoints used to load every parent row, every child row and the whole item
master, then rescan the *entire* child list once per parent to find its own
lines — an O(parents x children) join done in Python::

    grns = db.query(GoodsReceiptNote)...all()
    grn_items = db.query(GRNItem).all()
    for g in grns:
        gi = [x for x in grn_items if x.grn_id == g.id]     # rescans all of them

Measured on seeded data: 4,000 GRNs with 16,000 lines took 47s, of which 37s was
that one line — the three queries that fetched the data took 0.19s. So the cost
was the rescan, not the I/O. Cycle counts had the identical shape (28s at 2,000
counts), and the UI's select-all checkbox makes counts that contain every SKU, so
the child table grows fast in real use.

The fix is a single grouping pass plus paging. The tests that matter here are the
ones proving the rewrite did not change *what* is returned:

  * a page is byte-identical to the corresponding slice of the old algorithm
    (the old one is reimplemented below as the reference), and
  * paging through every page reproduces the full old result exactly — no
    duplicated or dropped rows at the boundaries.

Run:  python backend/test_enterprise_inventory_paging.py     (exit 0 = pass)
"""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import models
from database import Base

USER = {"sub": "admin", "tenant": "DEFAULT", "role": "Admin"}


def _seed(n_parents=120, lines=3, n_items=25):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for i in range(n_items):
        db.add(models.InventoryItem(item_code=f"SKU-{i}", item_name=f"Item {i}",
                                    category="Raw", unit="kg", current_stock=10))
    for p in range(n_parents):
        db.add(models.GoodsReceiptNote(grn_no=f"GRN-{p}", supplier_name="ACME",
                                       received_by="admin", status="Draft", notes=""))
        db.add(models.CycleCount(count_no=f"CC-{p}", counted_by="Ravi",
                                 status="Draft", notes=""))
    db.commit()
    for p in range(1, n_parents + 1):
        for l in range(lines):
            item_id = (p + l) % n_items + 1
            db.add(models.GRNItem(grn_id=p, item_id=item_id, lot_no=f"L{l}",
                                  ordered_qty=1, received_qty=2, accepted_qty=2,
                                  rejected_qty=0, inspection_status="Accepted"))
            db.add(models.CycleCountItem(count_id=p, item_id=item_id,
                                         book_qty=10, physical_qty=9, variance=-1))
    db.commit()
    return db


def _ser(x):
    return json.dumps(x, default=str, sort_keys=True)


def _old_get_grns(db):
    """The pre-fix implementation, kept verbatim as the reference oracle."""
    grns = db.query(models.GoodsReceiptNote).order_by(models.GoodsReceiptNote.id.desc()).all()
    grn_items = db.query(models.GRNItem).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    result = []
    for g in grns:
        gi = [x for x in grn_items if x.grn_id == g.id]
        result.append({
            "id": g.id, "grn_no": g.grn_no, "purchase_order_ref": g.purchase_order_ref,
            "supplier_name": g.supplier_name, "received_by": g.received_by,
            "status": g.status, "notes": g.notes, "created_at": g.created_at,
            "items": [{
                "id": x.id, "item_id": x.item_id,
                "item_code": items[x.item_id].item_code if x.item_id in items else "",
                "item_name": items[x.item_id].item_name if x.item_id in items else "",
                "lot_no": x.lot_no, "ordered_qty": x.ordered_qty,
                "received_qty": x.received_qty, "accepted_qty": x.accepted_qty,
                "rejected_qty": x.rejected_qty, "inspection_status": x.inspection_status,
            } for x in gi],
        })
    return result


def _old_get_cycle_counts(db):
    counts = db.query(models.CycleCount).order_by(models.CycleCount.id.desc()).all()
    count_items = db.query(models.CycleCountItem).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    result = []
    for c in counts:
        ci = [x for x in count_items if x.count_id == c.id]
        result.append({
            "id": c.id, "count_no": c.count_no, "counted_by": c.counted_by,
            "status": c.status, "notes": c.notes, "created_at": c.created_at,
            "items": [{
                "id": x.id, "item_id": x.item_id,
                "item_code": items[x.item_id].item_code if x.item_id in items else "",
                "item_name": items[x.item_id].item_name if x.item_id in items else "",
                "book_qty": x.book_qty, "physical_qty": x.physical_qty,
                "variance": x.variance,
            } for x in ci],
        })
    return result


CASES = [
    ("grns", eir.get_grns, _old_get_grns),
    ("cycle-counts", eir.get_cycle_counts, _old_get_cycle_counts),
]


def test_a_page_is_identical_to_the_old_result():
    db = _seed()
    for name, new_fn, old_fn in CASES:
        old = old_fn(db)
        page = new_fn(limit=50, offset=0, db=db, current_user=USER)
        assert len(page) == 50, f"{name}: expected a 50-row page, got {len(page)}"
        assert _ser(page) == _ser(old[:50]), f"{name}: the rewrite changed the payload"
    print("PASS the first page is byte-identical to the old algorithm, for both endpoints")


def test_paging_reproduces_the_whole_result_with_no_gaps_or_repeats():
    """Off-by-one at a page boundary would silently drop or duplicate a receipt."""
    db = _seed()
    for name, new_fn, old_fn in CASES:
        old = old_fn(db)
        walked, offset = [], 0
        while True:
            page = new_fn(limit=25, offset=offset, db=db, current_user=USER)
            if not page:
                break
            walked += page
            offset += 25
        assert _ser(walked) == _ser(old), f"{name}: walking the pages != the old full result"
        ids = [r["id"] for r in walked]
        assert len(ids) == len(set(ids)), f"{name}: a row appeared on two pages"
    print("PASS paging through every page reproduces the full old result exactly")


def test_the_page_is_bounded_however_big_the_table_gets():
    """The point of the change: response size must stop tracking table size."""
    small = _seed(n_parents=30)
    big = _seed(n_parents=300)
    for name, new_fn, _ in CASES:
        assert len(new_fn(db=small, current_user=USER)) == 30, name
        assert len(new_fn(db=big, current_user=USER)) == eir._PAGE_DEFAULT, \
            f"{name}: the default page must cap at {eir._PAGE_DEFAULT}, not grow with the table"
        assert len(new_fn(limit=10_000, offset=0, db=big, current_user=USER)) == eir._PAGE_MAX, \
            f"{name}: a caller asking for 10k rows must be clamped to {eir._PAGE_MAX}"
    print(f"PASS pages are bounded — default {eir._PAGE_DEFAULT}, hard cap {eir._PAGE_MAX}")


def test_hostile_paging_params_do_not_crash():
    """limit/offset come straight off the query string."""
    db = _seed(n_parents=10)
    for name, new_fn, _ in CASES:
        assert len(new_fn(limit=0, offset=0, db=db, current_user=USER)) == 1, f"{name}: limit 0 -> at least 1"
        assert len(new_fn(limit=-5, offset=-5, db=db, current_user=USER)) == 1, f"{name}: negatives clamped"
        assert new_fn(limit=5, offset=9999, db=db, current_user=USER) == [], f"{name}: past the end is empty"
    print("PASS zero, negative and past-the-end paging params are clamped, not crashes")


def test_line_items_are_attached_to_the_right_parent():
    """A grouping bug would attach the wrong lines — the failure mode a pure
    speed test would sail straight past."""
    db = _seed(n_parents=8, lines=2)
    for row in eir.get_grns(limit=50, offset=0, db=db, current_user=USER):
        assert len(row["items"]) == 2, row
        for line in row["items"]:
            owner = db.query(models.GRNItem).filter(models.GRNItem.id == line["id"]).first()
            assert owner.grn_id == row["id"], "a line was grouped under the wrong GRN"
            assert line["item_code"].startswith("SKU-"), f"item label not resolved: {line}"
    print("PASS every line item is grouped under its own parent, with labels resolved")


def test_a_line_pointing_at_a_missing_item_still_renders():
    """The old code degraded to empty strings for an unknown item id; the
    narrowed label lookup must do the same rather than KeyError."""
    db = _seed(n_parents=1, lines=1)
    orphan = db.query(models.GRNItem).first()
    orphan.item_id = 999_999            # item that does not exist
    db.commit()
    row = eir.get_grns(limit=5, offset=0, db=db, current_user=USER)[0]
    assert row["items"][0]["item_code"] == "" and row["items"][0]["item_name"] == ""
    print("PASS a line referencing a deleted item renders blank labels, not a 500")


def test_empty_tables_return_an_empty_list():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for name, new_fn, _ in CASES:
        assert new_fn(db=db, current_user=USER) == [], name
    print("PASS an empty history returns [] rather than erroring")


if __name__ == "__main__":
    test_a_page_is_identical_to_the_old_result()
    test_paging_reproduces_the_whole_result_with_no_gaps_or_repeats()
    test_the_page_is_bounded_however_big_the_table_gets()
    test_hostile_paging_params_do_not_crash()
    test_line_items_are_attached_to_the_right_parent()
    test_a_line_pointing_at_a_missing_item_still_renders()
    test_empty_tables_return_an_empty_list()
    print("\nALL ENTERPRISE-INVENTORY PAGING TESTS PASSED")
