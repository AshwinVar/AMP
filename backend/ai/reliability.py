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
"""
import re
from collections import Counter
from datetime import datetime, timedelta

import models

name = "reliability"

WINDOW_DAYS = 30
TOP_N = 8
_DIGITS = re.compile(r"\d+")


def _duration_minutes(duration) -> int:
    """Downtime durations are free-text ("120 min"); pull the leading number so
    repair time totals. Unparseable / empty -> 0."""
    if not duration:
        return 0
    m = _DIGITS.search(str(duration))
    return int(m.group()) if m else 0


def _mtbf_hours(operating_minutes: float, failures: int):
    """Mean operating time between failures, in hours. Undefined (None) with no
    failures — a machine that never stopped has no *between*-failure interval."""
    if failures <= 0:
        return None
    return round(operating_minutes / failures / 60, 1)


def build_reliability_summary(db, tenant: str) -> dict:
    """Fleet reliability over the last 30 days: fleet MTBF / MTTR / availability,
    a per-machine breakdown (least reliable first), the reliability bottleneck,
    and the failure-mode Pareto by repair time. downtime_logs and machines are
    auto-scoped (ADR-0002). Empty-safe: zeros, no divide-by-zero."""
    now = datetime.utcnow()
    start = now - timedelta(days=WINDOW_DAYS)
    window_minutes = WINDOW_DAYS * 24 * 60

    logs = [
        d for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= start).all()
        if d.created_at and d.created_at >= start
    ]
    machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in machines}
    line_of = {m.id: (m.line or "") for m in machines}

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
            "name": names.get(m.id, f"#{m.id}"),
            "line": line_of.get(m.id, ""),
            "failures": f,
            "repair_minutes": repair,
            "mttr_minutes": round(repair / f, 1) if f else 0.0,
            "mtbf_hours": _mtbf_hours(operating, f),
            "availability": availability,
        })

    # Least reliable first: most failures, then longest total downtime, then lowest
    # availability. Machines with no failures (perfectly reliable) sink to the end.
    rows.sort(key=lambda r: (-r["failures"], -r["repair_minutes"], r["availability"]))

    total_failures = sum(failures.values())
    total_repair = sum(repair_minutes.values())
    # Fleet operating time = every machine's calendar span less its own repair time.
    fleet_calendar = window_minutes * len(machines)
    fleet_operating = max(0, fleet_calendar - total_repair)

    fleet_mttr = round(total_repair / total_failures, 1) if total_failures else 0.0
    fleet_mtbf = _mtbf_hours(fleet_operating, total_failures)
    fleet_availability = round(fleet_operating / fleet_calendar * 100, 1) if fleet_calendar else 100.0

    # The reliability bottleneck: the machine actually pulling the fleet down.
    bottleneck = rows[0] if rows and rows[0]["failures"] > 0 else None

    # Failure-mode Pareto by repair time — where the maintenance hours go.
    mode_minutes: Counter = Counter()
    mode_count: Counter = Counter()
    for d in logs:
        reason = (d.reason or "Unknown").strip() or "Unknown"
        mode_minutes[reason] += _duration_minutes(d.duration)
        mode_count[reason] += 1
    top_modes = [
        {"reason": r, "count": mode_count[r], "minutes": mins}
        for r, mins in mode_minutes.most_common(TOP_N)
    ]

    return {
        "days": WINDOW_DAYS,
        "machines_tracked": len(machines),
        "total_failures": total_failures,
        "total_repair_minutes": total_repair,
        "mttr_minutes": fleet_mttr,
        "mtbf_hours": fleet_mtbf,
        "availability": fleet_availability,
        "by_machine": rows[:TOP_N],
        "bottleneck": bottleneck,
        "top_modes": top_modes,
    }
