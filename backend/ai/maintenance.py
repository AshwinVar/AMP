"""Maintenance work summary — the open maintenance load at a glance (ADR-0007).

Answers "what maintenance is outstanding, what's overdue, and what is the
Maintenance agent waiting on me to approve?": the open tasks (agent-proposed and
manual) by priority, the overdue count, the approvals pending, and the specific
tasks to do next (overdue first, then by priority). A read-model over
maintenance_tasks — auto-scoped to the tenant (ADR-0002); it adds no storage.

``build_maintenance_execution`` asks the harder question of the same table: not
"what is open" but *are we keeping the maintenance promise?* — of the work we
actually completed, how much landed on or before its planned date (PM
compliance), how much of it was planned versus firefighting (the reactive
ratio), and how stale the overdue backlog has gone. Reliability
(``ai/reliability.py``) measures what the machines do to us; this measures what
we do about it.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import models

name = "maintenance"

TOP_N = 8
OPEN_STATUSES = ("Proposed", "Open", "In Progress")
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
_PRIORITIES = ["Critical", "High", "Medium", "Low"]

# Execution window — maintenance is a slow-moving, monthly discipline, so the
# same 30 days the reliability read-model uses (not the 7-day pillar window).
EXECUTION_WINDOW_DAYS = 30
COMPLIANCE_TARGET = 90          # % of completions on or before plan we call healthy
COMPLIANCE_FLOOR = 70           # below this the discipline has broken down
MIN_JUDGED = 3                  # fewer dated completions than this and one late job
                                # would swing the rate — report it, don't condemn it
# Task types that mean "something already went wrong". Everything else —
# Preventive, Predictive, Lubrication, Calibration, inspections — is planned
# work. Matched as substrings, lowercased, so "Predictive (auto)" reads planned
# while the agent's defect-driven "Quality (auto)" / "Yield (auto)" read reactive.
REACTIVE_HINTS = ("corrective", "breakdown", "emergency", "repair", "unplanned",
                  "fault", "quality", "yield")
AGING_BUCKETS = [("1-7 days", 1, 7), ("8-30 days", 8, 30), ("30+ days", 31, None)]


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


def is_reactive(task_type) -> bool:
    """True when the task type says the work was triggered by a failure rather
    than scheduled ahead of it. Unknown / blank types read as planned — we only
    call it firefighting when the record says so."""
    t = (task_type or "").lower()
    return any(h in t for h in REACTIVE_HINTS)


def _aging_bucket(days_overdue: int) -> str:
    for label, lo, hi in AGING_BUCKETS:
        if days_overdue >= lo and (hi is None or days_overdue <= hi):
            return label
    return AGING_BUCKETS[0][0]


def build_maintenance_execution(db, tenant: str) -> dict:
    """PM compliance and backlog health over the last 30 days: of the maintenance
    completed in the window, what share landed on or before its planned date, how
    late the rest ran, how much of the work was planned versus reactive, and how
    old the overdue backlog has grown — with a worst-first per-machine breakdown
    and the tasks to chase. maintenance_tasks + machines are auto-scoped (ADR-0002);
    it adds no storage.

    Denominator note: compliance, the planned/reactive mix and maintenance
    downtime are all measured over the *completed* work dated inside the window —
    one consistent "what we actually did" set. Tasks marked Completed without a
    completion date can't be timed, so they are reported separately as
    ``undated_completions`` rather than silently scored as on-time."""
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=EXECUTION_WINDOW_DAYS - 1)
    all_tasks = db.query(models.MaintenanceTask).all()
    names = {m.id: m.name for m in db.query(models.Machine).all()}

    completed = [t for t in all_tasks if t.status == "Completed"]
    done = [t for t in completed if t.completed_date and cutoff <= t.completed_date <= today]
    undated = sum(1 for t in completed if not t.completed_date)

    # On time = finished on or before the planned date. A completion with no
    # planned date has no promise to keep, so it sits outside the ratio.
    timed = [t for t in done if t.planned_date]
    on_time = [t for t in timed if t.completed_date <= t.planned_date]
    late = [t for t in timed if t.completed_date > t.planned_date]
    days_late = [(t.completed_date - t.planned_date).days for t in late]
    compliance_rate = round(len(on_time) / len(timed) * 100) if timed else None

    reactive = [t for t in done if is_reactive(t.task_type)]
    planned_count = len(done) - len(reactive)
    planned_share = round(planned_count / len(done) * 100) if done else None

    # The open backlog and how long the overdue part has been sitting.
    open_tasks = [t for t in all_tasks if t.status in OPEN_STATUSES]
    overdue = [t for t in open_tasks if t.planned_date and t.planned_date < today]
    overdue_days = {t.task_no: (today - t.planned_date).days for t in overdue}
    aging: Counter = Counter(_aging_bucket(d) for d in overdue_days.values())
    oldest_days = max(overdue_days.values()) if overdue_days else None

    # Per-machine execution: who completes their maintenance on plan and who lets
    # it rot. Worst compliance first, then the biggest overdue backlog.
    agg: dict = defaultdict(lambda: {"completed": 0, "on_time": 0, "overdue": 0})
    for t in timed:
        if t.machine_id is not None:
            agg[t.machine_id]["completed"] += 1
            agg[t.machine_id]["on_time"] += 1 if t.completed_date <= t.planned_date else 0
    for t in overdue:
        if t.machine_id is not None:
            agg[t.machine_id]["overdue"] += 1
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"),
         "completed": a["completed"], "on_time": a["on_time"],
         "compliance_rate": round(a["on_time"] / a["completed"] * 100) if a["completed"] else None,
         "overdue": a["overdue"]}
        for mid, a in agg.items()
    ]
    # None (nothing completed to score) sorts as if perfect, so machines with a
    # real, poor compliance number surface above the merely-unmeasured.
    by_machine.sort(key=lambda m: (m["compliance_rate"] if m["compliance_rate"] is not None else 101,
                                   -m["overdue"]))

    chase = sorted(overdue, key=lambda t: (-overdue_days[t.task_no],
                                           PRIORITY_ORDER.get(t.priority, 2)))[:TOP_N]
    chase_rows = [{
        "task_no": t.task_no,
        "machine": names.get(t.machine_id, "—"),
        "task_type": t.task_type,
        "priority": t.priority or "Medium",
        "status": t.status,
        "planned_date": t.planned_date.isoformat() if t.planned_date else None,
        "days_overdue": overdue_days[t.task_no],
        "reactive": is_reactive(t.task_type),
    } for t in chase]

    if compliance_rate is None:
        verdict, tone = "No dated maintenance completed in the window.", "warn"
    elif compliance_rate >= COMPLIANCE_TARGET and not overdue:
        verdict, tone = f"{compliance_rate}% of maintenance completed on plan, nothing overdue.", "good"
    elif compliance_rate >= COMPLIANCE_TARGET:
        verdict, tone = (f"{compliance_rate}% completed on plan, but {len(overdue)} task"
                         f"{'s' if len(overdue) != 1 else ''} are now overdue.", "warn")
    elif compliance_rate >= COMPLIANCE_FLOOR:
        verdict, tone = f"Only {compliance_rate}% of maintenance landed on plan — slipping.", "warn"
    else:
        verdict, tone = (f"Maintenance discipline has broken down — {compliance_rate}% on plan"
                         f"{f', oldest overdue {oldest_days} days' if oldest_days else ''}.", "bad")

    # A plant that finished one job late is not a plant in crisis. Below the
    # minimum sample we still report the rate, but we don't call it a verdict.
    if tone == "bad" and len(timed) < MIN_JUDGED:
        verdict = (f"{compliance_rate}% on plan, but only {len(timed)} dated completion"
                   f"{'s' if len(timed) != 1 else ''} in {EXECUTION_WINDOW_DAYS} days — too thin to judge.")
        tone = "warn"

    return {
        "days": EXECUTION_WINDOW_DAYS,
        "completed": len(done),
        "timed": len(timed),
        "on_time": len(on_time),
        "late": len(late),
        "compliance_rate": compliance_rate,
        "target": COMPLIANCE_TARGET,
        "avg_days_late": round(sum(days_late) / len(days_late), 1) if days_late else 0.0,
        "worst_days_late": max(days_late) if days_late else 0,
        "undated_completions": undated,
        "planned_count": planned_count,
        "reactive_count": len(reactive),
        "planned_share": planned_share,
        "downtime_minutes": sum(t.downtime_minutes or 0 for t in done),
        "backlog": {
            "open": len(open_tasks),
            "overdue": len(overdue),
            "oldest_days": oldest_days,
            "aging": [{"bucket": label, "count": aging[label]}
                      for label, _, _ in AGING_BUCKETS if aging.get(label)],
        },
        "by_machine": by_machine[:TOP_N],
        "chase": chase_rows,
        "verdict": verdict,
        "tone": tone,
    }
