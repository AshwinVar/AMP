"""Schedule adherence — are we hitting the production plan? (ADR-0007).

The production read-model looks at *actual* output; this one holds that output
against what was *scheduled*. It answers the SME plant owner's planning
question: "of what we planned to make by now, how much did we actually make —
and which shifts, machines and plans fell behind?" Over the trailing week it
classifies every production plan by its attainment state (met / on-track /
behind / missed) from actual vs planned quantity, computes a pooled attainment
rate over the plans due so far, rolls that up per shift and per machine (worst
first), and lists the specific behind/missed plans to chase. A read-model over
production_plans (+ machines / work_orders for labels) — auto-scoped to the
tenant (ADR-0002); it adds no storage.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "schedule"

WINDOW_DAYS = 7
TOP_N = 10
# A plan counts as met once the status says it's finished, regardless of counts.
DONE_STATUSES = {"completed", "complete", "done", "closed", "finished"}


def _pct(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def _state(plan, today) -> str:
    """A single plan's attainment state. Met when the actual quantity reaches the
    plan (or the status says it's done); otherwise, for a plan already due (its
    date has passed), behind if some was made and missed if none was — while a
    plan still due today counts as on-track (the shift can still catch up)."""
    planned = plan.planned_quantity or 0
    actual = plan.actual_quantity or 0
    status = (plan.status or "").strip().lower()
    if status in DONE_STATUSES or (planned > 0 and actual >= planned):
        return "met"
    date = plan.plan_date
    if date is None or date >= today:
        return "on_track"
    return "behind" if actual > 0 else "missed"


def build_schedule_adherence(db, tenant: str) -> dict:
    """Production-plan adherence over the last 7 days: plant-wide state counts, a
    pooled attainment rate over the plans due so far, a per-shift and per-machine
    breakdown (worst first), a daily planned-vs-actual series, today's scheduled
    load, and the behind/missed plans to chase. production_plans, machines and
    work_orders are auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    # Window in SQL — production_plans grows continuously, so filtering the whole
    # table in Python re-scans it on every 30s poll. The window is contiguous days
    # from window[0] to today, so a >= / <= range is exactly `plan_date in window`.
    plans = (db.query(models.ProductionPlan)
             .filter(models.ProductionPlan.plan_date >= window[0],
                     models.ProductionPlan.plan_date <= today).all())

    machine_names = {m.id: m.name for m in db.query(models.Machine).all()}
    wo_numbers = {w.id: w.work_order_no for w in db.query(models.WorkOrder).all()}

    totals = {"met": 0, "on_track": 0, "behind": 0, "missed": 0}
    # "due so far" = plans whose date has already passed (strictly before today);
    # the honest adherence denominator. Today's plans are still in progress and
    # future plans aren't behind schedule yet, so both are held out of the rate.
    due_planned = due_actual = 0
    per_shift: dict = {}
    per_machine: dict = {}
    per_day_planned: dict = defaultdict(int)
    per_day_actual: dict = defaultdict(int)
    chase = []

    for p in plans:
        state = _state(p, today)
        totals[state] += 1
        planned = p.planned_quantity or 0
        actual = p.actual_quantity or 0

        # Full-window daily series (every day, incl. today) for the mini chart.
        if p.plan_date is not None:
            per_day_planned[p.plan_date] += planned
            per_day_actual[p.plan_date] += actual
        # Headline attainment is over plans already due (before today) only. The
        # per-shift / per-machine rates below MUST use this same basis, or the
        # breakdown contradicts the headline it sits under: a plan due today or
        # later has actual=0 simply because it hasn't run yet, which would drag
        # every row down while the headline stayed high.
        is_due = p.plan_date is not None and p.plan_date < today
        if is_due:
            due_planned += planned
            due_actual += actual

        shift = (p.shift_name or "—")
        s = per_shift.setdefault(shift, {
            "shift": shift, "plans": 0,
            "met": 0, "on_track": 0, "behind": 0, "missed": 0,
            "planned": 0, "actual": 0, "due_planned": 0, "due_actual": 0,
        })
        s["plans"] += 1
        s[state] += 1
        s["planned"] += planned
        s["actual"] += actual
        if is_due:
            s["due_planned"] += planned
            s["due_actual"] += actual

        mname = machine_names.get(p.machine_id, "—")
        m = per_machine.setdefault(p.machine_id, {
            "machine_id": p.machine_id, "machine": mname, "plans": 0,
            "met": 0, "on_track": 0, "behind": 0, "missed": 0,
            "planned": 0, "actual": 0, "due_planned": 0, "due_actual": 0,
        })
        m["plans"] += 1
        m[state] += 1
        m["planned"] += planned
        m["actual"] += actual
        if is_due:
            m["due_planned"] += planned
            m["due_actual"] += actual

        if state in ("behind", "missed"):
            chase.append({
                "plan_no": p.plan_no,
                "machine": mname,
                "work_order_no": wo_numbers.get(p.work_order_id),
                "shift_name": p.shift_name,
                "plan_date": p.plan_date.isoformat() if p.plan_date else None,
                "planned_quantity": planned,
                "actual_quantity": actual,
                "shortfall": max(planned - actual, 0),
                "attainment_rate": _pct(actual, planned),
                "state": state,
                "days_ago": (today - p.plan_date).days if p.plan_date else None,
            })

    # Rates over plans already due — the same basis as the headline above.
    by_shift = [{**s, "attainment_rate": _pct(s["due_actual"], s["due_planned"])} for s in per_shift.values()]
    by_shift.sort(key=lambda s: (s["missed"], s["behind"], -s["attainment_rate"]), reverse=True)

    by_machine = [{**m, "attainment_rate": _pct(m["due_actual"], m["due_planned"])} for m in per_machine.values()]
    by_machine.sort(key=lambda m: (m["missed"], m["behind"], -m["attainment_rate"]), reverse=True)

    daily = [{
        "date": d.isoformat(),
        "planned": per_day_planned.get(d, 0),
        "actual": per_day_actual.get(d, 0),
        "attainment_rate": _pct(per_day_actual.get(d, 0), per_day_planned.get(d, 0)),
    } for d in window]

    # chase list: missed first, then behind; within each, biggest shortfall first
    chase.sort(key=lambda c: (0 if c["state"] == "missed" else 1, -c["shortfall"]))

    today_plans = [p for p in plans if p.plan_date == today]
    today_planned = sum(p.planned_quantity or 0 for p in today_plans)
    today_actual = sum(p.actual_quantity or 0 for p in today_plans)

    return {
        "days": WINDOW_DAYS,
        "total": len(plans),
        "met": totals["met"],
        "on_track": totals["on_track"],
        "behind": totals["behind"],
        "missed": totals["missed"],
        "planned_units": due_planned,
        "actual_units": due_actual,
        "attainment_rate": _pct(due_actual, due_planned),
        "by_shift": by_shift,
        "by_machine": by_machine,
        "chase": chase[:TOP_N],
        "daily": daily,
        "today": {
            "plans": len(today_plans),
            "planned": today_planned,
            "actual": today_actual,
            "attainment_rate": _pct(today_actual, today_planned),
        },
    }


def build_shift_adherence(db, tenant: str, shift: str) -> dict:
    """Drill-down for a single shift (by name, as keyed in the summary): how its
    attainment reads against the plant, where it ranks among the shifts, its own
    state mix, a daily planned-vs-actual series, which machines inside the shift
    lose the plan (worst first), and the plans to chase. Same 7-day window and
    the same `_state` rules as the summary, so the two always agree. Composes
    production_plans + machines / work_orders (auto-scoped, ADR-0002); adds no
    storage. Returns found: false when the shift has no plans in the window."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    # Window in SQL (see build_schedule_adherence) — no full-table scan per poll.
    all_plans = (db.query(models.ProductionPlan)
                 .filter(models.ProductionPlan.plan_date >= window[0],
                         models.ProductionPlan.plan_date <= today).all())
    plans = [p for p in all_plans if (p.shift_name or "—") == shift]

    machine_names = {m.id: m.name for m in db.query(models.Machine).all()}
    wo_numbers = {w.id: w.work_order_no for w in db.query(models.WorkOrder).all()}

    # Plant baseline over the same window, on the same "due so far" denominator
    # the summary headline uses — so "vs plant" compares like with like.
    plant_due = [p for p in all_plans if p.plan_date is not None and p.plan_date < today]
    plant_rate = _pct(sum(p.actual_quantity or 0 for p in plant_due),
                      sum(p.planned_quantity or 0 for p in plant_due))

    # Rank among shifts on the shared worst-first ordering (missed, then behind,
    # then lowest attainment) — 1 = the shift losing the most plan.
    ranked = [s["shift"] for s in build_schedule_adherence(db, tenant)["by_shift"]]
    rank = ranked.index(shift) + 1 if shift in ranked else None

    totals = {"met": 0, "on_track": 0, "behind": 0, "missed": 0}
    due_planned = due_actual = 0
    per_day_planned: dict = defaultdict(int)
    per_day_actual: dict = defaultdict(int)
    per_machine: dict = {}
    chase = []

    for p in plans:
        state = _state(p, today)
        totals[state] += 1
        planned = p.planned_quantity or 0
        actual = p.actual_quantity or 0

        if p.plan_date is not None:
            per_day_planned[p.plan_date] += planned
            per_day_actual[p.plan_date] += actual
        # "Due so far" = plan_date strictly before today. The headline rate and
        # the per-machine rows below both use this basis so the breakdown
        # reconciles with the number it sits under (rule 3); a plan due today or
        # later has actual=0 only because it hasn't run yet.
        is_due = p.plan_date is not None and p.plan_date < today
        if is_due:
            due_planned += planned
            due_actual += actual

        mname = machine_names.get(p.machine_id, "—")
        m = per_machine.setdefault(p.machine_id, {
            "machine_id": p.machine_id, "machine": mname, "plans": 0,
            "met": 0, "on_track": 0, "behind": 0, "missed": 0,
            "planned": 0, "actual": 0, "due_planned": 0, "due_actual": 0,
        })
        m["plans"] += 1
        m[state] += 1
        m["planned"] += planned
        m["actual"] += actual
        if is_due:
            m["due_planned"] += planned
            m["due_actual"] += actual

        if state in ("behind", "missed"):
            chase.append({
                "plan_no": p.plan_no,
                "machine": mname,
                "work_order_no": wo_numbers.get(p.work_order_id),
                "plan_date": p.plan_date.isoformat() if p.plan_date else None,
                "planned_quantity": planned,
                "actual_quantity": actual,
                "shortfall": max(planned - actual, 0),
                "attainment_rate": _pct(actual, planned),
                "state": state,
                "days_ago": (today - p.plan_date).days if p.plan_date else None,
            })

    # Per-machine attainment/shortfall on the same "due so far" basis as the
    # shift headline (and the summary's own by_machine), so the parts sum to the
    # whole: sum(due_planned) == planned_units and sum(shortfall) == shortfall_units.
    by_machine = [{**m, "attainment_rate": _pct(m["due_actual"], m["due_planned"]),
                   "shortfall": max(m["due_planned"] - m["due_actual"], 0)}
                  for m in per_machine.values()]
    by_machine.sort(key=lambda m: (m["missed"], m["behind"], -m["attainment_rate"]), reverse=True)

    daily = [{
        "date": d.isoformat(),
        "planned": per_day_planned.get(d, 0),
        "actual": per_day_actual.get(d, 0),
        "attainment_rate": _pct(per_day_actual.get(d, 0), per_day_planned.get(d, 0)),
    } for d in window]

    chase.sort(key=lambda c: (0 if c["state"] == "missed" else 1, -c["shortfall"]))

    rate = _pct(due_actual, due_planned)
    return {
        "found": bool(plans),
        "shift": shift,
        "days": WINDOW_DAYS,
        "rank": rank,
        "shifts": len(ranked),
        "total": len(plans),
        "met": totals["met"],
        "on_track": totals["on_track"],
        "behind": totals["behind"],
        "missed": totals["missed"],
        "planned_units": due_planned,
        "actual_units": due_actual,
        "attainment_rate": rate,
        "shortfall_units": max(due_planned - due_actual, 0),
        "plant_attainment_rate": plant_rate,
        "vs_plant": rate - plant_rate,
        "by_machine": by_machine,
        "worst_machine": by_machine[0] if by_machine else None,
        "chase": chase[:TOP_N],
        "daily": daily,
    }
