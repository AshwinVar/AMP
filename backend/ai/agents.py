"""AI agents — from recommending to acting (ADR-0004).

An agent observes the event stream and takes a bounded, autonomous action, not
just an advisory. The first, the Maintenance agent, escalates the platform's own
judgement: when a machine's failure risk is Critical it opens a maintenance task
(a real work item) instead of only recommending one — idempotently and
tenant-scoped. The action is bounded (create a task), auditable (the task notes
cite the risk) and reversible (a task can be closed).
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


def _open_auto_task_exists(db, machine_id) -> bool:
    return (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.machine_id == machine_id,
            models.MaintenanceTask.task_type == AUTO_TASK_TYPE,
            models.MaintenanceTask.status == "Open",
        )
        .first()
        is not None
    )


def _open_maintenance_task(db, risk) -> bool:
    """Open one maintenance task for a critical-risk machine. Returns True if a
    task was created (False if an open auto-task already exists)."""
    machine_id = risk["machine_id"]
    if _open_auto_task_exists(db, machine_id):
        return False
    now = datetime.utcnow()
    db.add(models.MaintenanceTask(
        task_no=f"AUTO-MAINT-{machine_id}-{int(now.timestamp())}",
        machine_id=machine_id,
        task_type=AUTO_TASK_TYPE,
        priority="Critical",
        assigned_to="Maintenance team",
        planned_date=now.date(),
        status="Open",
        notes=(
            f"Auto-opened by the Maintenance agent: {risk['machine_name']} at risk "
            f"{risk['risk_score']} ({', '.join(risk['reasons'])})."
        ),
    ))
    return True


def act_on_machine_event(event, db) -> None:
    """Agent step: reassess the machine and, if risk is Critical, open a task."""
    machine_id = getattr(event, "machine_id", None)
    if machine_id is None:
        return
    risk = prediction.risk_for_machine(db, machine_id)
    if risk and risk["risk_score"] >= CRITICAL_RISK:
        _open_maintenance_task(db, risk)


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
    """Reorder agent: on low stock, autonomously draft a purchase order (status
    Draft, no supplier yet) for a human to approve and assign. Idempotent per
    item, tenant-scoped, reversible."""
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
    db.add(models.PurchaseOrder(
        po_no=f"{AUTO_PO_PREFIX}-{event.item_id}-{int(now.timestamp())}",
        supplier_id=None,
        item_id=event.item_id,
        item_name=event.item_name,
        order_quantity=order_qty,
        unit=unit,
        expected_delivery_date=(now + timedelta(days=7)).date(),
        status="Draft",
        notes=(
            f"Auto-drafted by the Reorder agent: {event.item_name} ({event.item_code}) at "
            f"{event.current_stock}, reorder level {event.reorder_level}. Assign a supplier and approve."
        ),
    ))


def register(bus=event_bus) -> None:
    """Wire the agents to their events."""
    bus.subscribe(ProductionCompleted, act_on_machine_event)      # Maintenance agent
    bus.subscribe(DowntimeStarted, act_on_machine_event)          # Maintenance agent
    bus.subscribe(InventoryLow, draft_reorder_on_inventory_low)   # Reorder agent
