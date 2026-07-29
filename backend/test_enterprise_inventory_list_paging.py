"""Remnant / issue-slip lists: bounded pages and label-only item lookups.

The GRN and cycle-count HISTORY endpoints were bounded and de-N+1'd earlier
(test_enterprise_inventory_paging), but the two FLAT list endpoints in the same
module — GET /remnants and GET /issue-slips — were left as the original::

    rows  = db.query(Remnant).order_by(Remnant.id.desc()).all()        # whole table
    items = {i.id: i for i in db.query(InventoryItem).all()}           # whole master

Both tables only ever grow (one row per offcut logged / per material request),
so the unbounded `.all()` is the rule-4 antipattern, and hydrating the entire
InventoryItem master as ORM objects just to read (item_code, item_name) is the
same N+1-adjacent waste the history endpoints already retired via _item_labels.

This locks the rewrite down the same way the history test does:

  * a page is byte-identical to the corresponding slice of the OLD algorithm
    (reimplemented below as the reference oracle), so the payload didn't change,
  * paging through every page reproduces the full old result with no dropped or
    duplicated rows at a boundary,
  * the page stays bounded (default 50, hard cap 200) however big the table,
  * hostile limit/offset off the query string are clamped, not crashes, and
  * a row whose item_id is missing / None still renders blank labels, not a 500.

Run:  python backend/test_enterprise_inventory_list_paging.py     (exit 0 = pass)
"""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import models
from database import Base

USER = {"sub": "admin", "tenant": "DEFAULT", "role": "Admin"}


def _seed(n_rows=120, n_items=25):
    """n_rows remnants + n_rows issue slips, labelled from n_items master items.

    Every 7th row is pointed at an item_id that does NOT exist so the missing-item
    branch is exercised under normal paging, not only in the dedicated test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for i in range(n_items):
        db.add(models.InventoryItem(item_code=f"SKU-{i}", item_name=f"Item {i}",
                                    category="Raw", unit="kg", current_stock=10))
    db.commit()
    for r in range(1, n_rows + 1):
        # A real item id in [1, n_items], except every 7th row references a gap.
        item_id = None if r % 7 == 0 else (r % n_items) + 1
        db.add(models.Remnant(tag_no=f"REM-{r}", item_id=item_id,
                              source_reference=f"WO-{r}", original_qty=100,
                              remaining_qty=40, unit="kg", location="Rack A",
                              status="Available", notes=""))
        db.add(models.MaterialIssueSlip(slip_no=f"MIS-{r}", item_id=item_id,
                                        work_order_ref=f"WO-{r}", requested_qty=25,
                                        issued_qty=0, requested_by="Operator",
                                        status="Pending", notes=""))
    db.commit()
    return db


def _ser(x):
    return json.dumps(x, default=str, sort_keys=True)


def _old_get_remnants(db):
    """The pre-fix implementation, kept verbatim as the reference oracle."""
    rows = db.query(models.Remnant).order_by(models.Remnant.id.desc()).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    return [
        {
            "id": r.id, "tag_no": r.tag_no, "item_id": r.item_id,
            "item_code": items[r.item_id].item_code if r.item_id in items else "",
            "item_name": items[r.item_id].item_name if r.item_id in items else "",
            "source_reference": r.source_reference,
            "original_qty": r.original_qty, "remaining_qty": r.remaining_qty,
            "unit": r.unit, "location": r.location,
            "status": r.status, "notes": r.notes, "created_at": r.created_at,
        }
        for r in rows
    ]


def _old_get_issue_slips(db):
    rows = db.query(models.MaterialIssueSlip).order_by(models.MaterialIssueSlip.id.desc()).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    return [
        {
            "id": s.id, "slip_no": s.slip_no, "item_id": s.item_id,
            "item_code": items[s.item_id].item_code if s.item_id in items else "",
            "item_name": items[s.item_id].item_name if s.item_id in items else "",
            "remnant_id": s.remnant_id, "work_order_ref": s.work_order_ref,
            "requested_qty": s.requested_qty, "issued_qty": s.issued_qty,
            "requested_by": s.requested_by, "approved_by": s.approved_by,
            "status": s.status, "notes": s.notes,
            "created_at": s.created_at, "issued_at": s.issued_at,
        }
        for s in rows
    ]


CASES = [
    ("remnants", eir.get_remnants, _old_get_remnants),
    ("issue-slips", eir.get_issue_slips, _old_get_issue_slips),
]


def test_a_page_is_identical_to_the_old_result():
    db = _seed()
    for name, new_fn, old_fn in CASES:
        old = old_fn(db)
        assert len(old) == 120, f"{name}: oracle should see all 120 rows, saw {len(old)}"
        page = new_fn(limit=50, offset=0, db=db, current_user=USER)
        assert len(page) == 50, f"{name}: expected a 50-row page, got {len(page)}"
        assert _ser(page) == _ser(old[:50]), f"{name}: the rewrite changed the payload"
    print("PASS the first page is byte-identical to the old algorithm, for both endpoints")


def test_paging_reproduces_the_whole_result_with_no_gaps_or_repeats():
    """Off-by-one at a page boundary would silently drop or duplicate a row."""
    db = _seed()
    for name, new_fn, old_fn in CASES:
        old = old_fn(db)
        walked, offset = [], 0
        while True:
            page = new_fn(limit=25, offset=offset, db=db, current_user=USER)
            if not page:
                break
            walked.extend(page)
            offset += 25
        ids = [row["id"] for row in walked]
        assert len(ids) == 120, f"{name}: walked {len(ids)} rows, expected 120"
        assert len(set(ids)) == 120, f"{name}: a row was duplicated across a page boundary"
        assert _ser(walked) == _ser(old), f"{name}: full walk differs from the old result"
    print("PASS paging through every page reproduces the full old result (no gaps/repeats)")


def test_rows_come_back_newest_first():
    """id desc — the same order the old `.all()` produced."""
    db = _seed()
    for name, new_fn, _ in CASES:
        page = new_fn(limit=10, offset=0, db=db, current_user=USER)
        ids = [row["id"] for row in page]
        assert ids == sorted(ids, reverse=True), f"{name}: page not newest-first: {ids}"
        assert ids[0] == 120, f"{name}: newest id should be 120, got {ids[0]}"
    print("PASS the first page is the newest rows, id-descending")


def test_the_page_is_bounded_however_big_the_table_gets():
    """A caller asking for 10,000 rows still gets at most _PAGE_MAX."""
    big = _seed(n_rows=500)
    for name, new_fn, _ in CASES:
        got = new_fn(limit=10_000, offset=0, db=big, current_user=USER)
        assert len(got) == eir._PAGE_MAX, \
            f"{name}: expected the {eir._PAGE_MAX} cap, got {len(got)}"
    print(f"PASS an oversized limit is capped at _PAGE_MAX ({eir._PAGE_MAX})")


def test_default_page_size_is_fifty():
    db = _seed()
    for name, new_fn, _ in CASES:
        got = new_fn(db=db, current_user=USER)  # no limit/offset supplied
        assert len(got) == eir._PAGE_DEFAULT == 50, \
            f"{name}: default page should be 50, got {len(got)}"
    print("PASS omitting limit/offset yields the default 50-row page")


def test_hostile_paging_params_do_not_crash():
    """limit/offset come straight off the query string."""
    db = _seed()
    for name, new_fn, _ in CASES:
        assert len(new_fn(limit=0, offset=0, db=db, current_user=USER)) == 1, \
            f"{name}: limit 0 -> at least 1 row"
        assert len(new_fn(limit=-5, offset=-5, db=db, current_user=USER)) == 1, \
            f"{name}: negatives clamped to at least 1 row"
        assert new_fn(limit=5, offset=9999, db=db, current_user=USER) == [], \
            f"{name}: past the end is an empty page, not an error"
    print("PASS zero, negative and past-the-end paging params are clamped, not crashes")


def test_a_row_pointing_at_a_missing_or_null_item_renders_blank():
    """item_id can be NULL (drawn from general stock) or point at a deleted item;
    either must render blank labels, never KeyError/500. The seed makes every 7th
    row (ids 7, 14, 21, ...) reference a gap, so the newest page contains some."""
    db = _seed()
    for name, new_fn, _ in CASES:
        page = new_fn(limit=50, offset=0, db=db, current_user=USER)
        blanks = [row for row in page if row["item_code"] == ""]
        assert blanks, f"{name}: expected some blank-label rows on the first page"
        for row in blanks:
            assert row["item_name"] == "", f"{name}: blank code but non-blank name: {row}"
    # An explicit NULL item_id, isolated.
    db2 = _seed(n_rows=1)
    db2.query(models.Remnant).update({models.Remnant.item_id: None})
    db2.query(models.MaterialIssueSlip).update({models.MaterialIssueSlip.item_id: None})
    db2.commit()
    for name, new_fn, _ in CASES:
        row = new_fn(limit=5, offset=0, db=db2, current_user=USER)[0]
        assert row["item_id"] is None, f"{name}: item_id should be NULL"
        assert row["item_code"] == "" and row["item_name"] == "", \
            f"{name}: NULL item_id must render blank labels, got {row}"
    print("PASS a missing / NULL item_id renders blank labels, not a 500")


def test_empty_tables_return_an_empty_list():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for name, new_fn, _ in CASES:
        assert new_fn(limit=50, offset=0, db=db, current_user=USER) == [], \
            f"{name}: empty table should return []"
    print("PASS an empty table returns [] rather than erroring")


if __name__ == "__main__":
    test_a_page_is_identical_to_the_old_result()
    test_paging_reproduces_the_whole_result_with_no_gaps_or_repeats()
    test_rows_come_back_newest_first()
    test_the_page_is_bounded_however_big_the_table_gets()
    test_default_page_size_is_fifty()
    test_hostile_paging_params_do_not_crash()
    test_a_row_pointing_at_a_missing_or_null_item_renders_blank()
    test_empty_tables_return_an_empty_list()
    print("ALL ENTERPRISE-INVENTORY LIST-PAGING TESTS PASSED")
