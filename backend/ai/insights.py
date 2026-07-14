"""Insights — the Mission Control read-model (ADR-0003, step 3; ADR-0005).

A per-tenant projection that unifies the AI platform's open recommendations,
recent notable domain events, and the agents' proposed actions into one stream —
"what the factory needs to know now." A read-model over existing tables; it adds
no storage.

Scoping is applied explicitly by tenant: ai_recommendations is auto-scoped
(ADR-0002), while event_log and agent_actions are only tenant-stamped, so the
feed filters them here to stay leak-proof.
"""
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import models

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
    source: str                     # "recommendation" | "event" | "action"
    kind: str                       # recommendation_type / event_type / action_type
    severity: str                   # Critical | High | Medium | Low | Info
    title: str
    message: str
    occurred_at: str                # ISO-8601
    related_machine_id: Optional[int] = None
    ref_id: Optional[int] = None    # row id to act on (recommendation / agent action); None for events


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


def _action_to_insight(a) -> Insight:
    """A proposed agent action — what the platform wants to do, awaiting approval."""
    return Insight(
        source="action",
        kind=a.action_type,
        severity=a.severity or "Medium",
        title=a.summary,
        message=f"{a.agent.title()} agent · proposed, awaiting approval.",
        occurred_at=(a.created_at or datetime.utcnow()).isoformat(),
        related_machine_id=a.related_machine_id,
        ref_id=a.id,
    )


def build_feed(db, tenant: str, limit: int = 50):
    """Most-recent-first insight feed for one tenant: open AI recommendations,
    recent notable domain events, and the agents' proposed actions, unified.
    ``tenant`` is applied explicitly so the feed is leak-proof regardless of the
    global scoping state."""
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
    actions = (
        db.query(models.AgentAction)
        .filter(models.AgentAction.tenant_code == tenant,
                models.AgentAction.status == "Proposed")
        .order_by(models.AgentAction.created_at.desc())
        .limit(limit).all()
    )
    insights = ([_rec_to_insight(r) for r in recs]
                + [_event_to_insight(e) for e in events]
                + [_action_to_insight(a) for a in actions])
    insights.sort(key=lambda i: i.occurred_at, reverse=True)
    return [asdict(i) for i in insights[:limit]]
