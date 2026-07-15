"""Machine Health twin — a live per-machine read-model (ADR-0006).

Composes one snapshot per machine from signals the platform already produces:
current state, a health score derived from predictive risk, recent downtime, and
the open maintenance tasks and pending agent actions targeting it. A read-model
over existing tables (adds no storage). Tenant-scoped explicitly for the tables
that are only stamped (agent_actions), and via the auto-scoping layer for the
rest (ADR-0002).
"""
import models
from ai import prediction

name = "twin"


def _band(health: int) -> str:
    if health >= 80:
        return "Healthy"
    if health >= 55:
        return "Watch"
    if health >= 35:
        return "At risk"
    return "Critical"


def _machine_twin(db, machine, risk, tenant) -> dict:
    score = int(risk["risk_score"]) if risk else 0
    health = max(0, 100 - score)
    recent_downtime = (
        db.query(models.DowntimeLog)
        .filter(models.DowntimeLog.machine_id == machine.id)
        .order_by(models.DowntimeLog.id.desc())
        .limit(3).all()
    )
    open_tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.machine_id == machine.id,
                models.MaintenanceTask.status.in_(("Proposed", "Open")))
        .count()
    )
    pending_actions = (
        db.query(models.AgentAction)
        .filter(models.AgentAction.tenant_code == tenant,
                models.AgentAction.related_machine_id == machine.id,
                models.AgentAction.status == "Proposed")
        .count()
    )
    return {
        "machine_id": machine.id,
        "name": machine.name,
        "status": machine.status,
        "utilization": machine.utilization,
        "downtime": machine.downtime,
        "health_score": health,
        "health_band": _band(health),
        "risk_score": score,
        "risk_level": risk["risk_level"] if risk else "Low",
        "top_reason": (risk["reasons"][0] if risk and risk.get("reasons") else "no major risk indicators"),
        "open_maintenance_tasks": open_tasks,
        "pending_agent_actions": pending_actions,
        "recent_downtime": [{"reason": d.reason, "duration": d.duration} for d in recent_downtime],
    }


def build_twins(db, tenant: str):
    """Live health twin for every machine of the tenant, worst health first."""
    machines = db.query(models.Machine).order_by(models.Machine.id).all()
    risks = {r["machine_id"]: r for r in prediction.assess_from_db(db)}
    twins = [_machine_twin(db, m, risks.get(m.id), tenant) for m in machines]
    twins.sort(key=lambda t: t["health_score"])
    return twins


# ── Single-machine detail (the drill-down cockpit) ─────────────────
def _iso(dt):
    return dt.isoformat() if dt else None


def _timeline(db, machine_id, tenant):
    """One newest-first history for a machine, merging the three things that
    happen to it — downtime, maintenance tasks, and agent actions — into a
    common shape. Downtime/tasks are auto-scoped (ADR-0002); agent actions are
    only stamped, so they are filtered by tenant explicitly."""
    events = []
    for d in (db.query(models.DowntimeLog)
              .filter(models.DowntimeLog.machine_id == machine_id)
              .order_by(models.DowntimeLog.id.desc()).limit(25).all()):
        events.append({"kind": "downtime", "at": _iso(d.created_at),
                       "title": f"Downtime — {d.reason}", "detail": d.duration or "", "status": None})
    for t in (db.query(models.MaintenanceTask)
              .filter(models.MaintenanceTask.machine_id == machine_id)
              .order_by(models.MaintenanceTask.id.desc()).limit(25).all()):
        events.append({"kind": "task", "at": _iso(t.created_at),
                       "title": f"{t.task_type} · {t.priority}", "detail": t.task_no, "status": t.status})
    for a in (db.query(models.AgentAction)
              .filter(models.AgentAction.related_machine_id == machine_id,
                      models.AgentAction.tenant_code == tenant)
              .order_by(models.AgentAction.id.desc()).limit(25).all()):
        events.append({"kind": "action", "at": _iso(a.created_at),
                       "title": f"{a.agent} agent · {a.action_type}", "detail": a.summary, "status": a.status})
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return events[:30]


def _open_actions(db, machine_id, tenant):
    """Agent actions still awaiting a human decision for this machine."""
    rows = (db.query(models.AgentAction)
            .filter(models.AgentAction.related_machine_id == machine_id,
                    models.AgentAction.tenant_code == tenant,
                    models.AgentAction.status == "Proposed")
            .order_by(models.AgentAction.id.desc()).all())
    return [{"id": a.id, "agent": a.agent, "action_type": a.action_type, "summary": a.summary,
             "severity": a.severity, "created_at": _iso(a.created_at)} for a in rows]


def build_machine_detail(db, tenant: str, machine_id: int):
    """A single-machine cockpit: the twin snapshot plus the full risk-factor
    breakdown, a unified event timeline, and the agent actions awaiting approval.
    Returns None when the machine isn't the tenant's (the caller then 404s)."""
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if not machine:
        return None
    risk = prediction.risk_for_machine(db, machine_id)
    detail = _machine_twin(db, machine, risk, tenant)
    detail["risk_factors"] = list(risk["reasons"]) if risk and risk.get("reasons") else []
    detail["timeline"] = _timeline(db, machine_id, tenant)
    detail["open_actions"] = _open_actions(db, machine_id, tenant)
    return detail
