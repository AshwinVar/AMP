"""Days-of-cover read-model tests (ADR-0007).

Rate-based stockout forecast over inventory_items + inventory_transactions:
recent outbound consumption becomes a daily burn, stock becomes days of cover,
and the items that run dry soonest surface first. Covers both seed vocabularies
(routes "Issue" / simulator "OUT"), the window bound, empty stock, no-burn stock,
and the reorder-first ordering.

Also covers the part drill-down: it reconciles with the summary row, shows the
daily in/out movement, and judges whether the open POs land before the projected
stockout (covered / arrives too late / nothing on order).

Run:  python backend/test_coverage.py     (exit 0 = pass)
"""
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import coverage


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(code, stock, name=None):
    return models.InventoryItem(
        item_code=code, item_name=name or code, category="raw", unit="pcs",
        supplier="Acme", current_stock=stock, reorder_level=0)


def _txn(item_id, ttype, qty, days_ago):
    return models.InventoryTransaction(
        item_id=item_id, transaction_type=ttype, quantity=qty,
        created_at=datetime.utcnow() - timedelta(days=days_ago))


def test_barely_moving_part_does_not_overflow_the_stockout_date():
    """A big pile of a slow-moving part computes centuries of cover. Adding that to
    a date raised OverflowError -> a 500 on both the summary and the part
    drill-down. Past the forecast horizon there is simply no dated stockout."""
    db = _fresh_session()
    db.add(_item("SLOW-1", 5_000_000))          # huge stock...
    db.commit()
    item = db.query(models.InventoryItem).first()
    db.add(_txn(item.id, "issue", 1, 3))        # ...one unit issued in the window
    db.commit()

    coverage.build_coverage_summary(db, "DEFAULT")               # must not raise

    part = coverage.build_part_runway(db, "DEFAULT", "SLOW-1")   # must not raise
    assert part["current_stock"] == 5_000_000
    assert part["days_of_cover"] > 3650          # centuries of cover...
    assert part["stockout_date"] is None         # ...so no meaningful dated stockout
    print("PASS a barely-moving part yields no dated stockout instead of overflowing")


def test_coverage_forecasts_stockout_and_ranks_soonest_first():
    db = _fresh_session()
    db.add_all([
        _item("FAST-01", stock=70),    # id 1: high burn, ~7 days cover -> critical
        _item("SLOW-02", stock=280),   # id 2: same burn, ~28 days cover -> ok (above watch)
        _item("MID-03", stock=140),    # id 3: burn -> ~14 days cover -> watch
        _item("DEAD-04", stock=500),   # id 4: stock but no recent burn -> ok
        _item("EMPTY-05", stock=0),    # id 5: already out
    ])
    db.commit()
    # 140 units out over the 14-day window = 10 units/day for the first three.
    db.add_all([
        _txn(1, "OUT", 140, days_ago=3),      # simulator vocabulary
        _txn(2, "Issue", 140, days_ago=5),    # routes vocabulary -> both must count
        _txn(3, "OUT", 70, days_ago=2),
        _txn(3, "OUT", 70, days_ago=6),
        # Noise that must NOT reduce cover: inbound + adjustments + out-of-window.
        _txn(1, "IN", 1000, days_ago=1),
        _txn(2, "Adjust", 500, days_ago=1),
        _txn(3, "OUT", 999, days_ago=40),     # older than the window -> ignored
        _txn(4, "Receive", 50, days_ago=2),
    ])
    db.commit()

    s = coverage.build_coverage_summary(db, "DEFAULT")
    assert s["total_items"] == 5
    assert s["out_of_stock"] == 1                 # EMPTY-05
    assert s["critical"] == 1                     # FAST-01 (~7 days)
    assert s["watch"] == 1                        # MID-03 (~14 days)
    assert s["running_out"] == 2                  # out + critical (EMPTY + FAST)

    # Reorder list excludes the healthy/dormant items (SLOW-02, DEAD-04).
    codes = [r["item_code"] for r in s["items"]]
    assert codes == ["EMPTY-05", "FAST-01", "MID-03"]  # soonest dry first
    assert "SLOW-02" not in codes and "DEAD-04" not in codes

    empty = s["items"][0]
    assert empty["state"] == "out" and empty["days_of_cover"] == 0

    fast = s["items"][1]
    assert fast["state"] == "critical"
    assert fast["daily_burn"] == 10.0            # 140 / 14 days
    assert fast["days_of_cover"] == 7.0          # 70 / 10
    # Projected stockout is dated one week out (int days added to today).
    assert fast["stockout_date"] == (datetime.utcnow().date() + timedelta(days=7)).isoformat()

    # No inventory at all -> zeros, no crash.
    e = coverage.build_coverage_summary(_fresh_session(), "DEFAULT")
    assert e["total_items"] == 0 and e["running_out"] == 0 and e["items"] == []


def test_stock_with_no_recent_consumption_is_not_a_runway_risk():
    db = _fresh_session()
    db.add(_item("DORMANT-01", stock=200))
    db.commit()
    s = coverage.build_coverage_summary(db, "DEFAULT")
    assert s["running_out"] == 0
    assert s["items"] == []                       # has stock, no burn -> ok, not surfaced


def _po(po_no, item_id, qty, received, due_in_days, status="Open", supplier_id=1):
    return models.PurchaseOrder(
        po_no=po_no, supplier_id=supplier_id, item_id=item_id, item_name="part",
        order_quantity=qty, received_quantity=received, unit="pcs",
        expected_delivery_date=date.today() + timedelta(days=due_in_days), status=status)


def _burning_part(db):
    """One critical part: 70 in stock, 140 out over the window = 10/day -> 7 days cover."""
    db.add(models.Supplier(supplier_code="SUP-1", supplier_name="Acme",
                           category="raw", status="Active"))
    db.add(_item("FAST-01", stock=70, name="Solder paste"))
    db.commit()
    db.add_all([
        _txn(1, "OUT", 140, days_ago=3),
        _txn(1, "IN", 40, days_ago=6),        # a receipt: shown, but never lowers the burn
        _txn(1, "OUT", 999, days_ago=40),     # outside the window -> ignored
    ])
    db.commit()


def test_part_runway_reconciles_with_the_summary_row():
    db = _fresh_session()
    _burning_part(db)

    row = next(r for r in coverage.build_coverage_summary(db, "DEFAULT")["items"]
               if r["item_code"] == "FAST-01")
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")

    assert p["found"] is True
    assert p["item_name"] == "Solder paste"
    # The drill-down must not tell a different story from the card it opened.
    for k in ("current_stock", "daily_burn", "days_of_cover", "stockout_date", "state"):
        assert p[k] == row[k], f"{k}: drill-down {p[k]} != summary {row[k]}"
    assert p["daily_burn"] == 10.0 and p["days_of_cover"] == 7.0

    # Window movement: consumption drives the burn, receipts are reported separately.
    assert p["consumed_units"] == 140 and p["received_units"] == 40
    assert len(p["daily"]) == coverage.WINDOW_DAYS
    assert sum(d["out"] for d in p["daily"]) == 140
    assert sum(d["in"] for d in p["daily"]) == 40
    assert p["daily"][-1]["date"] == datetime.utcnow().date().isoformat()   # today last

    # Recent movements, newest first, with a direction.
    assert [m["direction"] for m in p["recent"]] == ["out", "in"]


def test_inbound_po_covers_or_arrives_too_late():
    # Arrives day 4, runs dry day 7 -> covered.
    db = _fresh_session()
    _burning_part(db)
    db.add(_po("PO-1", item_id=1, qty=500, received=0, due_in_days=4))
    db.commit()
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")
    assert p["cover_verdict"] == "covered"
    assert p["days_uncovered"] is None
    assert p["inbound_units"] == 500
    assert p["inbound"][0]["arrives_before_stockout"] is True

    # Arrives day 12, runs dry day 7 -> five days uncovered.
    db = _fresh_session()
    _burning_part(db)
    db.add(_po("PO-2", item_id=1, qty=500, received=0, due_in_days=12))
    db.commit()
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")
    assert p["cover_verdict"] == "late_cover"
    assert p["days_uncovered"] == 5
    assert p["inbound"][0]["arrives_before_stockout"] is False

    # Nothing on order at all -> the buyer has to act.
    db = _fresh_session()
    _burning_part(db)
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")
    assert p["cover_verdict"] == "no_inbound" and p["inbound"] == []


def test_received_pos_are_not_counted_as_future_cover():
    db = _fresh_session()
    _burning_part(db)
    db.add_all([
        _po("PO-DONE", item_id=1, qty=500, received=500, due_in_days=-10),  # fully received
        _po("PO-OTHER", item_id=2, qty=900, received=0, due_in_days=1),     # a different part
    ])
    db.commit()
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")
    assert p["inbound"] == [] and p["inbound_units"] == 0
    assert p["cover_verdict"] == "no_inbound"

    # An overdue, still-short PO is outstanding cover — and can't land before today.
    db = _fresh_session()
    _burning_part(db)
    db.add(_po("PO-LATE", item_id=1, qty=500, received=100, due_in_days=-3))
    db.commit()
    p = coverage.build_part_runway(db, "DEFAULT", "FAST-01")
    assert p["inbound"][0]["state"] == "late"
    assert p["inbound"][0]["outstanding"] == 400        # 500 ordered - 100 in
    assert p["cover_verdict"] == "covered"              # today <= the day-7 stockout


def test_part_runway_is_healthy_and_missing_safe():
    db = _fresh_session()
    db.add(_item("DORMANT-01", stock=200))
    db.commit()
    p = coverage.build_part_runway(db, "DEFAULT", "DORMANT-01")
    assert p["found"] is True
    assert p["state"] == "ok" and p["days_of_cover"] is None
    assert p["cover_verdict"] == "not_at_risk"          # no burn -> no cover question

    missing = coverage.build_part_runway(db, "DEFAULT", "NOPE-99")
    assert missing["found"] is False
    assert missing["inbound"] == [] and missing["daily"] == [] and missing["recent"] == []


if __name__ == "__main__":
    test_barely_moving_part_does_not_overflow_the_stockout_date()
    test_coverage_forecasts_stockout_and_ranks_soonest_first()
    test_stock_with_no_recent_consumption_is_not_a_runway_risk()
    test_part_runway_reconciles_with_the_summary_row()
    test_inbound_po_covers_or_arrives_too_late()
    test_received_pos_are_not_counted_as_future_cover()
    test_part_runway_is_healthy_and_missing_safe()
    print("COVERAGE OK: burn-rate days-of-cover, both txn vocabularies, window bound, "
          "soonest-dry-first ordering, empty/dormant-safe; part drill-down reconciles "
          "with the summary row and judges inbound cover (covered / late / none)")
