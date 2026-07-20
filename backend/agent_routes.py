"""Agent oversight routes (ADR-0004 / ADR-0005) — the AI workforce surface.

The agent fleet's activity log + approval queue, oversight metrics, roster,
per-tenant autonomy policy, impact rollup, activity trend, and the human
approve/reject decisions. Peeled out of main.py, following the register(app)
pattern. Every handler is tenant-scoped; the mutating ones (policy PUT,
approve/reject) advance an AgentAction under human oversight (ADR-0005).
"""
from collections import Counter

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import ai
import models
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _agent_action_dict(a):
    return {
        "id": a.id, "agent": a.agent, "action_type": a.action_type, "summary": a.summary,
        "ref_kind": a.ref_kind, "ref_id": a.ref_id, "severity": a.severity, "status": a.status,
        "related_machine_id": a.related_machine_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "decided_by": a.decided_by, "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


def _decide_agent_action(action_id, decision, db, current_user):
    tenant = request_tenant(current_user)
    action = db.query(models.AgentAction).filter(
        models.AgentAction.id == action_id, models.AgentAction.tenant_code == tenant).first()
    if not action:
        raise HTTPException(status_code=404, detail="Agent action not found")
    if action.status != "Proposed":
        raise HTTPException(status_code=400, detail=f"Already {action.status.lower()}")
    ai.agents.apply_decision(db, action, decision,
                             decided_by=current_user.get("sub") or current_user.get("username"))
    db.commit()
    db.refresh(action)
    return _agent_action_dict(action)


def register(app):
    @app.get("/agent-actions")
    def list_agent_actions(status: str = None, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent activity log + approval queue (ADR-0005), tenant-scoped.
        tenant = request_tenant(current_user)
        q = db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant)
        if status:
            q = q.filter(models.AgentAction.status == status)
        rows = q.order_by(models.AgentAction.created_at.desc()).limit(300).all()
        return [_agent_action_dict(a) for a in rows]

    @app.get("/agent-actions/stats")
    def agent_action_stats(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent oversight metrics (ADR-0005), tenant-scoped.
        tenant = request_tenant(current_user)
        rows = db.query(models.AgentAction).filter(models.AgentAction.tenant_code == tenant).all()
        by_status = Counter(r.status for r in rows)
        by_agent = Counter(r.agent for r in rows)
        return {
            "total": len(rows),
            "proposed": by_status.get("Proposed", 0),
            "approved": by_status.get("Approved", 0),
            "rejected": by_status.get("Rejected", 0),
            "auto_approved": sum(1 for r in rows if r.decided_by == "auto-policy"),
            "by_agent": dict(by_agent),   # every agent that has acted (maintenance/quality/reorder/escalation/…)
        }

    @app.get("/agent-actions/impact")
    def agent_action_impact(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent Impact (ADR-0005): executive rollup of what the agent fleet has produced,
        # how much ran autonomously, and what's still awaiting a human — tenant-scoped.
        return ai.impact.build_impact(db, request_tenant(current_user))

    @app.get("/agent-roster")
    def agent_roster(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent Roster (ADR-0004/0005): the AI workforce — each agent's role, autonomy,
        # and live activity, tenant-scoped.
        return ai.roster.build_roster(db, request_tenant(current_user))

    @app.get("/agent-policy")
    def get_agent_policy(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent autonomy policy (ADR-0004/0005): which agents may act without a human,
        # per-tenant, with the platform default as fallback.
        return ai.roster.build_agent_policy(db, request_tenant(current_user))

    @app.put("/agent-policy")
    def update_agent_policy(payload: schemas.AgentPolicyUpdate, db: Session = Depends(_get_db),
                            current_user: dict = Depends(require_roles(["Admin"]))):
        # Changing which agents act autonomously is a trust decision — Admin only.
        ai.agents.set_agent_policy(db, request_tenant(current_user), payload.auto_approve)
        return ai.roster.build_agent_policy(db, request_tenant(current_user))

    @app.get("/agent-roster/{agent_key}")
    def agent_detail(agent_key: str, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent detail (ADR-0004/0005): the single-agent cockpit — role, autonomy,
        # decision tally with an approval rate, produced outputs, a 7-day activity
        # series, and recent actions. 404 for an unknown agent.
        detail = ai.roster.build_agent_detail(db, request_tenant(current_user), agent_key)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown agent")
        return detail

    @app.get("/agent-actions/trend")
    def agent_action_trend(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        # Agent activity trend (ADR-0005/0007): last-7-days daily action counts for a
        # sparkline of how busy the fleet has been, tenant-scoped.
        return ai.trends.build_agent_trend(db, request_tenant(current_user))

    @app.post("/agent-actions/{action_id}/approve")
    def approve_agent_action(action_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        return _decide_agent_action(action_id, "approve", db, current_user)

    @app.post("/agent-actions/{action_id}/reject")
    def reject_agent_action(action_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        return _decide_agent_action(action_id, "reject", db, current_user)
