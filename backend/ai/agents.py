"""AI agents — from recommending to acting (ADR-0004).

An agent observes the event stream and takes a bounded, autonomous action, not
just an advisory. The first, the Maintenance agent, escalates the platform's own
judgement: when a machine's failure risk is Critical it opens a maintenance task
(a real work item) instead of only recommending one — idempotently and
tenant-scoped. The action is bounded (create a task), auditable (the task notes
cite the risk) and reversible (a task can be closed).
"""
from datetime import datetime

import models
from ai import prediction
from events import ProductionCompleted, DowntimeStarted, event_bus

# Autonomy threshold: act only on Critical risk (see predictive_engine.classify_risk).
CRITICAL_RISK = 75

_AUTO_TASK_TYPE = "Predictive (auto)"


def _open_auto_task_exists(db, machine_id) -> bool:
    return (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.machine_id == machine_id,
            models.MaintenanceTask.task_type == _AUTO_TASK_TYPE,
            models.MaintenanceTask.status == "Open",
        )
        .first()
        is not None
    )


def _open_maintenance_task(db, risk) -> bool:
    """Open one maintenance task for a critical-risk machine. Returns True if a
    task was created (False if an open auto-task already exists)."""
    machine_id = risk["machine_id"]
    if _open_auto_task_exists(db, machine_id):
        return False
    now = datetime.utcnow()
    db.add(models.MaintenanceTask(
        task_no=f"AUTO-MAINT-{machine_id}-{int(now.timestamp())}",
        machine_id=machine_id,
        task_type=_AUTO_TASK_TYPE,
        priority="Critical",
        assigned_to="Maintenance team",
        planned_date=now.date(),
        status="Open",
        notes=(
            f"Auto-opened by the Maintenance agent: {risk['machine_name']} at risk "
            f"{risk['risk_score']} ({', '.join(risk['reasons'])})."
        ),
    ))
    return True


def act_on_machine_event(event, db) -> None:
    """Agent step: reassess the machine and, if risk is Critical, open a task."""
    machine_id = getattr(event, "machine_id", None)
    if machine_id is None:
        return
    risk = prediction.risk_for_machine(db, machine_id)
    if risk and risk["risk_score"] >= CRITICAL_RISK:
        _open_maintenance_task(db, risk)


def register(bus=event_bus) -> None:
    """Wire the Maintenance agent to the maintenance-relevant events."""
    bus.subscribe(ProductionCompleted, act_on_machine_event)
    bus.subscribe(DowntimeStarted, act_on_machine_event)
