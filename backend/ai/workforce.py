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
    def _rank(o):
        if o["units"] >= MIN_UNITS and o["quality_rate"] is not None:
            # rateable: worst quality first, then most rejects.
            return (0, o["quality_rate"], -o["rejected"])
        # the rest: busiest first, so they still surface in a sensible order.
        return (1, 0, -o["jobs"])
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
