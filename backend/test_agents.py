"""AI agent tests (ADR-0004 / PR #13).

The Maintenance agent must open a maintenance task only when risk is Critical,
do it once (idempotent), and be wired to the maintenance-relevant events.

Run:  python backend/test_agents.py     (exit 0 = pass)
"""
from datetime import date

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
    assert tasks[0].priority == "Critical" and tasks[0].status == "Proposed"   # pending approval
    assert tasks[0].task_type == "Predictive (auto)"
    # and it logged a proposed AgentAction (the audit trail / approval queue)
    actions = db.query(models.AgentAction).filter_by(ref_kind="maintenance_task").all()
    assert len(actions) == 1
    assert actions[0].agent == "maintenance" and actions[0].status == "Proposed" and actions[0].ref_id == tasks[0].id

    # idempotent: a second critical event does not propose a duplicate
    agents.act_on_machine_event(_completed(1), db)
    db.commit()
    assert db.query(models.MaintenanceTask).count() == 1
    assert db.query(models.AgentAction).count() == 1


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
    # and it logged a proposed AgentAction
    actions = db.query(models.AgentAction).filter_by(ref_kind="purchase_order").all()
    assert len(actions) == 1 and actions[0].agent == "reorder" and actions[0].ref_id == pos[0].id

    # idempotent: a second low-stock event doesn't draft a duplicate
    agents.draft_reorder_on_inventory_low(low, db)
    db.commit()
    assert db.query(models.PurchaseOrder).count() == 1
    assert db.query(models.AgentAction).count() == 1


def test_approve_and_reject_agent_actions():
    db = _fresh_session()
    db.add(models.MaintenanceTask(id=1, task_no="AUTO-MAINT-1-1", machine_id=1,
                                  task_type="Predictive (auto)", priority="Critical",
                                  assigned_to="Maintenance team", planned_date=date.today(), status="Proposed"))
    db.add(models.AgentAction(id=1, tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="Open a task", ref_kind="maintenance_task", ref_id=1, status="Proposed"))
    db.add(models.PurchaseOrder(id=1, po_no="AUTO-PO-5-1", item_id=5, item_name="Steel Rod",
                                order_quantity=17, unit="pcs", expected_delivery_date=date.today(), status="Draft"))
    db.add(models.AgentAction(id=2, tenant_code="DEFAULT", agent="reorder", action_type="draft_po",
                              summary="Draft a PO", ref_kind="purchase_order", ref_id=1, status="Proposed"))
    db.commit()

    task_action = db.query(models.AgentAction).filter_by(id=1).first()
    agents.apply_decision(db, task_action, "approve", decided_by="alice")
    db.commit()
    assert task_action.status == "Approved" and task_action.decided_by == "alice"
    assert db.query(models.MaintenanceTask).filter_by(id=1).first().status == "Open"   # task went live

    po_action = db.query(models.AgentAction).filter_by(id=2).first()
    agents.apply_decision(db, po_action, "reject", decided_by="bob")
    db.commit()
    assert po_action.status == "Rejected"
    assert db.query(models.PurchaseOrder).filter_by(id=1).first().status == "Cancelled"  # PO cancelled


def test_register_wires_agents_to_the_stream():
    bus = EventBus()
    agents.register(bus)
    assert agents.act_on_machine_event in bus._subscribers[ProductionCompleted]
    assert agents.act_on_machine_event in bus._subscribers[DowntimeStarted]
    assert agents.draft_reorder_on_inventory_low in bus._subscribers[InventoryLow]


if __name__ == "__main__":
    test_agent_opens_task_only_on_critical_risk_idempotently()
    test_reorder_agent_drafts_po_on_low_stock_idempotently()
    test_approve_and_reject_agent_actions()
    test_register_wires_agents_to_the_stream()
    print("AGENT OK: agents propose (task/PO pending) + log AgentActions; approve advances, reject cancels; idempotent; wired")
