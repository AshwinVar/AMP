"""Inventory-record-accuracy read-model tests (ADR-0007).

The cycle-count read-model over cycle_counts + cycle_count_items: of the items
counted in the window, the share whose book matched the physical count exactly
(IRA%), the count-programme coverage of the item master, and the net / absolute
unit variance found. Covers the accuracy split and its reconciliation (exact +
over + under == counted; abs variance sums to its parts, rule 3), independently
derived IRA% and coverage, the recompute-from-physical-book honesty (the stored
`variance` column is ignored, rule 1/2), latest-count-wins, the window bound, the
explicit tenant scoping (a line for a foreign item id is dropped, rule 5), and the
empty / None-field / no-currency edges (rule 6).

Run:  python backend/test_stock_accuracy.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import stock_accuracy
from ai.stock_accuracy import WINDOW_DAYS, ACCURACY_TARGET


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(code, name=None):
    return models.InventoryItem(
        item_code=code, item_name=name or code, category="raw", unit="pcs",
        supplier="Acme", current_stock=0, reorder_level=0)


def _add_items(db, codes):
    for c in codes:
        db.add(_item(c))
    db.commit()
    return {i.item_code: i.id for i in db.query(models.InventoryItem).all()}


def _count(db, no, status="Approved", days_ago=1):
    c = models.CycleCount(count_no=no, counted_by="A. Counter", status=status,
                          created_at=datetime.utcnow() - timedelta(days=days_ago))
    db.add(c); db.commit(); db.refresh(c)
    return c.id


def _line(db, count_id, item_id, book, physical, days_ago=1, variance=None):
    # `variance` defaults to the true physical - book; a test may pass a WRONG value
    # to prove the read-model recomputes it rather than trusting the column.
    db.add(models.CycleCountItem(
        count_id=count_id, item_id=item_id, book_qty=book, physical_qty=physical,
        variance=(physical - book) if variance is None else variance,
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def test_accuracy_split_coverage_and_variance_reconciliation():
    """Eight SKUs, five counted with hand-computed variances. IRA%, coverage, the
    exact/over/under split and the net/absolute variance are all derived by hand."""
    db = _fresh_session()
    ids = _add_items(db, ["A", "B", "C", "D", "E", "F", "G", "H"])   # 8 in the master
    cc1 = _count(db, "CC-1")
    cc2 = _count(db, "CC-2")
    _line(db, cc1, ids["A"], book=100, physical=100)   # exact  (var 0)
    _line(db, cc1, ids["B"], book=100, physical=100)   # exact  (var 0)
    _line(db, cc1, ids["C"], book=50,  physical=60)    # over   (var +10)
    _line(db, cc2, ids["D"], book=80,  physical=65)    # under  (var -15)
    _line(db, cc2, ids["E"], book=200, physical=200)   # exact  (var 0)
    # F, G, H are never counted.

    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")

    assert r["total_items"] == 8
    assert r["counted_items"] == 5
    # IRA% = 3 exact of 5 counted = 60%.
    assert r["accurate_items"] == 3 and r["inaccurate_items"] == 2
    assert r["accuracy_rate"] == 60, r["accuracy_rate"]
    # Coverage = 5 of 8 counted = round(62.5) = 62 (banker's rounding).
    assert r["count_coverage"] == 62, r["count_coverage"]
    assert r["counts_in_window"] == 2                       # CC-1 and CC-2
    # Direction split reconciles to counted_items (rule 3).
    assert r["over_items"] == 1 and r["under_items"] == 1
    assert r["accurate_items"] + r["over_items"] + r["under_items"] == r["counted_items"]
    # Variance in units: net = +10 - 15 = -5; absolute = 10 + 15 = 25.
    assert r["net_variance_units"] == -5, r["net_variance_units"]
    assert r["abs_variance_units"] == 25, r["abs_variance_units"]
    # Only the inaccurate items surface, worst (biggest abs) first: D (15) then C (10).
    assert [i["item_code"] for i in r["items"]] == ["D", "C"]
    assert r["worst"]["item_code"] == "D" and r["worst"]["variance"] == -15
    assert r["worst"]["direction"] == "under"
    assert r["items"][1]["variance"] == 10 and r["items"][1]["direction"] == "over"
    # The absolute variance of the surfaced rows sums to the headline abs variance.
    assert sum(i["abs_variance"] for i in r["items"]) == r["abs_variance_units"]
    # 60% is below the 80% floor -> records-can't-be-trusted verdict.
    assert r["tone"] == "bad", r["tone"]
    # No currency anywhere (the schema carries no unit cost).
    assert "cost" not in r and "value" not in r
    print("PASS accuracy split, coverage, variance reconciliation and worst-first order")


def test_variance_recomputed_from_physical_book_not_the_stored_column():
    """The read-model must derive variance as physical - book, ignoring a drifted
    `variance` column (rule 1/2)."""
    db = _fresh_session()
    ids = _add_items(db, ["X"])
    cc = _count(db, "CC-9")
    # Stored variance is a LIE (+999); the true discrepancy is 70 - 100 = -30.
    _line(db, cc, ids["X"], book=100, physical=70, variance=999)

    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    assert r["counted_items"] == 1
    assert r["net_variance_units"] == -30, r["net_variance_units"]
    assert r["abs_variance_units"] == 30
    assert r["under_items"] == 1 and r["over_items"] == 0
    assert r["worst"]["variance"] == -30
    print("PASS variance is recomputed from physical - book, not read from the column")


def test_latest_count_wins_and_window_bounds_the_scan():
    """An item counted twice is scored on its LATEST count; a count older than the
    window is out of scope entirely."""
    db = _fresh_session()
    ids = _add_items(db, ["X", "Y"])
    cc_old = _count(db, "CC-OLD", days_ago=WINDOW_DAYS + 20)
    cc_new = _count(db, "CC-NEW", days_ago=1)
    # X: an old miss (-30) then a recent correction (0). Latest (exact) must win.
    _line(db, cc_old, ids["X"], book=100, physical=70, days_ago=10)
    _line(db, cc_new, ids["X"], book=100, physical=100, days_ago=1)
    # Y: only ever counted OUTSIDE the window -> not counted in-window at all.
    _line(db, cc_old, ids["Y"], book=50, physical=20, days_ago=WINDOW_DAYS + 20)

    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    # Only X's recent count is in-window; Y's out-of-window count is excluded.
    assert r["counted_items"] == 1, r["counted_items"]
    assert r["accurate_items"] == 1 and r["inaccurate_items"] == 0
    assert r["accuracy_rate"] == 100
    assert r["abs_variance_units"] == 0 and r["net_variance_units"] == 0
    assert r["items"] == []                                # nothing inaccurate to surface
    print("PASS latest count wins and the window bounds the scan")


def test_foreign_item_lines_are_scoped_out():
    """A count line whose item_id is not in the tenant's item master (a foreign /
    orphaned item) must be dropped — the explicit tenant scoping for the un-scoped
    cycle-count tables (rule 5)."""
    db = _fresh_session()
    ids = _add_items(db, ["A"])            # only item id belongs to this master
    cc = _count(db, "CC-1")
    _line(db, cc, ids["A"], book=100, physical=90)     # this tenant's item (var -10)
    _line(db, cc, 999999, book=100, physical=0)        # item id not in the master

    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    assert r["total_items"] == 1
    assert r["counted_items"] == 1                     # only the real item, not #999999
    assert r["abs_variance_units"] == 10               # the foreign -100 line is excluded
    assert r["net_variance_units"] == -10
    print("PASS foreign item-id count lines are scoped out")


def test_empty_and_no_counts_are_safe():
    """No inventory -> honest zeros with accuracy None (not 0/100%). Items but no
    counts -> counted 0, accuracy unmeasured, no divide-by-zero, no invented currency."""
    empty = stock_accuracy.build_stock_accuracy(_fresh_session(), "DEFAULT")
    assert empty["total_items"] == 0 and empty["counted_items"] == 0
    assert empty["accuracy_rate"] is None              # no counts -> no accuracy, never 0%
    assert empty["count_coverage"] == 0 and empty["worst"] is None
    assert empty["items"] == [] and empty["tone"] == "warn"
    assert "No inventory" in empty["verdict"]
    assert "cost" not in empty and "value" not in empty

    db = _fresh_session()
    _add_items(db, ["A", "B"])                          # a master, but never counted
    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    assert r["total_items"] == 2 and r["counted_items"] == 0
    assert r["accuracy_rate"] is None                  # nothing counted -> unmeasured
    assert r["count_coverage"] == 0
    assert r["net_variance_units"] == 0 and r["abs_variance_units"] == 0
    assert "No cycle counts" in r["verdict"] and r["tone"] == "warn"
    print("PASS empty tables and no-count masters are safe with no invented currency")


def test_healthy_accuracy_reports_good():
    """A well-run programme: high accuracy over a real sample reads good, and the
    parts still reconcile."""
    db = _fresh_session()
    ids = _add_items(db, [f"I{n}" for n in range(10)])   # 10 SKUs
    cc = _count(db, "CC-1")
    # Count all 10; one is off by +2, the rest exact -> 9/10 = 90%? Make it 20/20
    # exact plus... keep it simple: 10 counted, 10 exact -> 100%.
    for c in ids:
        _line(db, cc, ids[c], book=100, physical=100)

    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    assert r["counted_items"] == 10 and r["accurate_items"] == 10
    assert r["accuracy_rate"] == 100 and r["count_coverage"] == 100
    assert r["abs_variance_units"] == 0
    assert r["tone"] == "good", (r["tone"], r["verdict"])
    assert "matched the shelf" in r["verdict"]
    print("PASS a high-accuracy, full-coverage programme reads good")


def test_accuracy_exposes_the_card_contract():
    # The Inventory stock-integrity card (StockIntegrityCards.tsx) renders exactly
    # these keys; pin them so a read-model edit can't silently break the card.
    db = _fresh_session()
    r = stock_accuracy.build_stock_accuracy(db, "DEFAULT")
    for k in ("window_days", "target", "total_items", "counted_items",
              "count_coverage", "accurate_items", "inaccurate_items", "over_items",
              "under_items", "accuracy_rate", "net_variance_units",
              "abs_variance_units", "items", "verdict", "tone"):
        assert k in r, f"stock-accuracy missing card key {k!r}"
    print("PASS stock-accuracy exposes the keys the card renders")


if __name__ == "__main__":
    test_accuracy_split_coverage_and_variance_reconciliation()
    test_variance_recomputed_from_physical_book_not_the_stored_column()
    test_latest_count_wins_and_window_bounds_the_scan()
    test_foreign_item_lines_are_scoped_out()
    test_empty_and_no_counts_are_safe()
    test_healthy_accuracy_reports_good()
    test_accuracy_exposes_the_card_contract()
    print("\nAll stock-accuracy tests passed.")
