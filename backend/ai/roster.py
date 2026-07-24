"""Agent Roster — the AI workforce, agent by agent (ADR-0004 / ADR-0005).

A read-model (ADR-0007) that presents each agent as a team member: what it
watches, what it proposes, whether it acts autonomously (per the auto-approve
policy), and how active it has been. Static role metadata + live counts from the
agent_actions log; tenant-scoped explicitly (agent_actions is stamped).
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

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


def build_roster(db, tenant: str):
    """One card per agent: role + autonomy + live activity, tenant-scoped.

    agent_actions is an append-only log that grows without bound, and this card is
    polled — so the per-agent tallies are computed as a GROUP BY count in SQL and
    the last-action stamp as a MAX, rather than hydrating the whole history on
    every poll (ADR-0002 rule 4). agent_actions is not in SCOPED_MODELS, so the
    tenant filter stays explicit."""
    from ai.agents import trusted_agents  # lazy: avoids an import cycle at package load

    trusted = trusted_agents(db, tenant)

    # Lifetime action tally per (agent, status) — one grouped count, not a scan of
    # the whole log. (agent, tenant_code) both index-backed.
    tally: dict = defaultdict(Counter)
    for agent, status, n in (
            db.query(models.AgentAction.agent, models.AgentAction.status, func.count())
            .filter(models.AgentAction.tenant_code == tenant)
            .group_by(models.AgentAction.agent, models.AgentAction.status).all()):
        tally[agent][status] += n
    # Most-recent action per agent — MAX on the indexed datetime, returned as a
    # real datetime (SQLAlchemy applies the column's type to func.max).
    last_by_agent = dict(
        db.query(models.AgentAction.agent, func.max(models.AgentAction.created_at))
        .filter(models.AgentAction.tenant_code == tenant)
        .group_by(models.AgentAction.agent).all())

    roster = []
    for meta in AGENTS:
        status = tally.get(meta["key"], Counter())
        last = last_by_agent.get(meta["key"])
        roster.append({
            **meta,
            "auto_approves": meta["key"] in trusted,
            "total_actions": sum(status.values()),
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
    from ai.agents import trusted_agents  # lazy: avoids an import cycle at package load

    # The whole cockpit reads one agent's slice of an append-only, unbounded log on
    # a polled endpoint, so nothing hydrates the full history: the lifetime tally
    # and output kinds are GROUP BY counts, the last-action stamp is a MAX, the
    # 7-day series is bounded to the window in SQL, and the evidence list is a
    # LIMIT (rule 4). agent_actions is not in SCOPED_MODELS -> explicit tenant filter.
    base = (db.query(models.AgentAction)
            .filter(models.AgentAction.tenant_code == tenant,
                    models.AgentAction.agent == agent_key))

    status = Counter(dict(base.with_entities(models.AgentAction.status, func.count())
                          .group_by(models.AgentAction.status).all()))
    approved, rejected = status.get("Approved", 0), status.get("Rejected", 0)
    decided = approved + rejected

    # what this agent has produced, by the kind of thing it creates
    outputs = {kind: n for kind, n in
               (base.with_entities(models.AgentAction.ref_kind, func.count())
                .filter(models.AgentAction.ref_kind.isnot(None))
                .group_by(models.AgentAction.ref_kind).all())}

    last = base.with_entities(func.max(models.AgentAction.created_at)).scalar()

    # 7-day daily activity, oldest -> newest, so the cockpit can draw a sparkline.
    # Bounded to the window in SQL; the set check keeps exact per-day semantics.
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(6, -1, -1)]
    window_set = set(window)
    cutoff = datetime.combine(window[0], datetime.min.time())
    per_day = Counter(
        c.date() for (c,) in
        base.with_entities(models.AgentAction.created_at)
        .filter(models.AgentAction.created_at >= cutoff).all()
        if c and c.date() in window_set)
    daily = [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in window]

    # Most-recent actions as evidence — a LIMIT, not the whole history.
    rows = base.order_by(models.AgentAction.id.desc()).limit(15).all()

    return {
        **meta,
        "auto_approves": agent_key in trusted_agents(db, tenant),
        "total_actions": sum(status.values()),
        "pending": status.get("Proposed", 0),
        "approved": approved,
        "rejected": rejected,
        "approval_rate": round(approved / decided * 100) if decided else None,
        "outputs": outputs,
        "last_action_at": last.isoformat() if last else None,
        "daily": daily,
        "recent": [{
            "id": a.id, "action_type": a.action_type, "summary": a.summary,
            "ref_kind": a.ref_kind, "ref_id": a.ref_id, "severity": a.severity,
            "status": a.status, "related_machine_id": a.related_machine_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "decided_by": a.decided_by,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        } for a in rows],
    }


def build_agent_policy(db, tenant: str) -> dict:
    """The tenant's agent-autonomy policy: for each agent whether it may act
    without approval, and whether that comes from a saved per-tenant policy or
    the platform default (so the UI can say which)."""
    from ai.agents import trusted_agents, tenant_has_policy  # lazy: avoids an import cycle at package load

    trusted = trusted_agents(db, tenant)
    return {
        "source": "tenant" if tenant_has_policy(db, tenant) else "default",
        "agents": [{
            "key": m["key"], "name": m["name"], "watches": m["watches"], "acts": m["acts"],
            "auto_approves": m["key"] in trusted,
        } for m in AGENTS],
    }
