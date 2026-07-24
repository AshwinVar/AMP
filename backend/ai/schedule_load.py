"""Schedule load board — the forward-looking machine schedule (ADR-0007).

Schedule adherence (``ai/schedule.py``) looks *backward*: of what we planned to
make, how much did we? This read-model looks *forward* at the other scheduling
table — ``production_schedules``, the dispatch board of jobs booked onto machines
and shifts — and answers the planner's morning question: "what's coming up, where
is the load piling up, and what has already slipped?"

For the next 7 days it rolls the open (not-yet-done) schedule entries up by day,
by machine and by shift — each breakdown summing to the same headline minutes and
job count — surfaces the busiest machine and the peak day, and separately flags
the schedules that have slipped: open entries already past their scheduled date,
plus any explicitly marked Delayed, with a chase list. A read-model over
production_schedules (+ machines for labels) — auto-scoped to the tenant
(ADR-0002); it adds no storage. Bounded in SQL to a lookback/horizon window on
scheduled_date so the board stays a range scan as the table grows.

Honesty notes:
  * "Load" is booked minutes/jobs, not utilisation — there is no per-machine
    daily capacity in the data, so this never claims a machine is over- or
    under-capacity, only where the booked work concentrates.
  * The forward-load window is today .. today+7; an entry that was due *before*
    today is not counted as upcoming load — it is surfaced separately as slipped
    backlog, so the timing label stays honest.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import models

name = "schedule_load"

HORIZON_DAYS = 7        # forward load window: today .. today+7
LOOKBACK_DAYS = 30      # how far back an open, past-due entry still counts as slipped backlog
TOP_N = 10
# Statuses that mean the schedule entry is closed out — everything else is still
# live work on the board.
DONE_STATUSES = {"completed", "complete", "done", "closed", "cancelled", "canceled"}
UNASSIGNED = "Unassigned"


def _is_done(status) -> bool:
    return (status or "").strip().lower() in DONE_STATUSES


def _is_delayed(status) -> bool:
    return (status or "").strip().lower() == "delayed"


def build_schedule_load(db, tenant: str) -> dict:
    """Forward schedule load over the next 7 days plus the slipped backlog: the
    booked jobs and minutes, a per-day / per-machine / per-shift breakdown (each
    reconciling to the same headline), the busiest machine and peak day, the
    status mix, the overdue and Delayed counts, and the entries to chase.
    production_schedules and machines are auto-scoped (ADR-0002). Empty-safe:
    zeros, no divide-by-zero, no max() on an empty sequence."""
    today = datetime.utcnow().date()
    horizon_end = today + timedelta(days=HORIZON_DAYS)
    lookback_start = today - timedelta(days=LOOKBACK_DAYS)

    # Bounded in SQL: the slippage lookback through the forward horizon. It is
    # auto-scoped (ADR-0002); scheduled_date is indexed (main.py) so this stays a
    # range scan rather than a full-table read on every poll.
    schedules = (db.query(models.ProductionSchedule)
                 .filter(models.ProductionSchedule.scheduled_date >= lookback_start,
                         models.ProductionSchedule.scheduled_date <= horizon_end).all())

    machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in machines}
    lines = {m.id: (m.line or "") for m in machines}

    by_status = Counter((s.status or "Unknown").strip() or "Unknown" for s in schedules)

    # Forward-load accumulators (open entries scheduled today .. today+7).
    horizon_days = [today + timedelta(days=i) for i in range(HORIZON_DAYS + 1)]
    day_jobs = {d: 0 for d in horizon_days}
    day_minutes = {d: 0 for d in horizon_days}
    machine_agg: dict = defaultdict(lambda: {"jobs": 0, "minutes": 0})
    shift_agg: dict = defaultdict(lambda: {"jobs": 0, "minutes": 0})
    scheduled_jobs = 0
    scheduled_minutes = 0

    # Slippage accumulators (open entries past their date, or marked Delayed).
    overdue = 0
    delayed = 0
    chase = []

    open_count = 0
    for s in schedules:
        if _is_done(s.status):
            continue
        open_count += 1
        mins = s.estimated_minutes or 0
        date = s.scheduled_date

        # Forward load: only entries actually dated inside the horizon.
        if date is not None and today <= date <= horizon_end:
            scheduled_jobs += 1
            scheduled_minutes += mins
            day_jobs[date] += 1
            day_minutes[date] += mins
            mid = s.machine_id                       # None -> Unassigned bucket, so the
            machine_agg[mid]["jobs"] += 1            # per-machine parts still sum to the whole
            machine_agg[mid]["minutes"] += mins
            shift = (s.shift_name or "—").strip() or "—"
            shift_agg[shift]["jobs"] += 1
            shift_agg[shift]["minutes"] += mins

        # Slippage: past-due (open) and/or explicitly Delayed.
        past_due = date is not None and date < today
        is_delayed = _is_delayed(s.status)
        if past_due:
            overdue += 1
        if is_delayed:
            delayed += 1
        if past_due or is_delayed:
            chase.append({
                "schedule_no": s.schedule_no,
                "machine": names.get(s.machine_id, UNASSIGNED) if s.machine_id is not None else UNASSIGNED,
                "shift": s.shift_name,
                "priority": s.priority or "Medium",
                "status": s.status,
                "scheduled_date": date.isoformat() if date else None,
                "planned_quantity": s.planned_quantity or 0,
                "estimated_minutes": mins,
                "days_overdue": (today - date).days if past_due else 0,
                "delayed": is_delayed,
            })

    by_machine = sorted(
        [{"machine_id": mid,
          "name": names.get(mid, UNASSIGNED) if mid is not None else UNASSIGNED,
          "line": lines.get(mid, "") if mid is not None else "",
          "jobs": a["jobs"], "minutes": a["minutes"]}
         for mid, a in machine_agg.items()],
        key=lambda m: (m["minutes"], m["jobs"]), reverse=True,
    )
    by_day = [{"date": d.isoformat(), "jobs": day_jobs[d], "minutes": day_minutes[d]}
              for d in horizon_days]
    by_shift = sorted(
        [{"shift": sh, "jobs": a["jobs"], "minutes": a["minutes"]} for sh, a in shift_agg.items()],
        key=lambda x: x["shift"],
    )

    busiest_machine = by_machine[0] if by_machine else None
    peak_day = max(by_day, key=lambda d: d["minutes"], default=None)
    if peak_day is not None and peak_day["minutes"] == 0:
        peak_day = None                              # no forward load -> no peak day

    # Chase list: most overdue first, then a Delayed-but-not-yet-due entry, then
    # by soonest scheduled date. `needs_attention` is the TRUE count (the chase
    # rows are a capped page of it, not the total).
    chase.sort(key=lambda c: (-c["days_overdue"], 0 if c["delayed"] else 1,
                              c["scheduled_date"] or "9999-12-31"))
    needs_attention = len(chase)

    if not schedules:
        verdict, tone = "Nothing on the schedule board in the window.", "warn"
    elif overdue:
        tail = (f", {scheduled_jobs} job{'s' if scheduled_jobs != 1 else ''} booked over the next "
                f"{HORIZON_DAYS} days" if scheduled_jobs else "")
        verdict = (f"{overdue} schedule{'s' if overdue != 1 else ''} past due and still open{tail}.")
        tone = "bad"
    elif scheduled_jobs == 0:
        verdict, tone = f"No open jobs scheduled in the next {HORIZON_DAYS} days.", "warn"
    else:
        verdict = (f"{scheduled_jobs} job{'s' if scheduled_jobs != 1 else ''} "
                   f"({scheduled_minutes:,} min) booked over the next {HORIZON_DAYS} days, "
                   f"nothing overdue.")
        tone = "good"

    return {
        "as_of": today.isoformat(),
        "horizon_days": HORIZON_DAYS,
        "lookback_days": LOOKBACK_DAYS,
        "total": len(schedules),
        "open": open_count,
        "scheduled_jobs": scheduled_jobs,
        "scheduled_minutes": scheduled_minutes,
        "by_status": [{"status": st, "count": c} for st, c in by_status.most_common()],
        "by_machine": by_machine[:TOP_N],
        "by_day": by_day,
        "by_shift": by_shift,
        "busiest_machine": busiest_machine,
        "peak_day": peak_day,
        "overdue": overdue,
        "delayed": delayed,
        "needs_attention": needs_attention,
        "chase": chase[:TOP_N],
        "verdict": verdict,
        "tone": tone,
    }
