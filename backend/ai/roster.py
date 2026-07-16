"""Agent Roster — the AI workforce, agent by agent (ADR-0004 / ADR-0005).

A read-model (ADR-0007) that presents each agent as a team member: what it
watches, what it proposes, whether it acts autonomously (per the auto-approve
policy), and how active it has been. Static role metadata + live counts from the
agent_actions log; tenant-scoped explicitly (agent_actions is stamped).
"""
from collections import Counter
from datetime import datetime, timedelta

import models

name = "roster"

# The fleet's fixed roster. Role knowledge lives with the agents (ADR-0004/0005);
# the order is the order humans meet them.
AGENTS = [
    {"key": "maintenance", "name": "Maintenance agent",
     "watches": "Machine risk on production & downtime events",
     "acts": "Proposes a maintenance task when risk turns Critical"},
    {"key": "quality", "name": "Quality agent",
     "watches": "Failed quality inspections",
     "acts": "Proposes a machine inspection above a fail-rate threshold"},
    {"key": "reorder", "name": "Reorder agent",
     "watches": "Inventory falling to its reorder level",
     "acts": "Drafts a replenishment purchase order"},
    {"key": "escalation", "name": "Escalation agent",
     "watches": "Repeated downtime on a machine",
     "acts": "Raises an escalation to the maintenance lead"},
    {"key": "yield", "name": "Yield agent",
     "watches": "A machine's good-rate across recent runs",
     "acts": "Proposes an investigation when yield drops"},
]


class _AgentRef:
    """Minimal stand-in so the auto-approve policy can be read by agent key."""

    def __init__(self, agent):
        self.agent = agent


def build_roster(db, tenant: str):
    """One card per agent: role + autonomy + live activity, tenant-scoped."""
    from ai.agents import should_auto_approve  # lazy: avoids an import cycle at package load

    rows = db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant).all()
    by_agent: dict[str, list] = {}
    for r in rows:
        by_agent.setdefault(r.agent, []).append(r)

    roster = []
    for meta in AGENTS:
        mine = by_agent.get(meta["key"], [])
        status = Counter(a.status for a in mine)
        last = max((a.created_at for a in mine if a.created_at), default=None)
        roster.append({
            **meta,
            "auto_approves": should_auto_approve(_AgentRef(meta["key"])),
            "total_actions": len(mine),
            "pending": status.get("Proposed", 0),
            "approved": status.get("Approved", 0),
            "rejected": status.get("Rejected", 0),
            "last_action_at": last.isoformat() if last else None,
        })
    return roster


def _meta(agent_key: str):
    return next((m for m in AGENTS if m["key"] == agent_key), None)


def build_agent_detail(db, tenant: str, agent_key: str):
    """A single-agent cockpit (the drill-down parallel to the machine twin):
    role + autonomy, the full decision tally with an approval rate, what the
    agent has produced, a 7-day activity series, and its most recent actions.
    Returns None for an unknown agent (the caller then 404s). Tenant-scoped —
    agent_actions is stamped, so it's filtered explicitly (ADR-0002)."""
    meta = _meta(agent_key)
    if meta is None:
        return None
    from ai.agents import should_auto_approve  # lazy: avoids an import cycle at package load

    rows = (db.query(models.AgentAction)
            .filter(models.AgentAction.tenant_code == tenant,
                    models.AgentAction.agent == agent_key)
            .order_by(models.AgentAction.id.desc()).all())
    status = Counter(a.status for a in rows)
    approved, rejected = status.get("Approved", 0), status.get("Rejected", 0)
    decided = approved + rejected

    # what this agent has produced, by the kind of thing it creates
    outputs = Counter(a.ref_kind for a in rows if a.ref_kind)

    # 7-day daily activity, oldest -> newest, so the cockpit can draw a sparkline
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(6, -1, -1)]
    window_set = set(window)
    per_day = Counter(a.created_at.date() for a in rows if a.created_at and a.created_at.date() in window_set)
    daily = [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in window]

    last = max((a.created_at for a in rows if a.created_at), default=None)

    return {
        **meta,
        "auto_approves": should_auto_approve(_AgentRef(agent_key)),
        "total_actions": len(rows),
        "pending": status.get("Proposed", 0),
        "approved": approved,
        "rejected": rejected,
        "approval_rate": round(approved / decided * 100) if decided else None,
        "outputs": dict(outputs),
        "last_action_at": last.isoformat() if last else None,
        "daily": daily,
        "recent": [{
            "id": a.id, "action_type": a.action_type, "summary": a.summary,
            "ref_kind": a.ref_kind, "ref_id": a.ref_id, "severity": a.severity,
            "status": a.status, "related_machine_id": a.related_machine_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "decided_by": a.decided_by,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        } for a in rows[:15]],
    }
