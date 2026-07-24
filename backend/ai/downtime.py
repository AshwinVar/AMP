"""Downtime summary — a fleet-wide read-model over downtime (ADR-0007).

Answers the first question a plant manager asks each morning: *what's stopping
my machines?* Over the last week it rolls up the downtime events **and the time
lost** — the top reasons and worst machines ranked by minutes down (a single
six-hour breakdown outweighs a handful of two-minute micro-stops), plus a
day-by-day series. A read-model over downtime_logs — auto-scoped to the tenant
by the query layer (ADR-0002) in a request context; it adds no storage.

Minutes come from the one shared free-text duration parser
(``duration.parse_duration_to_minutes``: "2 hrs 15 min" -> 135), the same one
the reason drill-down uses, so the summary and its drill-down reconcile.
"""
from collections import Counter
from datetime import datetime, timedelta

import models
# The one correct free-text duration parser ("2 hrs 15 min" -> 135), aliased so the
# call sites read unchanged — a local leading-digit regex read hour formats as minutes.
from duration import parse_duration_to_minutes as _duration_minutes

name = "downtime"

WINDOW_DAYS = 7
TOP_N = 5

def _norm_reason(d) -> str:
    return (d.reason or "Unknown").strip() or "Unknown"


def build_downtime_summary(db, tenant: str) -> dict:
    """Fleet downtime over the last 7 days: total events and total minutes lost,
    the top reasons and worst machines (both ranked by minutes down, events as the
    tiebreak), a per-line rollup, and a daily series carrying both counts and
    minutes. downtime_logs and machines are auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    # Windowed in SQL (the log grows continuously); the set check keeps the exact
    # per-day semantics for any future-dated rows.
    start = datetime.combine(window[0], datetime.min.time())
    logs = [
        d for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= start).all()
        if d.created_at and d.created_at.date() in window_set
    ]

    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}

    # One pass over the window: parse each stoppage's duration once (via the shared
    # helper) and roll events + minutes up by reason, machine, line and day. Every
    # per-day bucket sees every in-window log, so sum(daily minutes) == total_minutes.
    total_minutes = 0
    reason_events: Counter = Counter()
    reason_minutes: Counter = Counter()
    mach_events: Counter = Counter()
    mach_minutes: Counter = Counter()
    line_events: Counter = Counter()
    line_minutes: Counter = Counter()
    day_events: Counter = Counter()
    day_minutes: Counter = Counter()
    for d in logs:
        mins = _duration_minutes(d.duration)
        total_minutes += mins
        r = _norm_reason(d)
        reason_events[r] += 1
        reason_minutes[r] += mins
        if d.machine_id is not None:
            mach_events[d.machine_id] += 1
            mach_minutes[d.machine_id] += mins
            ln = line_of.get(d.machine_id, "")
            if ln:
                line_events[ln] += 1
                line_minutes[ln] += mins
        day = d.created_at.date()
        day_events[day] += 1
        day_minutes[day] += mins

    # Ranked by time lost (the honest impact), events then name as deterministic
    # tiebreaks so equal-minute rows order stably.
    ranked_reasons = sorted(reason_events,
                            key=lambda r: (-reason_minutes[r], -reason_events[r], r))
    top_reasons = [{"reason": r, "count": reason_events[r], "minutes": reason_minutes[r]}
                   for r in ranked_reasons[:TOP_N]]

    ranked_machines = sorted(mach_events,
                             key=lambda mid: (-mach_minutes[mid], -mach_events[mid], mid))
    by_machine = [{"machine_id": mid, "name": names.get(mid, f"#{mid}"),
                   "count": mach_events[mid], "minutes": mach_minutes[mid]}
                  for mid in ranked_machines[:TOP_N]]

    by_line = [{"line": ln, "count": line_events[ln], "minutes": line_minutes[ln]}
               for ln in sorted(line_events)]

    daily = [{"date": dd.isoformat(), "count": day_events.get(dd, 0),
              "minutes": day_minutes.get(dd, 0)} for dd in window]

    return {
        "days": WINDOW_DAYS,
        "total_events": len(logs),
        "total_minutes": total_minutes,
        "top_reasons": top_reasons,
        "by_machine": by_machine,
        "by_line": by_line,
        "daily": daily,
    }


def build_downtime_reason(db, tenant: str, reason: str) -> dict:
    """Drill-down for a single downtime reason over the last 7 days: the totals
    (events and minutes lost), the machines it hits, a daily trend, and the most
    recent instances. Composes downtime_logs (auto-scoped, ADR-0002); adds no
    storage. Returns a zeroed shape when the reason has no events in the window."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    # Windowed in SQL (the log grows continuously); the set check keeps the exact
    # per-day semantics for any future-dated rows. Mirrors build_downtime_summary.
    start = datetime.combine(window[0], datetime.min.time())
    logs = [
        d for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= start).all()
        if d.created_at and d.created_at.date() in window_set and _norm_reason(d) == reason
    ]

    names = {m.id: m.name for m in db.query(models.Machine).all()}
    events = Counter(d.machine_id for d in logs if d.machine_id is not None)
    minutes_by_machine: dict = {}
    for d in logs:
        if d.machine_id is not None:
            minutes_by_machine[d.machine_id] = minutes_by_machine.get(d.machine_id, 0) + _duration_minutes(d.duration)
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), "count": c,
         "minutes": minutes_by_machine.get(mid, 0)}
        for mid, c in events.most_common(TOP_N)
    ]

    per_day = Counter(d.created_at.date() for d in logs)
    daily = [{"date": dd.isoformat(), "count": per_day.get(dd, 0)} for dd in window]

    recent = sorted(logs, key=lambda d: (d.created_at or datetime.min, d.id), reverse=True)[:10]
    instances = [{
        "id": d.id,
        "machine_id": d.machine_id,
        "machine": names.get(d.machine_id, f"#{d.machine_id}") if d.machine_id is not None else "—",
        "duration": d.duration,
        "minutes": _duration_minutes(d.duration),
        "notes": d.notes,
        "at": d.created_at.isoformat() if d.created_at else None,
    } for d in recent]

    return {
        "reason": reason,
        "days": WINDOW_DAYS,
        "total_events": len(logs),
        "total_minutes": sum(_duration_minutes(d.duration) for d in logs),
        "by_machine": by_machine,
        "daily": daily,
        "instances": instances,
    }
