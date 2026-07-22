"""Machine reliability — MTBF / MTTR / availability from downtime (ADR-0007).

The downtime read-model answers *what's stopping my machines* (a 7-day Pareto of
reasons and counts). This one reframes the same downtime record into the two
classic reliability-engineering KPIs the maintenance planner actually manages by:

  * MTTR — Mean Time To Repair: how long a stoppage lasts, on average
            (total repair minutes / number of failures).
  * MTBF — Mean Time Between Failures: how long a machine runs between
            stoppages (operating minutes / number of failures).

From those it derives per-machine availability and ranks the fleet least-reliable
first, so triage sees the machine dragging plant reliability down — not just the
one that happened to stop most this week. A read-model over downtime_logs +
machines, auto-scoped to the tenant (ADR-0002); it adds no storage.

Time basis: calendar minutes over the window (24h/day). Operating time is that
calendar span less the machine's own repair minutes, which keeps MTBF and
availability internally consistent (availability = MTBF / (MTBF + MTTR)) without
needing a shift calendar we don't have. A longer window than the downtime card's
(30 days) — reliability is a slow-moving, monthly metric.

``build_machine_reliability`` drills into one machine from that ranking: the same
KPIs read against the fleet, its own failure modes, a weekly trend, the failures
themselves, and the maintenance already booked against it.
"""
from collections import Counter
from datetime import datetime, timedelta

import models
# The one correct free-text duration parser ("2 hrs 15 min" -> 135). Aliased so the
# call sites below read unchanged. A local leading-digit regex used to live here and
# read every hour-format stoppage as minutes, which understated MTTR ~60x and
# inverted the least-reliable ranking.
from duration import parse_duration_to_minutes as _duration_minutes
from ai.maintenance import OPEN_STATUSES, PRIORITY_ORDER

name = "reliability"

WINDOW_DAYS = 30
WEEKS = 4          # weekly trend buckets in the machine drill-down
TOP_N = 8

def _mtbf_hours(operating_minutes: float, failures: int):
    """Mean operating time between failures, in hours. Undefined (None) with no
    failures — a machine that never stopped has no *between*-failure interval."""
    if failures <= 0:
        return None
    return round(operating_minutes / failures / 60, 1)


def _window_logs(db, start):
    """Downtime records inside the window. Filtered in SQL and again in Python so
    rows with a NULL created_at can't slip through."""
    return [
        d for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= start).all()
        if d.created_at and d.created_at >= start
    ]


def _fleet_stats(failures, repair_minutes, machine_count):
    """Fleet-level MTBF / MTTR / availability from the per-machine rollups — the
    one definition shared by the summary and the drill-down's fleet baseline, so
    the two never disagree and the drill-down needn't recompute the whole summary."""
    window_minutes = WINDOW_DAYS * 24 * 60
    total_failures = sum(failures.values())
    total_repair = sum(repair_minutes.values())
    fleet_calendar = window_minutes * machine_count
    fleet_operating = max(0, fleet_calendar - total_repair)
    return {
        "total_failures": total_failures,
        "total_repair": total_repair,
        "mttr": round(total_repair / total_failures, 1) if total_failures else 0.0,
        "mtbf": _mtbf_hours(fleet_operating, total_failures),
        "availability": round(fleet_operating / fleet_calendar * 100, 1) if fleet_calendar else 100.0,
    }


def _rank_rows(db, logs):
    """Per-machine reliability rows over the window, least reliable first — the
    ranking shared by the fleet summary and the single-machine drill-down, so
    both agree on who is worst. Returns (rows, failures, repair_minutes)."""
    window_minutes = WINDOW_DAYS * 24 * 60
    machines = db.query(models.Machine).all()

    # Roll failures + repair minutes up per machine.
    failures: Counter = Counter()
    repair_minutes: dict = {}
    for d in logs:
        if d.machine_id is None:
            continue
        failures[d.machine_id] += 1
        repair_minutes[d.machine_id] = repair_minutes.get(d.machine_id, 0) + _duration_minutes(d.duration)

    rows = []
    for m in machines:
        f = failures.get(m.id, 0)
        repair = repair_minutes.get(m.id, 0)
        operating = max(0, window_minutes - repair)
        availability = round(operating / window_minutes * 100, 1) if window_minutes else 0.0
        rows.append({
            "machine_id": m.id,
            "name": m.name,
            "line": m.line or "",
            "failures": f,
            "repair_minutes": repair,
            "mttr_minutes": round(repair / f, 1) if f else 0.0,
            "mtbf_hours": _mtbf_hours(operating, f),
            "availability": availability,
        })

    # Least reliable first: most failures, then longest total downtime, then lowest
    # availability. Machines with no failures (perfectly reliable) sink to the end.
    rows.sort(key=lambda r: (-r["failures"], -r["repair_minutes"], r["availability"]))
    return rows, failures, repair_minutes


def _mode_pareto(logs) -> list:
    """Failure modes ranked by repair time — where the maintenance hours go."""
    mode_minutes: Counter = Counter()
    mode_count: Counter = Counter()
    for d in logs:
        reason = (d.reason or "Unknown").strip() or "Unknown"
        mode_minutes[reason] += _duration_minutes(d.duration)
        mode_count[reason] += 1
    return [
        {"reason": r, "count": mode_count[r], "minutes": mins}
        for r, mins in mode_minutes.most_common(TOP_N)
    ]


def build_reliability_summary(db, tenant: str) -> dict:
    """Fleet reliability over the last 30 days: fleet MTBF / MTTR / availability,
    a per-machine breakdown (least reliable first), the reliability bottleneck,
    and the failure-mode Pareto by repair time. downtime_logs and machines are
    auto-scoped (ADR-0002). Empty-safe: zeros, no divide-by-zero."""
    now = datetime.utcnow()
    start = now - timedelta(days=WINDOW_DAYS)

    logs = _window_logs(db, start)
    # _rank_rows already loads the machines (one row per machine), so len(rows) is
    # the machine count — no need for a second db.query(Machine).all() here.
    rows, failures, repair_minutes = _rank_rows(db, logs)
    stats = _fleet_stats(failures, repair_minutes, len(rows))

    # The reliability bottleneck: the machine actually pulling the fleet down.
    bottleneck = rows[0] if rows and rows[0]["failures"] > 0 else None

    return {
        "days": WINDOW_DAYS,
        "machines_tracked": len(rows),
        "total_failures": stats["total_failures"],
        "total_repair_minutes": stats["total_repair"],
        "mttr_minutes": stats["mttr"],
        "mtbf_hours": stats["mtbf"],
        "availability": stats["availability"],
        "by_machine": rows[:TOP_N],
        "bottleneck": bottleneck,
        # Failure-mode Pareto by repair time — where the maintenance hours go.
        "top_modes": _mode_pareto(logs),
    }


def build_machine_reliability(db, tenant: str, machine_id: int) -> dict:
    """Drill-down for one machine: its 30-day MTBF / MTTR / availability against
    the fleet, where it ranks, its own failure-mode Pareto, a weekly failure
    trend (is it getting worse?), the failures themselves, and the maintenance
    already scheduled against it — so "this machine is the bottleneck" turns into
    "and here is what's booked to fix it". Composes downtime_logs + machines +
    maintenance_tasks (auto-scoped, ADR-0002); adds no storage. Returns
    ``found: False`` with a zeroed shape when the machine isn't in the tenant."""
    now = datetime.utcnow()
    start = now - timedelta(days=WINDOW_DAYS)
    today = now.date()

    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    logs = _window_logs(db, start)
    rows, failures, repair_minutes = _rank_rows(db, logs)
    # Fleet baseline from the rows we already have — the same numbers the summary
    # returns, without recomputing the whole summary (which would repeat the
    # downtime scan, the machine load and the ranking).
    fleet = _fleet_stats(failures, repair_minutes, len(rows))

    row = next((r for r in rows if r["machine_id"] == machine_id), None)
    if machine is None or row is None:
        return {
            "found": False, "machine_id": machine_id, "name": None, "line": "",
            "status": None, "days": WINDOW_DAYS,
            "failures": 0, "repair_minutes": 0, "mttr_minutes": 0.0,
            "mtbf_hours": None, "availability": 100.0,
            "rank": None, "machines_tracked": len(rows),
            "fleet_mttr_minutes": fleet["mttr"],
            "fleet_availability": fleet["availability"],
            "top_modes": [], "weekly": [], "trend": "steady",
            "recent_failures": 0, "prior_failures": 0,
            "hours_since_last_failure": None, "overdue_vs_mtbf": False,
            "failures_log": [], "maintenance": {"open": 0, "overdue": 0, "tasks": []},
        }

    mine = [d for d in logs if d.machine_id == machine_id]

    # Rank among the fleet on the shared least-reliable-first ordering (1 = worst).
    rank = next(i for i, r in enumerate(rows, start=1) if r["machine_id"] == machine_id)

    # Weekly failure trend over the last 4 whole weeks (28 of the 30 days) — the
    # sparkline that says "getting worse" faster than any single number.
    weekly = []
    for w in range(WEEKS - 1, -1, -1):
        w_start = now - timedelta(days=7 * (w + 1))
        w_end = now - timedelta(days=7 * w)
        bucket = [d for d in mine if w_start <= d.created_at < w_end]
        weekly.append({
            "week_start": w_start.date().isoformat(),
            "failures": len(bucket),
            "minutes": sum(_duration_minutes(d.duration) for d in bucket),
        })

    # Direction of travel: this half of the window against the previous half.
    half = now - timedelta(days=WINDOW_DAYS / 2)
    recent_failures = sum(1 for d in mine if d.created_at >= half)
    prior_failures = len(mine) - recent_failures
    trend = ("worsening" if recent_failures > prior_failures
             else "improving" if recent_failures < prior_failures else "steady")

    # Time since the last stoppage, read against MTBF: past its own mean interval
    # is the machine that is statistically due to stop again.
    last = max((d.created_at for d in mine), default=None)
    hours_since = round((now - last).total_seconds() / 3600, 1) if last else None
    overdue_vs_mtbf = bool(
        hours_since is not None and row["mtbf_hours"] and hours_since > row["mtbf_hours"]
    )

    failures_log = [{
        "reason": (d.reason or "Unknown").strip() or "Unknown",
        "minutes": _duration_minutes(d.duration),
        "notes": d.notes,
        "at": d.created_at.isoformat(),
    } for d in sorted(mine, key=lambda d: d.created_at, reverse=True)[:TOP_N]]

    # What's already booked against it — open maintenance, overdue first.
    tasks = (db.query(models.MaintenanceTask)
             .filter(models.MaintenanceTask.machine_id == machine_id,
                     models.MaintenanceTask.status.in_(OPEN_STATUSES)).all())
    tasks.sort(key=lambda t: (0 if (t.planned_date and t.planned_date < today) else 1,
                              PRIORITY_ORDER.get(t.priority, 2),
                              t.planned_date or today))
    task_rows = [{
        "task_no": t.task_no,
        "task_type": t.task_type,
        "priority": t.priority or "Medium",
        "status": t.status,
        "planned_date": t.planned_date.isoformat() if t.planned_date else None,
        "overdue": bool(t.planned_date and t.planned_date < today),
    } for t in tasks[:TOP_N]]

    return {
        "found": True,
        "machine_id": machine_id,
        "name": row["name"],
        "line": row["line"],
        "status": machine.status,
        "days": WINDOW_DAYS,
        "failures": row["failures"],
        "repair_minutes": row["repair_minutes"],
        "mttr_minutes": row["mttr_minutes"],
        "mtbf_hours": row["mtbf_hours"],
        "availability": row["availability"],
        "rank": rank,
        "machines_tracked": len(rows),
        "fleet_mttr_minutes": fleet["mttr"],
        "fleet_availability": fleet["availability"],
        "top_modes": _mode_pareto(mine),
        "weekly": weekly,
        "trend": trend,
        "recent_failures": recent_failures,
        "prior_failures": prior_failures,
        "hours_since_last_failure": hours_since,
        "overdue_vs_mtbf": overdue_vs_mtbf,
        "failures_log": failures_log,
        "maintenance": {
            "open": len(tasks),
            "overdue": sum(1 for t in tasks if t.planned_date and t.planned_date < today),
            "tasks": task_rows,
        },
    }
