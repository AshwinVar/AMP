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
    window_set = set(window)
    plans = [p for p in db.query(models.ProductionPlan).all() if p.plan_date in window_set]

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
        # Headline attainment is over plans already due (before today) only.
        if p.plan_date is not None and p.plan_date < today:
            due_planned += planned
            due_actual += actual

        shift = (p.shift_name or "—")
        s = per_shift.setdefault(shift, {
            "shift": shift, "plans": 0,
            "met": 0, "on_track": 0, "behind": 0, "missed": 0,
            "planned": 0, "actual": 0,
        })
        s["plans"] += 1
        s[state] += 1
        s["planned"] += planned
        s["actual"] += actual

        mname = machine_names.get(p.machine_id, "—")
        m = per_machine.setdefault(p.machine_id, {
            "machine_id": p.machine_id, "machine": mname, "plans": 0,
            "met": 0, "on_track": 0, "behind": 0, "missed": 0,
            "planned": 0, "actual": 0,
        })
        m["plans"] += 1
        m[state] += 1
        m["planned"] += planned
        m["actual"] += actual

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

    by_shift = [{**s, "attainment_rate": _pct(s["actual"], s["planned"])} for s in per_shift.values()]
    by_shift.sort(key=lambda s: (s["missed"], s["behind"], -s["attainment_rate"]), reverse=True)

    by_machine = [{**m, "attainment_rate": _pct(m["actual"], m["planned"])} for m in per_machine.values()]
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
