"""Operator performance — the labour read-model over job executions (ADR-0007).

Every pillar so far measures the machines, the materials or the paperwork; none
measures the people running the line. This read-model reframes the operator
terminal log (``operator_job_executions``) into the question a shift lead asks
about the crew: "who is running jobs, how much good product are they turning out,
and whose quality needs a look?"

Over the last week it rolls the executions up per operator — jobs run, how many
completed, the good and rejected units they booked, and their first-pass quality
rate — ranks the crew so the operator with the worst quality (on real volume)
surfaces first, and carries the plant totals the parts reconcile to. A read-model
over operator_job_executions — auto-scoped to the tenant (ADR-0002); it adds no
storage.

``build_operator_detail`` drills into one operator off that ranking — their
quality against the plant and where they rank, which machines they ran (so a poor
rate can be traced to the operator or the machine they were put on), a measured
average job time from the real started/completed timestamps, and their recent
jobs. It reuses this module's own quality-rate and ranking definitions, so the
plant baseline and the rank reconcile with the summary card the user clicked.

Honesty notes:
  * Quality rate is good / (good + rejected). An operator who booked no units yet
    has no rate — it is None, never a misleading 0%, and such an operator is never
    ranked "worst" ahead of one with a genuinely poor rate (ADR-0007).
  * "Completed" counts jobs the operator closed out; a job still In Progress is
    not a failure, so it is reported, not scored against them.
  * The headline good/rejected/jobs are TRUE plant totals; ``by_operator`` is a
    capped page of the crew, and ``operators`` is the true head-count behind it.
  * Every number is derived from the same in-window rows, so the per-operator and
    per-day breakdowns sum back to the headline (ADR-0007, rule 3).
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "workforce"

WINDOW_DAYS = 7
TOP_N = 10
# Job statuses that mean the operator has closed the job out. Anything else
# (Started / In Progress / Paused) is still live work, counted as active.
DONE_STATUSES = {"completed", "complete", "done", "closed", "finished"}
# A crew member needs at least this many booked units before their quality rate
# is a signal rather than arithmetic off a handful of parts — one reject in three
# units is 33%, which would flag a barely-started operator as the worst on the floor.
MIN_UNITS = 20


def _quality_rate(good: int, total: int):
    """Good units as a % of booked units, or None when nothing was booked — an
    operator with no output has no quality to measure (ADR-0007: never present a
    metric the data can't support). A configured/real 0 good on real units is a
    true 0%, not None."""
    return round(good / total * 100) if total else None


def _is_done(status) -> bool:
    return (status or "").strip().lower() in DONE_STATUSES


def _rank(o):
    """The shared worst-first ordering used by both the summary's per-operator
    list and the drill-down's per-machine list: an entry with real volume
    (units >= MIN_UNITS) and a rate ranks by worst quality first, then most
    rejects; everything below the floor (or with no units) has no signal to rank
    on, so it sinks to the end ordered by busiest first. Kept at module scope so
    the two read-models can never disagree on who is "worst"."""
    if o["units"] >= MIN_UNITS and o["quality_rate"] is not None:
        # rateable: worst quality first, then most rejects.
        return (0, o["quality_rate"], -o["rejected"])
    # the rest: busiest first, so they still surface in a sensible order.
    return (1, 0, -o["jobs"])


def build_operator_summary(db, tenant: str) -> dict:
    """Operator performance over the last 7 days: plant totals (operators active,
    jobs run and completed, good/rejected units, pooled quality rate), a
    per-operator breakdown (worst quality on real volume first), the operator to
    look at first, and a daily good/rejected series. operator_job_executions is
    auto-scoped (ADR-0002); the scan is bounded to the window in SQL (started_at
    is indexed). Empty-safe: zeros, no divide-by-zero, quality None when nothing
    was booked."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    cutoff = datetime.combine(window[0], datetime.min.time())

    # Windowed in SQL — operator_job_executions grows continuously (a row per job,
    # every shift), so filtering the whole table in Python re-scans it on each poll.
    # The set check then keeps exact per-day semantics and drops NULL/future-dated
    # rows, so every total below is over the same in-window set and reconciles.
    rows = [
        r for r in (db.query(models.OperatorJobExecution)
                    .filter(models.OperatorJobExecution.started_at >= cutoff).all())
        if r.started_at and r.started_at.date() in window_set
    ]

    per_op: dict = {}
    day_good: dict = defaultdict(int)
    day_rejected: dict = defaultdict(int)
    for r in rows:
        op = (r.operator_name or "—").strip() or "—"
        a = per_op.setdefault(op, {
            "operator": op, "jobs": 0, "completed": 0, "active": 0,
            "good": 0, "rejected": 0,
        })
        good = r.good_count or 0
        rejected = r.rejected_count or 0
        a["jobs"] += 1
        if _is_done(r.job_status):
            a["completed"] += 1
        else:
            a["active"] += 1
        a["good"] += good
        a["rejected"] += rejected
        day = r.started_at.date()
        day_good[day] += good
        day_rejected[day] += rejected

    by_operator = []
    for a in per_op.values():
        units = a["good"] + a["rejected"]
        by_operator.append({
            **a,
            "units": units,
            "quality_rate": _quality_rate(a["good"], units),
        })

    # Worst quality first, but only among operators with real volume — an operator
    # below the volume floor (or with no units at all) has no signal to rank on and
    # sinks to the end, so a barely-started crew member never shows as "worst".
    by_operator.sort(key=_rank)

    # Plant totals — TRUE totals over the in-window rows, not the length of the
    # capped list. sum(per-operator) == these, and sum(daily) == these (asserted
    # in the tests).
    good = sum(a["good"] for a in per_op.values())
    rejected = sum(a["rejected"] for a in per_op.values())
    units = good + rejected
    completed = sum(a["completed"] for a in per_op.values())
    plant_rate = _quality_rate(good, units)

    # The operator to look at first: the worst-quality one that clears the volume
    # floor AND sits below the plant rate. The worst of a uniformly good crew
    # doesn't "need attention", so None then.
    needs_attention = None
    for o in by_operator:
        if o["units"] >= MIN_UNITS and o["quality_rate"] is not None:
            if plant_rate is not None and o["quality_rate"] < plant_rate:
                needs_attention = o
            break   # by_operator is worst-first, so the first rateable IS the worst

    daily = [{"date": d.isoformat(), "good": day_good.get(d, 0), "rejected": day_rejected.get(d, 0)}
             for d in window]

    return {
        "days": WINDOW_DAYS,
        "operators": len(per_op),          # true head-count behind by_operator
        "jobs": len(rows),
        "completed": completed,
        "active": len(rows) - completed,
        "good_units": good,
        "rejected_units": rejected,
        "total_units": units,
        "quality_rate": plant_rate,        # None when nothing was booked
        "min_units": MIN_UNITS,
        "by_operator": by_operator[:TOP_N],
        "needs_attention": needs_attention,
        "daily": daily,
    }


def _empty_operator(operator: str, window) -> dict:
    """Zeroed shape for an operator with no jobs in the window — the drawer renders
    a 'nothing to show' state rather than the caller having to special-case a 404."""
    return {
        "found": False,
        "operator": operator,
        "days": WINDOW_DAYS,
        "min_units": MIN_UNITS,
        "jobs": 0, "completed": 0, "active": 0,
        "good_units": 0, "rejected_units": 0, "total_units": 0,
        "quality_rate": None,
        "plant_quality_rate": None, "vs_plant": None,
        "rank": None, "operators": 0,
        "avg_job_minutes": None, "timed_jobs": 0,
        "by_machine": [],
        "daily": [{"date": d.isoformat(), "good": 0, "rejected": 0} for d in window],
        "recent": [],
    }


def build_operator_detail(db, tenant: str, operator: str) -> dict:
    """Drill-down for a single operator (by name, as keyed in the summary): their
    good/rejected units and quality rate over the last 7 days, how that reads
    against the plant and where they rank among the crew, which machines they ran
    (worst quality on real volume first), a measured average job time, a daily
    good/rejected series, and their recent jobs.

    The summary ranks the whole crew; this answers the follow-up a shift lead
    acts on — *is it this operator, or the machine they were put on?* It reuses
    the summary's shared quality-rate and ranking definitions (rule 1), so the
    plant rate and this operator's rank reconcile exactly with the summary card
    (rule 3). operator_job_executions is auto-scoped (ADR-0002) and the scan is
    bounded to the window in SQL (started_at is indexed, rule 4). Empty-safe:
    returns found:false for an operator with no jobs in the window.

    Honesty notes (ADR-0007):
      * ``avg_job_minutes`` is a MEASURED duration from the real started_at /
        completed_at timestamps — a job with no completion (or a clock-skew
        negative span) is held out of the average, never scored 0. It is a cycle
        time, NOT an "on-time" claim: the log carries no due timestamp to judge
        punctuality against (rule 2).
      * Quality rate is None when the operator booked no units (never a misleading
        0%). ``vs_plant`` is None whenever either side has no rate.
      * The per-machine and per-day breakdowns each sum back to this operator's
        totals — a job with no machine falls in the '—' bucket rather than being
        dropped (rule 3)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    cutoff = datetime.combine(window[0], datetime.min.time())

    # One window scan over ALL operators (started_at indexed) — the same in-window
    # set the summary aggregates, so the plant baseline and this operator's rank
    # come out identical to the summary card. This operator's own rows are then
    # filtered from it for the per-machine / daily / recent detail (no second scan).
    rows = [
        r for r in (db.query(models.OperatorJobExecution)
                    .filter(models.OperatorJobExecution.started_at >= cutoff).all())
        if r.started_at and r.started_at.date() in window_set
    ]

    # Aggregate per operator exactly as the summary does (same helpers/constants),
    # so plant_quality_rate == the summary's quality_rate and the ranking matches.
    per_op: dict = {}
    for r in rows:
        op = (r.operator_name or "—").strip() or "—"
        a = per_op.setdefault(op, {"operator": op, "jobs": 0, "completed": 0,
                                   "active": 0, "good": 0, "rejected": 0})
        a["jobs"] += 1
        if _is_done(r.job_status):
            a["completed"] += 1
        else:
            a["active"] += 1
        a["good"] += r.good_count or 0
        a["rejected"] += r.rejected_count or 0

    ranked = []
    for a in per_op.values():
        units = a["good"] + a["rejected"]
        ranked.append({**a, "units": units, "quality_rate": _quality_rate(a["good"], units)})
    ranked.sort(key=_rank)

    if operator not in per_op:
        return _empty_operator(operator, window)

    # Plant baseline over the same rows — the identical pooled number the summary
    # headline reports, so "vs plant" compares like with like (rule 3).
    plant_good = sum(a["good"] for a in per_op.values())
    plant_rejected = sum(a["rejected"] for a in per_op.values())
    plant_rate = _quality_rate(plant_good, plant_good + plant_rejected)

    me = next(o for o in ranked if o["operator"] == operator)
    rank = next(i for i, o in enumerate(ranked, start=1) if o["operator"] == operator)

    mine = [r for r in rows if ((r.operator_name or "—").strip() or "—") == operator]

    # Per-machine breakdown + daily series, both over this operator's rows only.
    # A job with machine_id NULL goes to the '—' bucket so the machine parts still
    # sum to this operator's totals (rule 3).
    machine_names = {m.id: m.name for m in db.query(models.Machine).all()}
    per_machine: dict = {}
    day_good: dict = defaultdict(int)
    day_rejected: dict = defaultdict(int)
    for r in mine:
        mid = r.machine_id
        m = per_machine.setdefault(mid, {
            "machine_id": mid,
            "name": machine_names.get(mid, "—") if mid is not None else "—",
            "jobs": 0, "good": 0, "rejected": 0,
        })
        m["jobs"] += 1
        m["good"] += r.good_count or 0
        m["rejected"] += r.rejected_count or 0
        day = r.started_at.date()
        day_good[day] += r.good_count or 0
        day_rejected[day] += r.rejected_count or 0

    by_machine = []
    for m in per_machine.values():
        units = m["good"] + m["rejected"]
        by_machine.append({**m, "units": units, "quality_rate": _quality_rate(m["good"], units)})
    by_machine.sort(key=_rank)   # same worst-quality-on-real-volume-first ordering

    # Measured job cycle time from the real timestamps only — a job still open (no
    # completed_at) or with a negative span can't be timed, so it's held out of the
    # average, not scored 0. `timed_jobs` is the honest denominator behind the mean.
    spans = [(r.completed_at - r.started_at).total_seconds() / 60
             for r in mine
             if r.completed_at and r.started_at and r.completed_at >= r.started_at]
    avg_job_minutes = round(sum(spans) / len(spans), 1) if spans else None

    daily = [{"date": d.isoformat(), "good": day_good.get(d, 0), "rejected": day_rejected.get(d, 0)}
             for d in window]

    recent = sorted(mine, key=lambda r: (r.started_at or datetime.min, r.id), reverse=True)[:TOP_N]
    recent_rows = [{
        "execution_no": r.execution_no,
        "machine": machine_names.get(r.machine_id, "—") if r.machine_id is not None else "—",
        "job_status": r.job_status,
        "done": _is_done(r.job_status),
        "good": r.good_count or 0,
        "rejected": r.rejected_count or 0,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "minutes": (round((r.completed_at - r.started_at).total_seconds() / 60, 1)
                    if r.completed_at and r.started_at and r.completed_at >= r.started_at else None),
    } for r in recent]

    quality_rate = me["quality_rate"]
    return {
        "found": True,
        "operator": operator,
        "days": WINDOW_DAYS,
        "min_units": MIN_UNITS,
        "jobs": me["jobs"],
        "completed": me["completed"],
        "active": me["active"],
        "good_units": me["good"],
        "rejected_units": me["rejected"],
        "total_units": me["units"],
        "quality_rate": quality_rate,
        "plant_quality_rate": plant_rate,
        "vs_plant": (quality_rate - plant_rate)
                    if (quality_rate is not None and plant_rate is not None) else None,
        "rank": rank,
        "operators": len(per_op),
        "avg_job_minutes": avg_job_minutes,
        "timed_jobs": len(spans),
        "by_machine": by_machine,
        "daily": daily,
        "recent": recent_rows,
    }
