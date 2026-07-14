"""AI agents — propose, log, and (on approval) act (ADR-0004, ADR-0005).

An agent observes the event stream and PROPOSES a bounded action: it creates the
underlying item in a pending state and records an AgentAction (the audit log +
approval queue). A human approves — the item goes live — or rejects — it is
cancelled. Two agents today:
  * Maintenance — on Critical machine risk, proposes a maintenance task.
  * Reorder     — on low stock, proposes (drafts) a purchase order.
Both bounded, idempotent, tenant-scoped, reversible, and never live until approved.
"""
from datetime import datetime, timedelta

import models
from ai import prediction
from events import ProductionCompleted, DowntimeStarted, InventoryLow, event_bus

# Autonomy threshold: act only on Critical risk (see predictive_engine.classify_risk).
CRITICAL_RISK = 75

AUTO_TASK_TYPE = "Predictive (auto)"
# The reorder agent tags its drafted POs with this prefix (humans never use it).
AUTO_PO_PREFIX = "AUTO-PO"


def _record_action(db, tenant, agent, action_type, summary, ref_kind, ref_id,
                   severity="Medium", machine_id=None) -> None:
    """Log a proposed agent action — the audit trail and the approval queue."""
    db.add(models.AgentAction(
        tenant_code=tenant, agent=agent, action_type=action_type, summary=summary,
        ref_kind=ref_kind, ref_id=ref_id, severity=severity,
        related_machine_id=machine_id, status="Proposed",
    ))


# ── Maintenance agent ──────────────────────────────────────────────
def _open_auto_task_exists(db, machine_id) -> bool:
    return (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.machine_id == machine_id,
            models.MaintenanceTask.task_type == AUTO_TASK_TYPE,
            models.MaintenanceTask.status.in_(("Proposed", "Open")),
        )
        .first()
        is not None
    )


def _propose_maintenance_task(db, risk, tenant) -> None:
    machine_id = risk["machine_id"]
    if _open_auto_task_exists(db, machine_id):
        return
    now = datetime.utcnow()
    task = models.MaintenanceTask(
        task_no=f"AUTO-MAINT-{machine_id}-{int(now.timestamp())}",
        machine_id=machine_id,
        task_type=AUTO_TASK_TYPE,
        priority="Critical",
        assigned_to="Maintenance team",
        planned_date=now.date(),
        status="Proposed",   # pending human approval (ADR-0005)
        notes=(
            f"Proposed by the Maintenance agent: {risk['machine_name']} at risk "
            f"{risk['risk_score']} ({', '.join(risk['reasons'])})."
        ),
    )
    db.add(task)
    db.flush()  # assign task.id for the action record
    _record_action(db, tenant, "maintenance", "open_task",
                   summary=f"Open a Critical maintenance task for {risk['machine_name']}",
                   ref_kind="maintenance_task", ref_id=task.id,
                   severity="Critical", machine_id=machine_id)


def act_on_machine_event(event, db) -> None:
    """Maintenance agent: reassess the machine and, if risk is Critical, propose a task."""
    machine_id = getattr(event, "machine_id", None)
    if machine_id is None:
        return
    risk = prediction.risk_for_machine(db, machine_id)
    if risk and risk["risk_score"] >= CRITICAL_RISK:
        _propose_maintenance_task(db, risk, event.tenant_code)


# ── Reorder agent ──────────────────────────────────────────────────
def _draft_po_exists(db, item_id) -> bool:
    return (
        db.query(models.PurchaseOrder)
        .filter(
            models.PurchaseOrder.item_id == item_id,
            models.PurchaseOrder.po_no.like(f"{AUTO_PO_PREFIX}-%"),
            models.PurchaseOrder.status == "Draft",
        )
        .first()
        is not None
    )


def draft_reorder_on_inventory_low(event: InventoryLow, db) -> None:
    """Reorder agent: on low stock, propose (draft) a purchase order."""
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
        supplier_id=None,
        item_id=event.item_id,
        item_name=event.item_name,
        order_quantity=order_qty,
        unit=unit,
        expected_delivery_date=(now + timedelta(days=7)).date(),
        status="Draft",   # pending human approval + supplier assignment (ADR-0005)
        notes=(
            f"Proposed by the Reorder agent: {event.item_name} ({event.item_code}) at "
            f"{event.current_stock}, reorder level {event.reorder_level}."
        ),
    )
    db.add(po)
    db.flush()
    _record_action(db, event.tenant_code, "reorder", "draft_po",
                   summary=f"Draft a PO for {event.item_name} ({order_qty} {unit})",
                   ref_kind="purchase_order", ref_id=po.id, severity="Medium")


# ── Human decision (approve / reject) ──────────────────────────────
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


def register(bus=event_bus) -> None:
    """Wire the agents to their events."""
    bus.subscribe(ProductionCompleted, act_on_machine_event)      # Maintenance agent
    bus.subscribe(DowntimeStarted, act_on_machine_event)          # Maintenance agent
    bus.subscribe(InventoryLow, draft_reorder_on_inventory_low)   # Reorder agent
