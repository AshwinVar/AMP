"""Maintenance work summary — the open maintenance load at a glance (ADR-0007).

Answers "what maintenance is outstanding, what's overdue, and what is the
Maintenance agent waiting on me to approve?": the open tasks (agent-proposed and
manual) by priority, the overdue count, the approvals pending, and the specific
tasks to do next (overdue first, then by priority). A read-model over
maintenance_tasks — auto-scoped to the tenant (ADR-0002); it adds no storage.
"""
from collections import Counter
from datetime import datetime

import models

name = "maintenance"

TOP_N = 8
OPEN_STATUSES = ("Proposed", "Open", "In Progress")
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
_PRIORITIES = ["Critical", "High", "Medium", "Low"]


def build_maintenance_summary(db, tenant: str) -> dict:
    """The open maintenance load: counts by priority, overdue and pending-approval
    totals, and the tasks to do next (overdue first, then by priority, then
    soonest planned). maintenance_tasks and machines are auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    tasks = (db.query(models.MaintenanceTask)
             .filter(models.MaintenanceTask.status.in_(OPEN_STATUSES)).all())
    names = {m.id: m.name for m in db.query(models.Machine).all()}

    by_priority = Counter(t.priority or "Medium" for t in tasks)
    pending_approval = sum(1 for t in tasks if t.status == "Proposed")   # agent-proposed, awaiting a human
    overdue = sum(1 for t in tasks if t.planned_date and t.planned_date < today)

    def _sort_key(t):
        is_overdue = 0 if (t.planned_date and t.planned_date < today) else 1
        return (is_overdue, PRIORITY_ORDER.get(t.priority, 2), t.planned_date or today)

    # Open-task load per machine (most first), so triage knows where it's piling up.
    per_machine = Counter(t.machine_id for t in tasks if t.machine_id is not None)
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), "count": c}
        for mid, c in per_machine.most_common(TOP_N)
    ]

    top = sorted(tasks, key=_sort_key)[:TOP_N]
    rows = [{
        "task_no": t.task_no,
        "machine": names.get(t.machine_id, "—"),
        "task_type": t.task_type,
        "priority": t.priority or "Medium",
        "status": t.status,
        "planned_date": t.planned_date.isoformat() if t.planned_date else None,
        "overdue": bool(t.planned_date and t.planned_date < today),
        "proposed": t.status == "Proposed",
    } for t in top]

    return {
        "open": len(tasks),
        "pending_approval": pending_approval,
        "overdue": overdue,
        "by_priority": [{"priority": p, "count": by_priority[p]} for p in _PRIORITIES if by_priority.get(p)],
        "by_machine": by_machine,
        "tasks": rows,
    }
