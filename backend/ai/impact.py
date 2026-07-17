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
    """Executive rollup of the agent fleet's activity for one tenant."""
    rows = db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant).all()
    by_status = Counter(r.status for r in rows)
    approved = by_status.get("Approved", 0)
    rejected = by_status.get("Rejected", 0)
    decided = approved + rejected
    auto = sum(1 for r in rows if r.decided_by == "auto-policy")

    # Concrete outputs the fleet produced — proposals that weren't rejected/cancelled.
    outputs = {label: 0 for label in _OUTPUT_LABELS.values()}
    for r in rows:
        if r.status in ("Proposed", "Approved"):
            key = _OUTPUT_LABELS.get(r.ref_kind)
            if key:
                outputs[key] += 1

    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    recent = [r for r in rows if r.created_at and r.created_at >= cutoff]
    recent_status = Counter(r.status for r in recent)

    # Per-agent contribution — who did what, for the ROI view ("meet your workers").
    from ai.roster import AGENTS  # lazy: avoids an import cycle at package load
    agent_names = {a["key"]: a["name"] for a in AGENTS}
    per_agent: dict = {}
    for r in rows:
        pa = per_agent.setdefault(r.agent, {
            "agent": r.agent, "name": agent_names.get(r.agent, r.agent),
            "actions": 0, "approved": 0, "auto_approved": 0, "pending": 0,
            "outputs": {label: 0 for label in _OUTPUT_LABELS.values()},
        })
        pa["actions"] += 1
        if r.status == "Approved":
            pa["approved"] += 1
        if r.status == "Proposed":
            pa["pending"] += 1
        if r.decided_by == "auto-policy":
            pa["auto_approved"] += 1
        if r.status in ("Proposed", "Approved"):
            key = _OUTPUT_LABELS.get(r.ref_kind)
            if key:
                pa["outputs"][key] += 1
    by_agent = sorted(per_agent.values(), key=lambda a: a["actions"], reverse=True)

    agents_active = sorted({r.agent for r in rows})
    return {
        "agents_active": agents_active,
        "total_actions": len(rows),
        "approved": approved,
        "rejected": rejected,
        "auto_approved": auto,
        "auto_rate": round(auto / decided * 100) if decided else 0,   # % of decisions made autonomously
        "pending_backlog": by_status.get("Proposed", 0),              # human decisions still waiting
        "outputs": outputs,
        "by_agent": by_agent,                                         # per-agent contribution
        "last_7_days": {
            "total": len(recent),
            "proposed": recent_status.get("Proposed", 0),
            "approved": recent_status.get("Approved", 0),
            "rejected": recent_status.get("Rejected", 0),
        },
        "headline": _headline(agents_active, len(rows), auto, by_status.get("Proposed", 0)),
    }
