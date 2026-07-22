"""Days-of-cover read-model tests (ADR-0007).

Rate-based stockout forecast over inventory_items + inventory_transactions:
recent outbound consumption becomes a daily burn, stock becomes days of cover,
and the items that run dry soonest surface first. Covers both seed vocabularies
(routes "Issue" / simulator "OUT"), the window bound, empty stock, no-burn stock,
and the reorder-first ordering.

Run:  python backend/test_coverage.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

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


if __name__ == "__main__":
    test_coverage_forecasts_stockout_and_ranks_soonest_first()
    test_stock_with_no_recent_consumption_is_not_a_runway_risk()
    print("COVERAGE OK: burn-rate days-of-cover, both txn vocabularies, window bound, "
          "soonest-dry-first ordering, empty/dormant-safe")
