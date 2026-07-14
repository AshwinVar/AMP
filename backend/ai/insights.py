"""Insights — the Mission Control read-model (ADR-0003, step 3).

A per-tenant projection that unifies the AI platform's open recommendations with
recent notable domain events into one stream — "what the factory needs to know
now." A read-model over existing tables (ai_recommendations, event_log); it adds
no storage.

Scoping is applied **explicitly** by tenant: ai_recommendations is auto-scoped
(ADR-0002) but event_log is only tenant-*stamped*, so the feed must filter it
here to stay leak-proof.
"""
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import models
from ai.agents import AUTO_TASK_TYPE, AUTO_PO_PREFIX

name = "insights"

# Events worth surfacing in Mission Control (routine completions are omitted).
NOTABLE_EVENTS = ("DowntimeStarted", "QualityInspectionFailed", "InventoryLow")
_EVENT_SEVERITY = {
    "QualityInspectionFailed": "High",
    "DowntimeStarted": "High",
    "InventoryLow": "Medium",
}


@dataclass(frozen=True)
class Insight:
    source: str                     # "recommendation" | "event"
    kind: str                       # recommendation_type or event_type
    severity: str                   # Critical | High | Medium | Low | Info
    title: str
    message: str
    occurred_at: str                # ISO-8601
    related_machine_id: Optional[int] = None
    ref_id: Optional[int] = None    # source row id (recommendation) for actioning; None for events


def _describe_event(event_type: str, p: dict):
    if event_type == "DowntimeStarted":
        return (f"Downtime — {p.get('reason', 'stopped')}",
                f"Machine {p.get('machine_id', '?')} entered downtime: "
                f"{p.get('reason', '')} ({p.get('duration', 'n/a')}).")
    if event_type == "QualityInspectionFailed":
        return (f"Quality fail on {p.get('inspection_no', '?')}",
                f"{p.get('failed_quantity', '?')} of {p.get('inspected_quantity', '?')} units failed"
                f" — {p.get('defect_category') or 'defects'}.")
    if event_type == "InventoryLow":
        return (f"Low stock — {p.get('item_name', '?')}",
                f"{p.get('item_name', '?')} ({p.get('item_code', '?')}) at {p.get('current_stock', '?')}"
                f", reorder level {p.get('reorder_level', '?')}.")
    return (event_type, "")


def _event_to_insight(e) -> Insight:
    try:
        payload = json.loads(e.payload) if e.payload else {}
    except (ValueError, TypeError):
        payload = {}
    title, message = _describe_event(e.event_type, payload)
    return Insight(
        source="event",
        kind=e.event_type,
        severity=_EVENT_SEVERITY.get(e.event_type, "Info"),
        title=title,
        message=message,
        occurred_at=(e.occurred_at or datetime.utcnow()).isoformat(),
        related_machine_id=payload.get("machine_id"),
    )


def _rec_to_insight(r) -> Insight:
    return Insight(
        source="recommendation",
        kind=r.recommendation_type,
        severity=r.severity or "Medium",
        title=r.title,
        message=r.message,
        occurred_at=(r.created_at or datetime.utcnow()).isoformat(),
        related_machine_id=r.related_machine_id,
        ref_id=r.id,
    )


def _task_to_insight(t) -> Insight:
    """An agent-opened maintenance task — what the platform *did*, not just advised."""
    return Insight(
        source="action",
        kind="maintenance_task",
        severity=t.priority or "Medium",
        title=f"Maintenance task opened - machine #{t.machine_id}",
        message=t.notes or f"{t.task_type} {t.task_no} for machine {t.machine_id}.",
        occurred_at=(t.created_at or datetime.utcnow()).isoformat(),
        related_machine_id=t.machine_id,
    )


def _po_to_insight(p) -> Insight:
    """An agent-drafted purchase order — the reorder agent acting on low stock."""
    return Insight(
        source="action",
        kind="purchase_order",
        severity="Medium",
        title=f"Purchase order drafted - {p.item_name}",
        message=p.notes or f"Draft PO {p.po_no}: {p.order_quantity} {p.unit} of {p.item_name}.",
        occurred_at=(p.created_at or datetime.utcnow()).isoformat(),
    )


def build_feed(db, tenant: str, limit: int = 50):
    """Most-recent-first insight feed for one tenant: open AI recommendations,
    recent notable domain events, and the agents' open auto-tasks and drafted
    purchase orders, unified. ``tenant`` is applied explicitly so the feed is
    leak-proof regardless of the global scoping state."""
    recs = (
        db.query(models.AIRecommendation)
        .filter(models.AIRecommendation.tenant_code == tenant,
                models.AIRecommendation.status == "Open")
        .order_by(models.AIRecommendation.created_at.desc())
        .limit(limit).all()
    )
    events = (
        db.query(models.EventLog)
        .filter(models.EventLog.tenant_code == tenant,
                models.EventLog.event_type.in_(NOTABLE_EVENTS))
        .order_by(models.EventLog.occurred_at.desc())
        .limit(limit).all()
    )
    tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.tenant_code == tenant,
                models.MaintenanceTask.task_type == AUTO_TASK_TYPE,
                models.MaintenanceTask.status == "Open")
        .order_by(models.MaintenanceTask.created_at.desc())
        .limit(limit).all()
    )
    pos = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.tenant_code == tenant,
                models.PurchaseOrder.po_no.like(f"{AUTO_PO_PREFIX}-%"),
                models.PurchaseOrder.status == "Draft")
        .order_by(models.PurchaseOrder.created_at.desc())
        .limit(limit).all()
    )
    insights = ([_rec_to_insight(r) for r in recs]
                + [_event_to_insight(e) for e in events]
                + [_task_to_insight(t) for t in tasks]
                + [_po_to_insight(p) for p in pos])
    insights.sort(key=lambda i: i.occurred_at, reverse=True)
    return [asdict(i) for i in insights[:limit]]
