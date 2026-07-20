"""Order-book CSV export test.

The tenant's customer orders rendered as CSV (header + a row per order), ready
to open in Excel. Run:  python backend/test_orders_export.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import orders_routes
import models
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_orders_export_renders_csv():
    db = _fresh_session()
    db.add(models.CustomerOrder(order_no="BUG-1", customer_name="Bugatti", product_name="CLB-PCB",
                                order_quantity=100, dispatched_quantity=40, status="Pending",
                                priority="High", due_date=date(2026, 8, 1)))
    db.add(models.CustomerOrder(order_no="MER-1", customer_name="Mercedes", product_name="CLB-GAUGE",
                                order_quantity=50, dispatched_quantity=50, status="Delivered",
                                priority="Medium", due_date=date(2026, 7, 20)))
    db.commit()

    csv_text = orders_routes._orders_csv(db)
    lines = csv_text.strip().splitlines()
    # header + 2 orders
    assert lines[0].startswith("Order No,Customer,Product")
    assert len(lines) == 3
    # sorted by due date -> Mercedes (Jul 20) before Bugatti (Aug 1); rows carry the data
    assert "MER-1,Mercedes,CLB-GAUGE,50,50,Delivered,Medium,2026-07-20" in csv_text
    assert "BUG-1,Bugatti,CLB-PCB,100,40,Pending,High,2026-08-01" in csv_text

    # empty book -> just the header, no crash
    assert orders_routes._orders_csv(_fresh_session()).strip().splitlines() == [
        "Order No,Customer,Product,Order Qty,Dispatched Qty,Status,Priority,Due Date"
    ]


if __name__ == "__main__":
    test_orders_export_renders_csv()
    print("ORDERS EXPORT OK: order book rendered as CSV (header + a row per order, due-date sorted); empty-safe")
