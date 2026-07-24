"""Order delivery outlook read-model tests (ADR-0007).

Classifies customer orders into delivered / on-track / at-risk / late from what's
dispatched vs due, rolls up per customer (worst first), and lists the orders to
chase. Run:  python backend/test_delivery.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import delivery


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _order(no, customer, qty, dispatched, due_offset_days, status="Pending", product="CLB-PCB"):
    return models.CustomerOrder(
        order_no=no, customer_name=customer, product_name=product,
        order_quantity=qty, dispatched_quantity=dispatched, status=status,
        due_date=(datetime.utcnow().date() + timedelta(days=due_offset_days)),
    )


def test_delivery_classifies_orders_and_rolls_up_by_customer():
    db = _fresh_session()
    db.add_all([
        # Bugatti: one delivered (full dispatch), one late (overdue, short)
        _order("BUG-1", "Bugatti", 100, 100, 5),               # delivered (dispatched in full)
        _order("BUG-2", "Bugatti", 100, 20, -2),               # late (2 days overdue)
        # Mercedes: one at-risk (due in 2 days), one on-track (due in 30)
        _order("MER-1", "Mercedes", 100, 0, 2),                # at_risk (<= 3 days)
        _order("MER-2", "Mercedes", 100, 50, 30),              # on_track
        _order("MER-3", "Mercedes", 100, 100, -10, status="Delivered"),  # delivered by status though overdue date
    ])
    db.commit()

    s = delivery.build_delivery_summary(db, "DEFAULT")
    assert s["total"] == 5
    assert s["delivered"] == 2 and s["late"] == 1 and s["at_risk"] == 1 and s["on_track"] == 1
    # unit fulfillment: dispatched 270 of 500 ordered = 54%
    assert s["fulfillment_rate"] == 54
    # on-track rate: 2 delivered + 1 on-track of 5 orders = 60%
    assert s["on_track_rate"] == 60
    # delivery reliability: of the orders that have come due (2 delivered + 1 late),
    # 2 delivered = 67%. Not-yet-due orders (on-track / at-risk) are held out of the
    # denominator, so this is NOT (total - late)/total = (5-1)/5 = 80%.
    assert s["resolved"] == 3 and s["reliability_rate"] == 67
    assert s["reliability_rate"] != round((s["total"] - s["late"]) / s["total"] * 100)
    # the plant-wide reliability reconciles with the sum of each customer's due orders
    by_c = {c["customer"]: c for c in s["by_customer"]}
    due = sum(c["delivered"] + c["late"] for c in by_c.values())
    delivered = sum(c["delivered"] for c in by_c.values())
    assert due == s["resolved"] and round(delivered / due * 100) == s["reliability_rate"]
    # unit totals: 500 ordered, 270 dispatched, 230 remaining
    assert s["units_ordered"] == 500 and s["units_dispatched"] == 270 and s["units_remaining"] == 230
    # units at risk: undelivered on the late (BUG-2: 80) + at-risk (MER-1: 100) orders
    assert s["units_at_risk"] == 180

    by = {c["customer"]: c for c in s["by_customer"]}
    assert by["Bugatti"]["orders"] == 2 and by["Bugatti"]["delivered"] == 1 and by["Bugatti"]["late"] == 1
    assert by["Mercedes"]["orders"] == 3 and by["Mercedes"]["at_risk"] == 1
    # worst-first: Bugatti (has a late order) sorts before Mercedes
    assert s["by_customer"][0]["customer"] == "Bugatti"

    # chase list: late first (BUG-2), then at-risk (MER-1); delivered/on-track excluded
    chase = s["at_risk_orders"]
    assert [o["order_no"] for o in chase] == ["BUG-2", "MER-1"]
    assert chase[0]["state"] == "late" and chase[0]["days_to_due"] == -2

    # upcoming due load: 7 forward days; only MER-1 (due in 2 days, undelivered) lands
    assert len(s["upcoming"]) == 7
    assert sum(u["orders"] for u in s["upcoming"]) == 1 and s["upcoming"][2]["orders"] == 1

    # empty order book -> zeros, no divide-by-zero
    empty = delivery.build_delivery_summary(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["fulfillment_rate"] == 0 and empty["at_risk_orders"] == []
    assert empty["on_track_rate"] == 0 and empty["units_at_risk"] == 0 and empty["units_remaining"] == 0
    assert empty["resolved"] == 0 and empty["reliability_rate"] == 0   # no divide-by-zero


def test_customer_detail_scopes_and_scores_one_customer():
    db = _fresh_session()
    db.add_all([
        # Bugatti: one delivered-in-full, one overdue-and-short, one at-risk.
        _order("BUG-1", "Bugatti", 100, 100, 5),    # delivered
        _order("BUG-2", "Bugatti", 100, 20, -2),    # late (80 units still owed)
        _order("BUG-3", "Bugatti", 100, 0, 2),      # at_risk (due in 2 days)
        # Mercedes' order must not bleed into Bugatti's drill-down.
        _order("MER-9", "Mercedes", 100, 0, 1),
    ])
    db.commit()

    d = delivery.build_customer_detail(db, "DEFAULT", "Bugatti")
    assert d["customer"] == "Bugatti"
    assert d["total"] == 3                                  # MER-9 (Mercedes) excluded
    assert d["delivered"] == 1 and d["late"] == 1 and d["at_risk"] == 1 and d["on_track"] == 0
    # unit fulfillment: 120 of 300 ordered = 40%
    assert d["fulfillment_rate"] == 40
    # reliability: of the due orders (1 delivered + 1 late), 1 delivered in full = 50%
    assert d["reliability_rate"] == 50
    assert d["overdue_units"] == 80                         # 100 - 20 on the late order
    # chase list: late (BUG-2) first, then at-risk (BUG-3); delivered excluded
    assert [o["order_no"] for o in d["chase"]] == ["BUG-2", "BUG-3"]
    # upcoming: BUG-3 (due in 2 days, undelivered) lands; delivered/overdue don't
    assert len(d["upcoming"]) == 7 and sum(u["orders"] for u in d["upcoming"]) == 1
    # recent lists all three orders, each with its state
    assert len(d["recent"]) == 3
    assert {r["order_no"] for r in d["recent"]} == {"BUG-1", "BUG-2", "BUG-3"}


def test_customer_detail_is_empty_safe_for_unknown_customer():
    db = _fresh_session()
    db.add(_order("BUG-1", "Bugatti", 100, 100, 5))
    db.commit()
    d = delivery.build_customer_detail(db, "DEFAULT", "Nonexistent")
    assert d["total"] == 0
    assert d["fulfillment_rate"] == 0 and d["reliability_rate"] == 0   # no divide-by-zero
    assert d["chase"] == [] and d["recent"] == []


if __name__ == "__main__":
    test_delivery_classifies_orders_and_rolls_up_by_customer()
    test_customer_detail_scopes_and_scores_one_customer()
    test_customer_detail_is_empty_safe_for_unknown_customer()
    print("DELIVERY OK: orders classified delivered/on-track/at-risk/late; unit fulfillment; "
          "per-customer rollup (worst first); chase list (late then at-risk); empty-safe; "
          "customer drill-down scopes to one customer (fulfillment, reliability, overdue "
          "units, chase, upcoming, recent) and is empty-safe")
