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
