"""Runtime parity test for the domain event bus (ADR-0001 / PR #1).

Proves the event-driven path produces the SAME inventory movement the old
inline work-order handler did — plus the new append-only ``event_log`` row.
Uses an in-memory SQLite DB, so it is deterministic and needs no seeded
backend or running server.

Run:  python backend/test_event_bus.py     (exit 0 = pass)
Also collectable by pytest.
"""
import json

from sqlalchemy import create_engine, text
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


def _null_column(db, item_code, column):
    """Force a REAL NULL into a nullable stock column. The ORM's Column(default=0)
    fires even when you pass current_stock=None on insert, so the row would store
    0, not NULL — the crash we're guarding against only arises from a raw-SQL /
    migration / cleared-field write. Reproduce that exactly (the same technique
    the costing / copilot / analytics null-safety tests use), then expire the
    identity map so the handler re-reads the NULL from the DB."""
    db.execute(text(f"UPDATE inventory_items SET {column}=NULL WHERE item_code=:c"),
               {"c": item_code})
    db.commit()
    db.expire_all()


def test_null_raw_stock_does_not_crash_completion():
    """A NULL raw current_stock must not 500 the work-order completion.

    current_stock / reorder_level are nullable Integer columns; a raw-SQL /
    migration / cleared-field write can store NULL. The BOM subscriber runs
    inside the completion transaction and a subscriber error propagates by
    design, so `min(int, None)` / `None - int` would surface as an unhandled
    500. An empty shelf can issue nothing: consume 0, stock stays 0.
    """
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                category="Raw", unit="kg", current_stock=50, reorder_level=10))
    db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                category="Finished", unit="pcs", current_stock=5, reorder_level=0))
    db.commit()
    _null_column(db, "RM-STEEL-001", "current_stock")   # force a real NULL

    bus = EventBus()
    subscribers.register(bus)
    bus.publish(ProductionCompleted(
        tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-NULLRAW",
        part_number="SHAFT-001", quantity=5, machine_id=1), db)
    db.commit()

    raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
    fg = db.query(models.InventoryItem).filter_by(item_code="FG-SHAFT-001").first()
    issue = db.query(models.InventoryTransaction).filter_by(transaction_type="Issue").first()
    # NULL stock -> 0; wants 5*2=10 but can only issue what's there (0).
    assert raw.current_stock == 0, raw.current_stock
    assert issue and issue.quantity == 0, issue and issue.quantity
    # Consumption can't trip reorder from an empty shelf: no InventoryLow raised.
    low = db.query(models.EventLog).filter_by(event_type="InventoryLow").all()
    assert low == [], low
    # Finished-goods receive is unaffected: 5 + 5 = 10.
    assert fg.current_stock == 10, fg.current_stock


def test_null_finished_goods_stock_does_not_crash_completion():
    """A NULL finished-goods current_stock must not 500 the completion either;
    `None + qty` is a TypeError. Treat the missing stock as 0, then add qty."""
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                category="Raw", unit="kg", current_stock=100, reorder_level=10))
    db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                category="Finished", unit="pcs", current_stock=5, reorder_level=0))
    db.commit()
    _null_column(db, "FG-SHAFT-001", "current_stock")   # force a real NULL

    bus = EventBus()
    subscribers.register(bus)
    bus.publish(ProductionCompleted(
        tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-NULLFG",
        part_number="SHAFT-001", quantity=5, machine_id=1), db)
    db.commit()

    raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
    fg = db.query(models.InventoryItem).filter_by(item_code="FG-SHAFT-001").first()
    assert raw.current_stock == 90, raw.current_stock            # 100 - 5*2
    assert fg.current_stock == 5, fg.current_stock               # None(->0) + 5


def test_null_reorder_level_does_not_crash_completion():
    """A NULL reorder_level must not crash the `was_above` / reorder comparison
    (`int > None`). It coalesces to 0, so a non-empty shelf reads as above it."""
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                category="Raw", unit="kg", current_stock=100, reorder_level=10))
    db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                category="Finished", unit="pcs", current_stock=5, reorder_level=0))
    db.commit()
    _null_column(db, "RM-STEEL-001", "reorder_level")   # force a real NULL

    bus = EventBus()
    subscribers.register(bus)
    bus.publish(ProductionCompleted(
        tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-NULLLVL",
        part_number="SHAFT-001", quantity=5, machine_id=1), db)
    db.commit()

    raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
    assert raw.current_stock == 90, raw.current_stock            # 100 - 5*2
    # 90 <= 0 is False, so consumption did not trip a reorder.
    low = db.query(models.EventLog).filter_by(event_type="InventoryLow").all()
    assert low == [], low


def test_consumption_tripping_reorder_still_raises_inventory_low():
    """Regression guard: the coalescing must not change the happy-path behaviour
    where consumption crosses the reorder level and raises InventoryLow. Numbers
    independently derived: 12 in stock, consume 5*2=10 -> 2 left, 2 <= 10 reorder."""
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="RM-STEEL-001", item_name="Steel",
                                category="Raw", unit="kg", current_stock=12, reorder_level=10))
    db.add(models.InventoryItem(item_code="FG-SHAFT-001", item_name="Shaft",
                                category="Finished", unit="pcs", current_stock=0, reorder_level=0))
    db.commit()

    bus = EventBus()
    subscribers.register(bus)
    bus.publish(ProductionCompleted(
        tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-TRIP",
        part_number="SHAFT-001", quantity=5, machine_id=1), db)
    db.commit()

    raw = db.query(models.InventoryItem).filter_by(item_code="RM-STEEL-001").first()
    issue = db.query(models.InventoryTransaction).filter_by(transaction_type="Issue").first()
    assert raw.current_stock == 2, raw.current_stock             # 12 - 10
    # The issue transaction reconciles with the actual stock delta (12 -> 2 = 10).
    assert issue.quantity == 10, issue.quantity
    low = db.query(models.EventLog).filter_by(event_type="InventoryLow").all()
    assert len(low) == 1, low
    payload = json.loads(low[0].payload)
    assert payload["current_stock"] == 2 and payload["reorder_level"] == 10, payload


if __name__ == "__main__":
    r, f = test_production_completed_moves_bom_and_logs_event()
    print(f"PARITY OK: steel 100->{r} (-10), finished 5->{f} (+5), +2 txns, +1 event_log row")
    test_null_raw_stock_does_not_crash_completion()
    print("NULL-RAW OK: empty shelf issues 0, no crash, no false reorder")
    test_null_finished_goods_stock_does_not_crash_completion()
    print("NULL-FG OK: missing finished stock treated as 0 before receive")
    test_null_reorder_level_does_not_crash_completion()
    print("NULL-REORDER OK: reorder comparison survives a NULL level")
    test_consumption_tripping_reorder_still_raises_inventory_low()
    print("REORDER-TRIP OK: consumption crossing reorder still raises InventoryLow")
    print("ALL SUBSCRIBER BOM TESTS PASSED")
