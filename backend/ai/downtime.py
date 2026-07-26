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

``build_downtime_trend`` asks the question a single week's Pareto can't: not
*what stopped my machines* but *which way is downtime going, and who moved it?*
It compares the last 7 days of lost minutes against the 7 before — on the same
shared duration parser — and attributes the swing to specific machines and
reasons, so a plant sees downtime creeping up while it is still a few extra
stoppages rather than after a bad month. The quality read-model has the same
this-week-vs-last-week trend for scrap; this is its downtime twin.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import models
# The one correct free-text duration parser ("2 hrs 15 min" -> 135), aliased so the
# call sites read unchanged — a local leading-digit regex read hour formats as minutes.
from duration import parse_duration_to_minutes as _duration_minutes

name = "downtime"

WINDOW_DAYS = 7
TOP_N = 5

# Trend window — two WINDOW_DAYS halves, so "this week vs last week" over the same
# 7-day window the summary already reports.
TREND_HALVES = 2
TREND_WINDOW_DAYS = WINDOW_DAYS * TREND_HALVES
# Absolute change in lost minutes below which a week-to-week move is ordinary
# noise, not a trend — and the floor a machine/reason must clear to be named a
# mover. A single micro-stop shouldn't read as "downtime worsening".
MIN_MOVE_MINUTES = 30
# Fewer stoppages than this across BOTH halves and one long breakdown swings the
# whole number, so we report the move but don't call it a trend.
MIN_TREND_EVENTS = 4


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


def _half_of(day, today):
    """Which half a day falls in: 'current' = the last WINDOW_DAYS including
    today, 'prior' = the WINDOW_DAYS before that, None = outside the window.
    Mirrors ai.quality._half_of so the two trend cards split their windows the
    same way."""
    age = (today - day).days
    if 0 <= age < WINDOW_DAYS:
        return "current"
    if WINDOW_DAYS <= age < TREND_WINDOW_DAYS:
        return "prior"
    return None


def build_downtime_trend(db, tenant: str) -> dict:
    """Which way is downtime going, and who moved it? Compares the last 7 days of
    lost minutes against the 7 before — same shared duration parser as the
    summary — and attributes the swing to machines and reasons. A read-model over
    downtime_logs (+ machines for labels), auto-scoped to the tenant (ADR-0002);
    it adds no storage.

    Downtime *minutes* (not the raw event count) is the metric that moves: the
    summary already established that a six-hour breakdown outweighs a handful of
    micro-stops, so the trend is measured the same way. Every in-window log lands
    in exactly one day and one half, so the daily series sums back to the half
    totals (rule 3). Thin weeks (one big stoppage) are reported but not judged."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=n) for n in range(TREND_WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    # Windowed in SQL (the log grows continuously); the set/half check keeps exact
    # per-day semantics and drops any future-dated rows. Mirrors the summary.
    start = datetime.combine(window[0], datetime.min.time())
    logs = [
        d for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= start).all()
        if d.created_at and d.created_at.date() in window_set
    ]

    names = {m.id: m.name for m in db.query(models.Machine).all()}

    # One pass: parse each stoppage once, roll minutes + events up by day, half,
    # machine and reason.
    daily = {d: {"events": 0, "minutes": 0} for d in window}
    halves = {"current": {"events": 0, "minutes": 0},
              "prior": {"events": 0, "minutes": 0}}
    per_machine: dict = defaultdict(lambda: {
        "current": {"events": 0, "minutes": 0}, "prior": {"events": 0, "minutes": 0}})
    per_reason: dict = defaultdict(lambda: {"current": 0, "prior": 0})

    for d in logs:
        day = d.created_at.date()
        half = _half_of(day, today)
        if half is None:
            continue
        mins = _duration_minutes(d.duration)
        daily[day]["events"] += 1
        daily[day]["minutes"] += mins
        halves[half]["events"] += 1
        halves[half]["minutes"] += mins
        if d.machine_id is not None:
            per_machine[d.machine_id][half]["events"] += 1
            per_machine[d.machine_id][half]["minutes"] += mins
        per_reason[_norm_reason(d)][half] += mins

    series = [{"date": d.isoformat(), "events": daily[d]["events"], "minutes": daily[d]["minutes"]}
              for d in window]
    current, prior = halves["current"], halves["prior"]

    delta_minutes = current["minutes"] - prior["minutes"]
    # Percentage move is only meaningful against a non-zero prior week; a jump from
    # zero downtime is a real worsening but has no finite percentage.
    delta_pct = round(delta_minutes / prior["minutes"] * 100) if prior["minutes"] else None

    total_events = current["events"] + prior["events"]
    if current["minutes"] == 0 and prior["minutes"] == 0:
        direction = "none"
    elif delta_minutes > MIN_MOVE_MINUTES:
        direction = "worsening"
    elif delta_minutes < -MIN_MOVE_MINUTES:
        direction = "improving"
    else:
        direction = "steady"

    # A swing built on a couple of stoppages is arithmetic, not a trend.
    thin = 0 < total_events < MIN_TREND_EVENTS

    # Machine movers: biggest change in lost minutes, worsening then improving.
    machines = []
    for mid, a in per_machine.items():
        d_min = a["current"]["minutes"] - a["prior"]["minutes"]
        machines.append({
            "machine_id": mid, "name": names.get(mid, f"#{mid}"),
            "minutes": a["current"]["minutes"], "prior_minutes": a["prior"]["minutes"],
            "delta_minutes": d_min,
            "events": a["current"]["events"], "prior_events": a["prior"]["events"],
        })
    worsening_machines = sorted((m for m in machines if m["delta_minutes"] >= MIN_MOVE_MINUTES),
                                key=lambda m: m["delta_minutes"], reverse=True)[:TOP_N]
    improving_machines = sorted((m for m in machines if m["delta_minutes"] <= -MIN_MOVE_MINUTES),
                                key=lambda m: m["delta_minutes"])[:TOP_N]

    # Reason movers: which failure modes grew (or a brand-new one appeared).
    reason_movers = [
        {"reason": r, "minutes": a["current"], "prior_minutes": a["prior"],
         "delta_minutes": a["current"] - a["prior"],
         "is_new": a["prior"] == 0 and a["current"] > 0}
        for r, a in per_reason.items() if a["current"] or a["prior"]
    ]
    reason_movers.sort(key=lambda r: (r["delta_minutes"], r["minutes"]), reverse=True)

    now_min = current["minutes"]
    worst = worsening_machines[0] if worsening_machines else None
    pct_txt = f" ({abs(delta_pct)}%)" if delta_pct is not None else ""
    if direction == "none":
        verdict, tone = "No downtime in the last 14 days.", "good"
    elif thin:
        verdict, tone = (f"Downtime moved {delta_minutes:+} min to {now_min} min this week, but on "
                         f"{total_events} stoppage{'s' if total_events != 1 else ''} in 14 days — "
                         "too little to call a trend.", "warn")
    elif direction == "worsening":
        blame = f" — {worst['name']} drove it (+{worst['delta_minutes']} min)" if worst else ""
        verdict, tone = (f"Downtime up {delta_minutes} min{pct_txt} to {now_min} min "
                         f"week on week{blame}.", "bad")
    elif direction == "improving":
        verdict, tone = (f"Downtime down {abs(delta_minutes)} min{pct_txt} to {now_min} min "
                         "week on week.", "good")
    else:
        verdict, tone = (f"Downtime steady at {now_min} min ({delta_minutes:+} min week on week).", "good")

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "current": current,
        "prior": prior,
        "delta_minutes": delta_minutes,
        "delta_pct": delta_pct,
        "direction": direction,
        "thin_sample": thin,
        "move_threshold_minutes": MIN_MOVE_MINUTES,
        "series": series,
        "worsening_machines": worsening_machines,
        "improving_machines": improving_machines,
        "reason_movers": reason_movers[:TOP_N],
        "verdict": verdict,
        "tone": tone,
    }
