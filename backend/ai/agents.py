"""AI agents — propose, log, auto-approve trusted actions, act on approval
(ADR-0004, ADR-0005).

Agents observe the event stream and PROPOSE bounded actions: they create the item
in a pending state and record an AgentAction (audit log + approval queue). Trusted
low-risk actions are auto-approved by policy; higher-stakes ones wait for a human.
Approve advances the item, reject cancels it. Five agents today:
  * Maintenance — on Critical machine risk, proposes a maintenance task.
  * Quality     — on a high-fail inspection, proposes a machine inspection task.
  * Reorder     — on low stock, drafts a purchase order (auto-approved by policy).
  * Escalation  — on repeated downtime, proposes an escalation.
  * Yield       — on a machine's good-rate dropping, proposes an investigation task.
"""
import os
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
# Downtime events on a machine before the Escalation agent raises an escalation.
ESCALATION_THRESHOLD = 3
# Yield agent: propose an investigation when a machine's recent good-rate falls
# below YIELD_MIN_RATE (%) across at least YIELD_MIN_UNITS produced.
YIELD_TASK_TYPE = "Yield (auto)"
YIELD_MIN_RATE = 85
YIELD_MIN_UNITS = 50


# ── Oversight: propose, policy, decide ─────────────────────────────
def _env_trusted() -> set:
    """The platform default trust set, from the AUTO_APPROVE_AGENTS env var
    (comma-separated agent keys); itself defaulting to 'reorder' (reversible
    drafts)."""
    return {a.strip() for a in os.environ.get("AUTO_APPROVE_AGENTS", "reorder").split(",") if a.strip()}


def _tenant_trusted(db, tenant):
    """The tenant's stored auto-approve set, or None when no policy row exists
    (the caller then falls back to the env default). An empty stored policy is a
    real choice — 'no agent auto-approves' — and returns an empty set, not None."""
    if db is None or not tenant:
        return None
    row = db.query(models.AgentPolicy).filter(models.AgentPolicy.tenant_code == tenant).first()
    if row is None:
        return None
    return {a.strip() for a in (row.auto_approve_agents or "").split(",") if a.strip()}


def tenant_has_policy(db, tenant) -> bool:
    """Whether this tenant has an explicit saved policy (vs. the default)."""
    return _tenant_trusted(db, tenant) is not None


def trusted_agents(db=None, tenant=None) -> set:
    """Agent keys that auto-approve for this tenant: a stored per-tenant policy
    wins; otherwise the AUTO_APPROVE_AGENTS env default ('reorder')."""
    stored = _tenant_trusted(db, tenant)
    return stored if stored is not None else _env_trusted()


def should_auto_approve(action, db=None) -> bool:
    """Trusted low-risk actions skip the human gate. Trust is per-tenant (a saved
    policy, set by an Admin in the UI) and falls back to the AUTO_APPROVE_AGENTS
    env default ('reorder' — reversible drafts). Maintenance and quality stay
    gated unless explicitly trusted."""
    return action.agent in trusted_agents(db, getattr(action, "tenant_code", None))


def set_agent_policy(db, tenant, agent_keys) -> list:
    """Persist the tenant's auto-approve set, keeping only valid agent keys.
    Returns the stored, sorted list."""
    from ai.roster import AGENTS  # lazy: avoids an import cycle at package load

    valid = {m["key"] for m in AGENTS}
    keys = sorted({k for k in agent_keys if k in valid})
    row = db.query(models.AgentPolicy).filter(models.AgentPolicy.tenant_code == tenant).first()
    if row is None:
        db.add(models.AgentPolicy(tenant_code=tenant, auto_approve_agents=",".join(keys)))
    else:
        row.auto_approve_agents = ",".join(keys)
    db.commit()
    return keys


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
    elif action.ref_kind == "escalation":
        item = db.query(models.Escalation).filter(models.Escalation.id == action.ref_id).first()
        if item and item.status == "Proposed":
            item.status = "Open" if approve else "Cancelled"


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
    if should_auto_approve(action, db):
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


# ── Escalation agent ───────────────────────────────────────────────
def _open_agent_escalation_exists(db, machine_id) -> bool:
    return (
        db.query(models.Escalation)
        .filter(models.Escalation.machine_id == machine_id,
                models.Escalation.source == "Escalation agent",
                models.Escalation.status.in_(("Proposed", "Open")))
        .first()
        is not None
    )


def escalate_on_repeated_downtime(event: DowntimeStarted, db) -> None:
    """Escalation agent: after repeated downtime on a machine, propose an escalation."""
    if event.machine_id is None:
        return
    count = db.query(models.DowntimeLog).filter(models.DowntimeLog.machine_id == event.machine_id).count()
    if count < ESCALATION_THRESHOLD or _open_agent_escalation_exists(db, event.machine_id):
        return
    esc = models.Escalation(
        tenant_code=event.tenant_code,
        machine_id=event.machine_id,
        title=f"Repeated downtime on machine #{event.machine_id} ({count} events)",
        severity="High",
        owner="Maintenance Lead",
        department="Maintenance",
        status="Proposed",
        source="Escalation agent",
        notes=f"Proposed by the Escalation agent after {count} downtime events. Latest: {event.reason}.",
    )
    db.add(esc)
    db.flush()
    _propose(db, event.tenant_code, "escalation", "raise_escalation",
             summary=f"Escalate repeated downtime on machine #{event.machine_id}",
             ref_kind="escalation", ref_id=esc.id, severity="High", machine_id=event.machine_id)


# ── Escalation agent, proactive mode (from the morning briefing) ───
# Department that owns each briefing module's escalation.
_BRIEFING_DEPT = {
    "machines": "Maintenance", "downtime": "Maintenance",
    "inventory": "Procurement", "quality": "Quality", "oee": "Operations",
    "orders": "Planning", "cmms": "Maintenance", "documents": "Quality",
}


def _briefing_marker(key: str) -> str:
    """A stable machine-readable tag stamped into an escalation's notes so the
    briefing signal it came from can be recognised later (dedupe + UI state)."""
    return f"[briefing:{key}]"


def open_briefing_escalation_ids(db, tenant) -> dict:
    """Map each briefing-alert key to the id of the open (Proposed/Open) escalation
    the Escalation agent raised for it — so the briefing can mark which alerts it
    acted on, link straight to the escalation, and not raise the same one twice.
    Reads the ``[briefing:<key>]`` marker the agent stamps into the notes."""
    rows = (db.query(models.Escalation)
            .filter(models.Escalation.tenant_code == tenant,
                    models.Escalation.source == "Escalation agent",
                    models.Escalation.status.in_(("Proposed", "Open")),
                    models.Escalation.notes.like("%[briefing:%"))
            .order_by(models.Escalation.id)
            .all())
    out: dict = {}
    for r in rows:
        n = r.notes or ""
        i = n.find("[briefing:")
        j = n.find("]", i)
        if i != -1 and j > i:
            out[n[i + len("[briefing:"):j]] = r.id   # later (higher id) wins
    return out


def escalate_from_briefing(db, tenant) -> dict:
    """Escalation agent, proactive mode: turn the morning briefing's most urgent
    (high-severity) alert into a proposed Escalation + AgentAction, so it lands in
    the approval queue and notifications instead of only being displayed on the
    dashboard. Deduped per alert kind (one open agent escalation per briefing
    signal). Returns a small result the caller can report; makes no change when
    there's nothing urgent to raise."""
    from ai.briefing import build_briefing, DOWN_STATUSES  # lazy: avoids an import cycle at package load

    b = build_briefing(db, tenant)
    if not b["has_data"]:
        return {"escalated": False, "reason": "no_data"}
    top = next((a for a in b["alerts"] if a["severity"] == "high"), None)
    if top is None:
        return {"escalated": False, "reason": "no_high_alert"}
    if top["key"] in open_briefing_escalation_ids(db, tenant):
        return {"escalated": False, "reason": "already_open", "alert_key": top["key"]}

    # Link the escalation to a machine when the alert is about machines being down.
    machine_id = None
    if top["key"] == "machines_down":
        m = (db.query(models.Machine)
             .filter(models.Machine.tenant_code == tenant,
                     models.Machine.status.in_(DOWN_STATUSES)).first())
        machine_id = m.id if m else None

    esc = models.Escalation(
        tenant_code=tenant, machine_id=machine_id,
        title=f"Briefing: {top['title']}", severity="High",
        owner="Plant Manager", department=_BRIEFING_DEPT.get(top["module"], "Operations"),
        status="Proposed", source="Escalation agent",
        notes=(f"{_briefing_marker(top['key'])} Raised by the Escalation agent from the "
               f"morning briefing. {top['title']} — {top['detail']}. Act in: {top['module']}."),
    )
    db.add(esc)
    db.flush()
    _propose(db, tenant, "escalation", "raise_escalation",
             summary=f"Escalate from briefing: {top['title']}",
             ref_kind="escalation", ref_id=esc.id, severity="High", machine_id=machine_id)
    return {"escalated": True, "alert_key": top["key"], "summary": esc.title, "escalation_id": esc.id}


# ── Yield agent ────────────────────────────────────────────────────
def _recent_yield(db, machine_id, limit=10):
    """Good/total across a machine's most recent production runs."""
    recs = (db.query(models.ProductionRecord)
            .filter(models.ProductionRecord.machine_id == machine_id)
            .order_by(models.ProductionRecord.id.desc()).limit(limit).all())
    total = sum(r.total_count or 0 for r in recs)
    good = sum(r.good_count or 0 for r in recs)
    return total, good


def assess_yield_on_production(event: ProductionCompleted, db) -> None:
    """Yield agent: when a machine's recent good-rate drops below the threshold
    (with enough volume), propose an investigation task."""
    machine_id = getattr(event, "machine_id", None)
    if machine_id is None:
        return
    total, good = _recent_yield(db, machine_id)
    if total < YIELD_MIN_UNITS:
        return
    rate = round(good / total * 100)
    if rate >= YIELD_MIN_RATE:
        return
    if _open_auto_task_exists(db, machine_id, YIELD_TASK_TYPE):
        return
    _propose_task(
        db, event.tenant_code, "yield",
        task_no=f"AUTO-YIELD-{machine_id}-{int(datetime.utcnow().timestamp())}",
        machine_id=machine_id, task_type=YIELD_TASK_TYPE, priority="High",
        summary=f"Investigate low yield on machine #{machine_id} ({rate}% good)",
        notes=(f"Proposed by the Yield agent: recent good-rate {rate}% over {total} units "
               f"(below {YIELD_MIN_RATE}%). Latest run {event.work_order_no}: "
               f"{event.quantity} of {event.part_number}."),
        severity="High",
    )


def register(bus=event_bus) -> None:
    """Wire the agents to their events."""
    bus.subscribe(ProductionCompleted, act_on_machine_event)            # Maintenance agent
    bus.subscribe(ProductionCompleted, assess_yield_on_production)      # Yield agent
    bus.subscribe(DowntimeStarted, act_on_machine_event)                # Maintenance agent
    bus.subscribe(DowntimeStarted, escalate_on_repeated_downtime)       # Escalation agent
    bus.subscribe(QualityInspectionFailed, inspect_on_quality_failed)   # Quality agent
    bus.subscribe(InventoryLow, draft_reorder_on_inventory_low)         # Reorder agent
