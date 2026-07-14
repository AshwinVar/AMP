"""AI agent tests (ADR-0004 / PR #13).

The Maintenance agent must open a maintenance task only when risk is Critical,
do it once (idempotent), and be wired to the maintenance-relevant events.

Run:  python backend/test_agents.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from events import EventBus, ProductionCompleted, DowntimeStarted, InventoryLow
import ai.agents as agents


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _completed(machine_id):
    return ProductionCompleted(tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-1",
                               part_number="P-1", quantity=10, machine_id=machine_id)


def test_agent_opens_task_only_on_critical_risk_idempotently():
    db = _fresh_session()
    # Critical: breakdown (+35) + utilization < 40 (+20) + downtime >= 120 (+25) = 80
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    # healthy machine -> no action
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=65))
    db.commit()

    agents.act_on_machine_event(_completed(2), db)   # not critical
    db.commit()
    assert db.query(models.MaintenanceTask).count() == 0

    agents.act_on_machine_event(_completed(1), db)   # critical -> opens a task
    db.commit()
    tasks = db.query(models.MaintenanceTask).all()
    assert len(tasks) == 1
    assert tasks[0].machine_id == 1
    assert tasks[0].priority == "Critical" and tasks[0].status == "Open"
    assert tasks[0].task_type == "Predictive (auto)"

    # idempotent: a second critical event does not open a duplicate open task
    agents.act_on_machine_event(_completed(1), db)
    db.commit()
    assert db.query(models.MaintenanceTask).count() == 1


def test_reorder_agent_drafts_po_on_low_stock_idempotently():
    db = _fresh_session()
    db.add(models.InventoryItem(id=5, item_code="RM-STEEL-001", item_name="Steel Rod",
                                category="Raw", unit="pcs", current_stock=3, reorder_level=10))
    db.commit()
    low = InventoryLow(tenant_code="DEFAULT", item_id=5, item_code="RM-STEEL-001",
                       item_name="Steel Rod", current_stock=3, reorder_level=10)

    agents.draft_reorder_on_inventory_low(low, db)
    db.commit()
    pos = db.query(models.PurchaseOrder).all()
    assert len(pos) == 1
    assert pos[0].item_id == 5 and pos[0].status == "Draft" and pos[0].supplier_id is None
    assert pos[0].order_quantity == 17 and pos[0].unit == "pcs"   # 2*10 - 3 = 17; unit from the item
    assert pos[0].po_no.startswith("AUTO-PO")

    # idempotent: a second low-stock event doesn't draft a duplicate
    agents.draft_reorder_on_inventory_low(low, db)
    db.commit()
    assert db.query(models.PurchaseOrder).count() == 1


def test_register_wires_agents_to_the_stream():
    bus = EventBus()
    agents.register(bus)
    assert agents.act_on_machine_event in bus._subscribers[ProductionCompleted]
    assert agents.act_on_machine_event in bus._subscribers[DowntimeStarted]
    assert agents.draft_reorder_on_inventory_low in bus._subscribers[InventoryLow]


if __name__ == "__main__":
    test_agent_opens_task_only_on_critical_risk_idempotently()
    test_reorder_agent_drafts_po_on_low_stock_idempotently()
    test_register_wires_agents_to_the_stream()
    print("AGENT OK: Maintenance agent opens a task on Critical risk; Reorder agent drafts a PO on low stock; idempotent; wired")
