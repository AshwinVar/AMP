"""Stock-health read-model tests (ADR-0007).

The capital-tied-up side of inventory over inventory_items + inventory_transactions:
an item with stock but no outbound movement in the window is DEAD; an item whose
current burn gives more than OVERSTOCK_DAYS of cover is OVERSTOCKED; everything
else with stock is healthy and an item with no stock is empty. Covers the state
split and its reconciliation (the counts sum to total, the flagged units sum to
their parts), the independently-derived overstock excess, the window bound, and
the empty / None-field / no-currency edges.

Run:  python backend/test_stock_health.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import stock_health
from ai.stock_health import OVERSTOCK_DAYS, WINDOW_DAYS


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(code, stock, reorder=0, name=None):
    return models.InventoryItem(
        item_code=code, item_name=name or code, category="raw", unit="pcs",
        supplier="Acme", current_stock=stock, reorder_level=reorder)


def _txn(item_id, ttype, qty, days_ago):
    return models.InventoryTransaction(
        item_id=item_id, transaction_type=ttype, quantity=qty,
        created_at=datetime.utcnow() - timedelta(days=days_ago))


def _add_items(db, specs):
    """specs: list of _item(...) — returns {item_code: id} after commit."""
    for it in specs:
        db.add(it)
    db.commit()
    return {i.item_code: i.id for i in db.query(models.InventoryItem).all()}


def test_state_split_and_unit_reconciliation():
    """Six items, one of each shape, with hand-computed expected numbers. The
    per-state counts must sum to total_items and the flagged-unit totals must sum
    to their per-item parts (rule 3)."""
    db = _fresh_session()
    ids = _add_items(db, [
        _item("DEAD-1", stock=500),              # stock, no movement       -> dead (500 idle)
        _item("DEAD-2", stock=200),              # stock, only an inbound    -> dead (200 idle)
        _item("OVER-1", stock=1000),             # 100 out/60d -> 600d cover -> overstocked
        _item("HEAL-1", stock=100),              # 60 out/60d  -> 100d cover -> healthy
        _item("EDGE-1", stock=180),              # 60 out/60d  -> 180d cover -> healthy (not > 180)
        _item("EMPTY-1", stock=0),               # nothing on hand           -> empty
    ])
    # DEAD-2 receives stock but nothing is issued -> still dead (no consumption).
    db.add(_txn(ids["DEAD-2"], "receive", 200, days_ago=5))
    # OVER-1: 100 units issued across the window -> burn 100/60/day, 600 days cover.
    db.add(_txn(ids["OVER-1"], "issue", 100, days_ago=10))
    # HEAL-1: 60 issued -> 1.0/day -> 100 days cover.
    db.add(_txn(ids["HEAL-1"], "issue", 60, days_ago=10))
    # EDGE-1: 60 issued -> 1.0/day -> exactly 180 days cover (boundary is healthy).
    db.add(_txn(ids["EDGE-1"], "out", 60, days_ago=10))
    db.commit()

    r = stock_health.build_stock_health(db, "DEFAULT")

    # Independently-derived state counts.
    assert r["total_items"] == 6, r["total_items"]
    assert r["dead"] == 2, r["dead"]
    assert r["overstocked"] == 1, r["overstocked"]
    assert r["healthy"] == 2, r["healthy"]           # HEAL-1 and the 180d boundary EDGE-1
    assert r["empty"] == 1, r["empty"]
    # Parts sum to the whole (rule 3).
    assert r["dead"] + r["overstocked"] + r["healthy"] + r["empty"] == r["total_items"]

    # Dead capital = the full stock of the two dead items.
    assert r["dead_units"] == 500 + 200, r["dead_units"]

    # OVER-1: burn = 100/60/day; OVERSTOCK_DAYS(180) of cover = 100/60*180 = 300 units;
    # excess = 1000 - 300 = 700. days_of_cover = 1000 / (100/60) = 600.
    over = next(i for i in r["items"] if i["item_code"] == "OVER-1")
    assert over["state"] == "overstocked"
    assert over["days_of_cover"] == 600.0, over["days_of_cover"]
    assert over["flagged_units"] == 700, over["flagged_units"]
    assert r["excess_units"] == 700, r["excess_units"]

    # Flagged totals reconcile with the rows.
    assert r["flagged_items"] == 3
    assert r["flagged_units"] == r["dead_units"] + r["excess_units"] == 1400
    dead_rows = [i for i in r["items"] if i["state"] == "dead"]
    over_rows = [i for i in r["items"] if i["state"] == "overstocked"]
    assert sum(i["flagged_units"] for i in dead_rows) == r["dead_units"]
    assert sum(i["flagged_units"] for i in over_rows) == r["excess_units"]

    # Worst-first: dead leads (rank 0), most idle units first -> DEAD-1 (500).
    assert r["worst"]["item_code"] == "DEAD-1", r["worst"]
    assert r["tone"] == "bad"                         # any dead stock is the loud state
    print("PASS state split, overstock excess and unit reconciliation")


def test_movement_outside_the_window_does_not_rescue_a_dead_item():
    """DEAD means no outbound in the LAST window_days. An issue older than the
    window must not count as recent movement, or a long-idle part reads healthy."""
    db = _fresh_session()
    ids = _add_items(db, [_item("STALE-1", stock=300)])
    # One issue, but WINDOW_DAYS + 30 days ago -> outside the bound.
    db.add(_txn(ids["STALE-1"], "issue", 50, days_ago=WINDOW_DAYS + 30))
    db.commit()

    r = stock_health.build_stock_health(db, "DEFAULT")
    assert r["dead"] == 1, r["dead"]
    assert r["dead_units"] == 300
    row = r["items"][0]
    assert row["state"] == "dead"
    assert row["daily_burn"] == 0.0
    assert row["days_of_cover"] is None              # no burn to divide by
    print("PASS an issue older than the window leaves the item dead")


def test_empty_and_none_fields_are_safe():
    """Empty item master -> honest zeros, no divide-by-zero, no crash. A None
    stock column is treated as 0 (empty), and no currency figure appears anywhere
    in the payload (the schema carries no unit cost)."""
    empty = stock_health.build_stock_health(_fresh_session(), "DEFAULT")
    assert empty["total_items"] == 0
    assert empty["dead"] == empty["overstocked"] == 0
    assert empty["flagged_units"] == 0
    assert empty["worst"] is None
    assert empty["items"] == []
    assert empty["tone"] == "warn"
    assert "No inventory" in empty["verdict"]

    db = _fresh_session()
    _add_items(db, [_item("NULLS-1", stock=None), _item("OK-1", stock=90)])
    r = stock_health.build_stock_health(db, "DEFAULT")
    assert r["total_items"] == 2
    assert r["empty"] == 1                             # None stock -> empty, not a crash
    assert r["dead"] == 1                              # OK-1 has stock, no movement -> dead
    # No currency anywhere in the payload.
    assert "cost" not in r and "value" not in r
    print("PASS empty tables and None fields are safe with no invented currency")


def test_overstock_only_when_no_dead_stock():
    """With nothing dead but a genuinely overstocked item, the headline is the
    overstock warning (not bad), and excess is the units beyond OVERSTOCK_DAYS."""
    db = _fresh_session()
    ids = _add_items(db, [_item("OVER-A", stock=2000)])
    # 200 issued over the window -> burn 200/60/day; OVERSTOCK_DAYS cover = 600 units;
    # excess = 2000 - 600 = 1400.
    db.add(_txn(ids["OVER-A"], "issue", 200, days_ago=15))
    db.commit()

    r = stock_health.build_stock_health(db, "DEFAULT")
    assert r["dead"] == 0
    assert r["overstocked"] == 1
    assert r["excess_units"] == 1400, r["excess_units"]
    assert r["flagged_units"] == 1400
    assert r["tone"] == "warn"
    assert str(OVERSTOCK_DAYS) in r["verdict"]
    print("PASS overstock-only path warns and prices excess in units over target")


def test_health_exposes_the_card_contract():
    # The Inventory stock-integrity card (StockIntegrityCards.tsx) renders exactly
    # these keys; pin them so a read-model edit can't silently break the card.
    db = _fresh_session()
    r = stock_health.build_stock_health(db, "DEFAULT")
    for k in ("window_days", "overstock_days", "total_items", "dead", "overstocked",
              "healthy", "empty", "dead_units", "excess_units", "flagged_items",
              "flagged_units", "worst", "items", "verdict", "tone"):
        assert k in r, f"stock-health missing card key {k!r}"
    print("PASS stock-health exposes the keys the card renders")


if __name__ == "__main__":
    test_state_split_and_unit_reconciliation()
    test_movement_outside_the_window_does_not_rescue_a_dead_item()
    test_empty_and_none_fields_are_safe()
    test_overstock_only_when_no_dead_stock()
    test_health_exposes_the_card_contract()
    print("\nAll stock-health tests passed.")
