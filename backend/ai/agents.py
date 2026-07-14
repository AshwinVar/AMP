"""AI agents — propose, log, auto-approve trusted actions, act on approval
(ADR-0004, ADR-0005).

Agents observe the event stream and PROPOSE bounded actions: they create the item
in a pending state and record an AgentAction (audit log + approval queue). Trusted
low-risk actions are auto-approved by policy; higher-stakes ones wait for a human.
Approve advances the item, reject cancels it. Three agents today:
  * Maintenance — on Critical machine risk, proposes a maintenance task.
  * Quality     — on a high-fail inspection, proposes a machine inspection task.
  * Reorder     — on low stock, drafts a purchase order (auto-approved by policy).
"""
from datetime import datetime, timedelta

import models
from ai import prediction
from events import (
    ProductionCompleted, DowntimeStarted, InventoryLow, QualityInspectionFailed, event_bus,
)

CRITICAL_RISK = 75                  # maintenance: act only on Critical risk
QUALITY_FAIL_RATE = 10              # quality: propose an inspection above this % fail rate
AUTO_TASK_TYPE = "Predictive (auto)"
QUALITY_TASK_TYPE = "Quality (auto)"
# The reorder agent tags its drafted POs with this prefix (humans never use it).
AUTO_PO_PREFIX = "AUTO-PO"


# ── Oversight: propose, policy, decide ─────────────────────────────
def should_auto_approve(action) -> bool:
    """Trusted low-risk actions skip the gate. Reorder drafts are reversible — no
    live order until a supplier is assigned and it's sent — so they auto-approve;
    maintenance and quality tasks wait for a human."""
    return action.agent == "reorder"


def apply_decision(db, action, decision, decided_by=None) -> None:
    """Approve or reject a proposed action, advancing or cancelling its item.
    approve -> task Open / PO Approved; reject -> item Cancelled."""
    approve = decision == "approve"
    action.status = "Approved" if approve else "Rejected"
    action.decided_by = decided_by
    action.decided_at = datetime.utcnow()
    if action.ref_kind == "maintenance_task":
        item = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == action.ref_id).first()
        if item and item.status == "Proposed":
            item.status = "Open" if approve else "Cancelled"
    elif action.ref_kind == "purchase_order":
        item = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == action.ref_id).first()
        if item and item.status == "Draft":
            item.status = "Approved" if approve else "Cancelled"


def _propose(db, tenant, agent, action_type, summary, ref_kind, ref_id,
             severity="Medium", machine_id=None) -> None:
    """Record a proposed action; auto-approve it if policy allows, otherwise
    notify a human that it awaits approval."""
    action = models.AgentAction(
        tenant_code=tenant, agent=agent, action_type=action_type, summary=summary,
        ref_kind=ref_kind, ref_id=ref_id, severity=severity,
        related_machine_id=machine_id, status="Proposed",
    )
    db.add(action)
    db.flush()
    if should_auto_approve(action):
        apply_decision(db, action, "approve", decided_by="auto-policy")
    else:
        # A human needs to decide — surface it in the notifications feed.
        db.add(models.Notification(
            tenant_code=tenant,
            notification_type="agent_proposal",
            severity=severity,
            title=f"Approval needed: {summary}",
            message=f"The {agent} agent proposed an action awaiting approval in Agent Activity.",
            status="Unread",
        ))


# ── Maintenance & Quality agents (both propose a task) ─────────────
def _open_auto_task_exists(db, machine_id, task_type) -> bool:
    return (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.machine_id == machine_id,
            models.MaintenanceTask.task_type == task_type,
            models.MaintenanceTask.status.in_(("Proposed", "Open")),
        )
        .first()
        is not None
    )


def _propose_task(db, tenant, agent, task_no, machine_id, task_type, priority, summary, notes, severity):
    now = datetime.utcnow()
    task = models.MaintenanceTask(
        task_no=task_no, machine_id=machine_id, task_type=task_type, priority=priority,
        assigned_to="Maintenance team", planned_date=now.date(), status="Proposed", notes=notes,
    )
    db.add(task)
    db.flush()
    _propose(db, tenant, agent, "open_task", summary, "maintenance_task", task.id,
             severity=severity, machine_id=machine_id)


def act_on_machine_event(event, db) -> None:
    """Maintenance agent: reassess the machine and, if risk is Critical, propose a task."""
    machine_id = getattr(event, "machine_id", None)
    if machine_id is None:
        return
    risk = prediction.risk_for_machine(db, machine_id)
    if not risk or risk["risk_score"] < CRITICAL_RISK:
        return
    if _open_auto_task_exists(db, machine_id, AUTO_TASK_TYPE):
        return
    _propose_task(
        db, event.tenant_code, "maintenance",
        task_no=f"AUTO-MAINT-{machine_id}-{int(datetime.utcnow().timestamp())}",
        machine_id=machine_id, task_type=AUTO_TASK_TYPE, priority="Critical",
        summary=f"Open a Critical maintenance task for {risk['machine_name']}",
        notes=(f"Proposed by the Maintenance agent: {risk['machine_name']} at risk "
               f"{risk['risk_score']} ({', '.join(risk['reasons'])})."),
        severity="Critical",
    )


def inspect_on_quality_failed(event: QualityInspectionFailed, db) -> None:
    """Quality agent: on a high-fail inspection, propose a machine inspection task."""
    if event.machine_id is None or not event.inspected_quantity:
        return
    rate = event.failed_quantity / event.inspected_quantity * 100
    if rate < QUALITY_FAIL_RATE:
        return
    if _open_auto_task_exists(db, event.machine_id, QUALITY_TASK_TYPE):
        return
    _propose_task(
        db, event.tenant_code, "quality",
        task_no=f"AUTO-QUAL-{event.machine_id}-{int(datetime.utcnow().timestamp())}",
        machine_id=event.machine_id, task_type=QUALITY_TASK_TYPE, priority="High",
        summary=f"Inspect machine #{event.machine_id} after {round(rate)}% quality failures",
        notes=(f"Proposed by the Quality agent: {event.failed_quantity} of "
               f"{event.inspected_quantity} failed ({round(rate)}%) on {event.inspection_no} "
               f"- {event.defect_category or 'defects'}."),
        severity="High",
    )


# ── Reorder agent ──────────────────────────────────────────────────
def _draft_po_exists(db, item_id) -> bool:
    return (
        db.query(models.PurchaseOrder)
        .filter(
            models.PurchaseOrder.item_id == item_id,
            models.PurchaseOrder.po_no.like(f"{AUTO_PO_PREFIX}-%"),
            models.PurchaseOrder.status.in_(("Draft", "Approved")),
        )
        .first()
        is not None
    )


def draft_reorder_on_inventory_low(event: InventoryLow, db) -> None:
    """Reorder agent: on low stock, draft a purchase order (auto-approved by policy)."""
    if _draft_po_exists(db, event.item_id):
        return
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == event.item_id)
        .first()
    )
    unit = item.unit if item else "units"
    order_qty = max(2 * event.reorder_level - event.current_stock, 1)  # refill to ~2x reorder level
    now = datetime.utcnow()
    po = models.PurchaseOrder(
        po_no=f"{AUTO_PO_PREFIX}-{event.item_id}-{int(now.timestamp())}",
        supplier_id=None, item_id=event.item_id, item_name=event.item_name,
        order_quantity=order_qty, unit=unit,
        expected_delivery_date=(now + timedelta(days=7)).date(), status="Draft",
        notes=(f"Proposed by the Reorder agent: {event.item_name} ({event.item_code}) at "
               f"{event.current_stock}, reorder level {event.reorder_level}."),
    )
    db.add(po)
    db.flush()
    _propose(db, event.tenant_code, "reorder", "draft_po",
             summary=f"Draft a PO for {event.item_name} ({order_qty} {unit})",
             ref_kind="purchase_order", ref_id=po.id, severity="Medium")


def register(bus=event_bus) -> None:
    """Wire the agents to their events."""
    bus.subscribe(ProductionCompleted, act_on_machine_event)            # Maintenance agent
    bus.subscribe(DowntimeStarted, act_on_machine_event)                # Maintenance agent
    bus.subscribe(QualityInspectionFailed, inspect_on_quality_failed)   # Quality agent
    bus.subscribe(InventoryLow, draft_reorder_on_inventory_low)         # Reorder agent
