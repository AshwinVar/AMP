"""Agent Impact — an executive read-model over the agent audit log (ADR-0005).

Answers the question a factory owner actually asks: *what has my autonomous
agent fleet done for me?* It rolls up the agent_actions log into the concrete
outputs the fleet produced (maintenance tasks opened, purchase orders drafted,
escalations raised), how much ran without a human (the auto-approval rate),
what is still waiting on a human (the decision backlog), and a last-7-days
slice. A read-model over an existing table (adds no storage); agent_actions is
tenant-stamped, so it is filtered by tenant explicitly (ADR-0002).
"""
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func

import models

name = "impact"

WINDOW_DAYS = 7
# What each agent produces, keyed by the AgentAction.ref_kind it creates.
_OUTPUT_LABELS = {
    "maintenance_task": "maintenance_tasks",
    "purchase_order": "purchase_orders",
    "escalation": "escalations",
}


def _headline(agents_active, total, auto, pending) -> str:
    n = len(agents_active)
    parts = [f"{n} agent{'s' if n != 1 else ''} active",
             f"{total} action{'s' if total != 1 else ''}"]
    if auto:
        parts.append(f"{auto} auto-approved")
    if pending:
        parts.append(f"{pending} awaiting you")
    return " · ".join(parts)


def build_impact(db, tenant: str) -> dict:
    """Executive rollup of the agent fleet's activity for one tenant.

    agent_actions grows a row per agent decision and this rollup is polled on the
    command header (via ai.pulse), so it is aggregated in SQL — a GROUP BY count
    over (agent, status, decided_by, ref_kind) plus a windowed status count —
    rather than hydrating the whole, ever-growing table into Python on every poll
    (rule 4, matching ai.roster). Bounded by the number of distinct combinations,
    not by the row count, so the query cost doesn't grow with history; the numbers
    are the identical lifetime totals a full scan would produce. agent_actions is
    not in SCOPED_MODELS, so the tenant filter is explicit (ADR-0002); tenant_code
    and created_at are indexed."""
    from ai.roster import AGENTS  # lazy: avoids an import cycle at package load
    agent_names = {a["key"]: a["name"] for a in AGENTS}

    # One grouped pass over the lifetime log: every (agent, status, decided_by,
    # ref_kind) combination with its row count.
    grouped = (db.query(models.AgentAction.agent, models.AgentAction.status,
                        models.AgentAction.decided_by, models.AgentAction.ref_kind,
                        func.count())
               .filter(models.AgentAction.tenant_code == tenant)
               .group_by(models.AgentAction.agent, models.AgentAction.status,
                         models.AgentAction.decided_by, models.AgentAction.ref_kind)
               .all())

    total = 0
    by_status: Counter = Counter()
    auto = 0
    # Concrete outputs the fleet produced — proposals that weren't rejected/cancelled.
    outputs = {label: 0 for label in _OUTPUT_LABELS.values()}
    # Per-agent contribution — who did what, for the ROI view ("meet your workers").
    per_agent: dict = {}
    for agent, status, decided_by, ref_kind, count in grouped:
        total += count
        by_status[status] += count
        is_auto = decided_by == "auto-policy"
        if is_auto:
            auto += count
        label = _OUTPUT_LABELS.get(ref_kind)
        is_output = bool(label) and status in ("Proposed", "Approved")
        if is_output:
            outputs[label] += count

        pa = per_agent.setdefault(agent, {
            "agent": agent, "name": agent_names.get(agent, agent),
            "actions": 0, "approved": 0, "auto_approved": 0, "pending": 0,
            "outputs": {lbl: 0 for lbl in _OUTPUT_LABELS.values()},
        })
        pa["actions"] += count
        if status == "Approved":
            pa["approved"] += count
        if status == "Proposed":
            pa["pending"] += count
        if is_auto:
            pa["auto_approved"] += count
        if is_output:
            pa["outputs"][label] += count

    approved = by_status.get("Approved", 0)
    rejected = by_status.get("Rejected", 0)
    decided = approved + rejected

    # Last-7-days slice: a windowed status count (created_at is indexed), not a
    # re-scan of the whole history in Python.
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    recent_status: Counter = Counter(dict(
        db.query(models.AgentAction.status, func.count())
        .filter(models.AgentAction.tenant_code == tenant,
                models.AgentAction.created_at >= cutoff)
        .group_by(models.AgentAction.status).all()))

    # Busiest agent first; the agent key breaks ties so the order is deterministic.
    by_agent = sorted(per_agent.values(), key=lambda a: (-a["actions"], a["agent"]))

    agents_active = sorted(per_agent)
    return {
        "agents_active": agents_active,
        "total_actions": total,
        "approved": approved,
        "rejected": rejected,
        "auto_approved": auto,
        "auto_rate": round(auto / decided * 100) if decided else 0,   # % of decisions made autonomously
        "pending_backlog": by_status.get("Proposed", 0),              # human decisions still waiting
        "outputs": outputs,
        "by_agent": by_agent,                                         # per-agent contribution
        "last_7_days": {
            "total": sum(recent_status.values()),
            "proposed": recent_status.get("Proposed", 0),
            "approved": recent_status.get("Approved", 0),
            "rejected": recent_status.get("Rejected", 0),
        },
        "headline": _headline(agents_active, total, auto, by_status.get("Proposed", 0)),
    }
