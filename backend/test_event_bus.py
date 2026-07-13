"""Runtime parity test for the domain event bus (ADR-0001 / PR #1).

Proves the event-driven path produces the SAME inventory movement the old
inline work-order handler did — plus the new append-only ``event_log`` row.
Uses an in-memory SQLite DB, so it is deterministic and needs no seeded
backend or running server.

Run:  python backend/test_event_bus.py     (exit 0 = pass)
Also collectable by pytest.
"""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from events import EventBus, ProductionCompleted
import subscribers


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_production_completed_moves_bom_and_logs_event():
    db = _fresh_session()
    # SHAFT-001 recipe: 2x RM-STEEL-001 -> FG-SHAFT-001
    db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                category="Raw", unit="kg", current_stock=100, reorder_level=10))
    db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                category="Finished", unit="pcs", current_stock=5, reorder_level=0))
    db.commit()

    # Isolated bus with exactly the one subscriber under test
    bus = EventBus()
    subscribers.register(bus)

    bus.publish(ProductionCompleted(
        tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-TEST",
        part_number="SHAFT-001", quantity=5, machine_id=1), db)
    db.commit()

    raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
    fg = db.query(models.InventoryItem).filter_by(item_code="FG-SHAFT-001").first()
    issue = db.query(models.InventoryTransaction).filter_by(transaction_type="Issue").first()
    receive = db.query(models.InventoryTransaction).filter_by(transaction_type="Receive").first()
    logs = db.query(models.EventLog).all()

    # Inventory movement identical to the old inline handler
    assert raw.current_stock == 90, raw.current_stock           # 100 - 5*2
    assert fg.current_stock == 10, fg.current_stock             # 5 + 5
    assert issue and issue.quantity == 10 and issue.reference == "WO-TEST"
    assert receive and receive.quantity == 5 and receive.reference == "WO-TEST"
    # New: the event was recorded to the append-only log
    assert len(logs) == 1 and logs[0].event_type == "ProductionCompleted"
    assert json.loads(logs[0].payload)["work_order_no"] == "WO-TEST"
    return raw.current_stock, fg.current_stock


if __name__ == "__main__":
    r, f = test_production_completed_moves_bom_and_logs_event()
    print(f"PARITY OK: steel 100->{r} (-10), finished 5->{f} (+5), +2 txns, +1 event_log row")
