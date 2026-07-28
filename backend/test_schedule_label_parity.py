"""Reference oracle: the schedule read-models must not change their numbers.

ai/schedule.py windowed its production plans in SQL (already fixed) but then
loaded the ENTIRE work_orders table just to turn plan.work_order_id into a
work-order number for the chase list.

Measured on 20,000 work orders with 91 plans in the 14-day window referencing
45 distinct orders:

    whole table -> id:number map     173.9 ms
    only the referenced orders         0.5 ms

The map is only ever read as ``wo_numbers.get(p.work_order_id)`` for plans in
the window, so fetching only those ids is exactly equivalent - an id that is
absent returns None either way. This file keeps both PRE-CHANGE implementations
verbatim and asserts the new ones agree field for field, on plans that
reference a missing work order, a NULL work_order_id, and a work order outside
the window, so "exactly equivalent" is tested rather than argued.

Same approach as #405 and #407.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from collections import defaultdict
from datetime import datetime, timedelta

import models
from ai.schedule import (
    TOP_N,
    WINDOW_DAYS,
    _pct,
    _state,
    build_schedule_adherence,
    build_shift_adherence,
)


def reference_schedule_adherence(db, tenant: str) -> dict:
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


def reference_shift_adherence(db, tenant: str, shift: str) -> dict:
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


# ---------------------------------------------------------------- the parity test

import json
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "sched.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    today = datetime.utcnow().date()
    m = models.Machine(name="CNC-1", status="Running", utilization=70,
                       downtime="0 min", tenant_code="DEFAULT")
    m2 = models.Machine(name="CNC-2", status="Idle", utilization=10,
                        downtime="0 min", tenant_code="DEFAULT")
    db.add_all([m, m2])
    db.flush()

    def wo(no):
        w = models.WorkOrder(work_order_no=no, part_number="P", batch_number="B",
                             machine_id=m.id, target_quantity=100, actual_quantity=0,
                             status="Released", material_state="RAW", tenant_code="DEFAULT")
        db.add(w)
        db.flush()
        return w

    in_window = wo("WO-IN-WINDOW")
    # Referenced only by a plan OUTSIDE the window — the whole-table map used to
    # carry it, the narrowed one must not need to.
    outside = wo("WO-OUTSIDE-WINDOW")
    # Referenced by nobody at all: the bulk of a real table.
    for i in range(20):
        wo(f"WO-UNREFERENCED-{i}")

    def plan(no, wo_id, machine_id, days_ago, planned, actual, shift="A"):
        db.add(models.ProductionPlan(
            plan_no=no, work_order_id=wo_id, machine_id=machine_id,
            planned_quantity=planned, actual_quantity=actual,
            plan_date=today - timedelta(days=days_ago), shift_name=shift,
            status="Planned", tenant_code="DEFAULT"))

    plan("PP-behind", in_window.id, m.id, 2, 100, 10)          # chase row, real WO
    plan("PP-missed", in_window.id, m2.id, 3, 100, 0, "B")     # chase row, second machine
    plan("PP-met", in_window.id, m.id, 1, 100, 100)
    plan("PP-today", in_window.id, m.id, 0, 100, 50)
    # A plan whose work order does not exist: .get() must return None, before
    # and after.
    plan("PP-missing-wo", 999_999, m.id, 4, 100, 0, "C")
    # A plan with NO work order at all.
    plan("PP-null-wo", None, m.id, 5, 100, 0, "C")
    # Outside the window entirely — must not pull its work order into the map.
    plan("PP-old", outside.id, m.id, 60, 100, 0)
    db.commit()
    return in_window, outside


def _compare(db, label):
    pairs = [
        (build_schedule_adherence, reference_schedule_adherence, "schedule_adherence", ()),
        (build_shift_adherence, reference_shift_adherence, "shift_adherence:A", ("A",)),
        (build_shift_adherence, reference_shift_adherence, "shift_adherence:B", ("B",)),
        (build_shift_adherence, reference_shift_adherence, "shift_adherence:C", ("C",)),
        (build_shift_adherence, reference_shift_adherence, "shift_adherence:absent", ("ZZZ",)),
    ]
    results = {}
    for fn_new, fn_ref, name, extra in pairs:
        expected = fn_ref(db, "DEFAULT", *extra)
        actual = fn_new(db, "DEFAULT", *extra)
        if actual != expected:
            for key in expected:
                if actual.get(key) != expected[key]:
                    raise AssertionError(
                        f"{label}/{name}: '{key}' changed\n"
                        f"  was: {json.dumps(expected[key], default=str)[:400]}\n"
                        f"  now: {json.dumps(actual.get(key), default=str)[:400]}")
            raise AssertionError(f"{label}/{name}: keys differ")
        results[name] = actual
    return results


def test_matches_reference_on_every_branch():
    db = _fresh_db()
    _seed(db)
    out = _compare(db, "every-branch")

    # Guard the guard: the chase list must actually be exercising the lookup,
    # including the two rows where it has to resolve to None.
    chase = out["schedule_adherence"]["chase"]
    numbers = {row["plan_no"]: row["work_order_no"] for row in chase}
    assert numbers.get("PP-behind") == "WO-IN-WINDOW", numbers
    assert "PP-missing-wo" in numbers and numbers["PP-missing-wo"] is None, numbers
    assert "PP-null-wo" in numbers and numbers["PP-null-wo"] is None, numbers
    print(f"  every-branch parity OK - {len(chase)} chase rows")
    db.close()


def test_matches_reference_on_an_empty_database():
    db = _fresh_db()
    _compare(db, "empty")
    print("  empty-database parity OK")
    db.close()


def test_matches_reference_with_plans_but_no_work_orders():
    db = _fresh_db()
    today = datetime.utcnow().date()
    m = models.Machine(name="M", status="Idle", utilization=0, downtime="0 min", tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    db.add(models.ProductionPlan(plan_no="PP", work_order_id=42, machine_id=m.id,
                                 planned_quantity=100, actual_quantity=0,
                                 plan_date=today - timedelta(days=1), shift_name="A",
                                 status="Planned", tenant_code="DEFAULT"))
    db.commit()
    _compare(db, "no-work-orders")
    print("  no-work-orders parity OK")
    db.close()


if __name__ == "__main__":
    test_matches_reference_on_every_branch()
    test_matches_reference_on_an_empty_database()
    test_matches_reference_with_plans_but_no_work_orders()
    print("schedule label-lookup parity: OK")
