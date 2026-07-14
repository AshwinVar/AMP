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
from events import EventBus, ProductionCompleted, DowntimeStarted, InventoryLow, QualityInspectionFailed
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
    # reorder is trusted -> auto-approved by policy (PO advanced, action decided)
    assert pos[0].item_id == 5 and pos[0].status == "Approved" and pos[0].supplier_id is None
    assert pos[0].order_quantity == 17 and pos[0].unit == "pcs"   # 2*10 - 3 = 17; unit from the item
    assert pos[0].po_no.startswith("AUTO-PO")
    actions = db.query(models.AgentAction).filter_by(ref_kind="purchase_order").all()
    assert len(actions) == 1 and actions[0].agent == "reorder"
    assert actions[0].status == "Approved" and actions[0].decided_by == "auto-policy"

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


def test_quality_agent_proposes_inspection_on_high_fail_rate():
    db = _fresh_session()
    db.add(models.Machine(id=4, name="CNC-04", status="Running", utilization=70))
    db.commit()
    high = QualityInspectionFailed(tenant_code="DEFAULT", inspection_no="QC-9",
                                   failed_quantity=12, inspected_quantity=80,   # 15% >= 10%
                                   machine_id=4, defect_category="surface")
    agents.inspect_on_quality_failed(high, db)
    db.commit()
    tasks = db.query(models.MaintenanceTask).filter_by(task_type="Quality (auto)").all()
    assert len(tasks) == 1 and tasks[0].machine_id == 4 and tasks[0].status == "Proposed"
    actions = db.query(models.AgentAction).filter_by(agent="quality").all()
    assert len(actions) == 1 and actions[0].status == "Proposed"   # quality is NOT auto-approved

    # below the threshold -> no action
    low = QualityInspectionFailed(tenant_code="DEFAULT", inspection_no="QC-10",
                                  failed_quantity=1, inspected_quantity=100, machine_id=4)
    agents.inspect_on_quality_failed(low, db)
    db.commit()
    assert db.query(models.MaintenanceTask).filter_by(task_type="Quality (auto)").count() == 1


def test_proposal_notifies_but_auto_approved_does_not():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    db.add(models.InventoryItem(id=5, item_code="RM-1", item_name="Steel", category="Raw",
                                unit="pcs", current_stock=3, reorder_level=10))
    db.commit()

    # maintenance agent proposes (Critical, pending approval) -> a notification
    agents.act_on_machine_event(_completed(1), db)
    db.commit()
    assert db.query(models.Notification).filter_by(notification_type="agent_proposal").count() == 1

    # reorder agent auto-approves -> no new notification (trusted, no human needed)
    agents.draft_reorder_on_inventory_low(
        InventoryLow(tenant_code="DEFAULT", item_id=5, item_code="RM-1", item_name="Steel",
                     current_stock=3, reorder_level=10), db)
    db.commit()
    assert db.query(models.Notification).filter_by(notification_type="agent_proposal").count() == 1


def test_auto_approve_policy_is_env_configurable():
    import os as _os

    class _A:
        pass

    a = _A()
    _os.environ.pop("AUTO_APPROVE_AGENTS", None)
    a.agent = "reorder"; assert agents.should_auto_approve(a) is True      # default trusts reorder
    a.agent = "quality"; assert agents.should_auto_approve(a) is False     # default gates quality
    _os.environ["AUTO_APPROVE_AGENTS"] = "reorder,quality"
    a.agent = "quality"; assert agents.should_auto_approve(a) is True      # now trusted via config
    a.agent = "maintenance"; assert agents.should_auto_approve(a) is False
    _os.environ.pop("AUTO_APPROVE_AGENTS", None)                           # cleanup


def test_register_wires_agents_to_the_stream():
    bus = EventBus()
    agents.register(bus)
    assert agents.act_on_machine_event in bus._subscribers[ProductionCompleted]
    assert agents.act_on_machine_event in bus._subscribers[DowntimeStarted]
    assert agents.inspect_on_quality_failed in bus._subscribers[QualityInspectionFailed]
    assert agents.draft_reorder_on_inventory_low in bus._subscribers[InventoryLow]


if __name__ == "__main__":
    test_agent_opens_task_only_on_critical_risk_idempotently()
    test_reorder_agent_drafts_po_on_low_stock_idempotently()
    test_quality_agent_proposes_inspection_on_high_fail_rate()
    test_approve_and_reject_agent_actions()
    test_proposal_notifies_but_auto_approved_does_not()
    test_auto_approve_policy_is_env_configurable()
    test_register_wires_agents_to_the_stream()
    print("AGENT OK: 3 agents propose; reorder auto-approves, maintenance/quality wait + notify; approve/reject; idempotent; wired")
