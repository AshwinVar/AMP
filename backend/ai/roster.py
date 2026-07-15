"""Agent Roster — the AI workforce, agent by agent (ADR-0004 / ADR-0005).

A read-model (ADR-0007) that presents each agent as a team member: what it
watches, what it proposes, whether it acts autonomously (per the auto-approve
policy), and how active it has been. Static role metadata + live counts from the
agent_actions log; tenant-scoped explicitly (agent_actions is stamped).
"""
from collections import Counter

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
