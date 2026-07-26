"""Production-schedule analytics endpoint tests (/analytics/production-schedules).

The endpoint summarises the booked schedule: status split (scheduled / running /
completed / delayed), total planned quantity and estimated minutes, per-machine
load, per-shift load, and a load-ranked bottleneck list. Things it must get
right, pinned here because the route test only covers registration:

  * The growing production_schedules table is aggregated in SQL (GROUP BY /
    COALESCE(SUM)), not hydrated whole into Python to bucket with comprehensions —
    the rule-4 anti-pattern the sibling rollups already dropped. Machine names are
    resolved from ONE lookup, never a per-schedule query.
  * estimated_minutes is a nullable column (it carries only a column default); a
    NULL must read as 0 in both the total and the per-machine load, not crash the
    summation (a None -> TypeError 500).
  * The headline totals and their per-machine / per-shift breakdowns share one
    basis, so the parts sum to the whole; a NULL-status row is still counted in
    total_schedules even though it lands in none of the named status buckets.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_production_schedule_analytics.py
"""
import inspect
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import analytics_routes
from database import Base
from analytics_routes import get_production_schedule_analytics

USER = {"tenant": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _sched(no, machine_id, shift, qty, minutes, status):
    return models.ProductionSchedule(
        schedule_no=no, machine_id=machine_id, shift_name=shift,
        scheduled_date=datetime.utcnow().date(), planned_quantity=qty,
        estimated_minutes=minutes, status=status,
    )


def test_summary_totals_load_and_bottlenecks():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    db.add_all([
        _sched("PS-1", 1, "A", 100, 480, "Scheduled"),
        _sched("PS-2", 1, "B", 50, 120, "Running"),
        _sched("PS-3", 2, "A", 200, 300, "Delayed"),
        # points at a machine row that doesn't exist -> "Machine {id}" label
        _sched("PS-4", 99, "A", 10, 60, "Completed"),
    ])
    db.commit()

    out = get_production_schedule_analytics(db=db, current_user=USER)

    # status split
    assert out["total_schedules"] == 4
    assert out["scheduled"] == 1 and out["running"] == 1
    assert out["completed"] == 1 and out["delayed"] == 1

    # independently-derived totals
    assert out["total_quantity"] == 100 + 50 + 200 + 10 == 360
    assert out["total_minutes"] == 480 + 120 + 300 + 60 == 960

    # per-machine load reconciles to the same estimated-minute basis as the total
    assert out["machine_load"] == {"CNC-01": 600, "CNC-02": 300, "Machine 99": 60}
    assert sum(out["machine_load"].values()) == out["total_minutes"]

    # per-shift load reconciles to the same planned-quantity basis as the total
    assert out["shift_load"] == {"A": 100 + 200 + 10, "B": 50}
    assert sum(out["shift_load"].values()) == out["total_quantity"]

    # bottlenecks are the machine_load entries, heaviest first
    assert [b["machine"] for b in out["bottlenecks"]] == ["CNC-01", "CNC-02", "Machine 99"]
    assert [b["load_minutes"] for b in out["bottlenecks"]] == [600, 300, 60]
    print("PASS production-schedule summary: totals, reconciled load, ranked bottlenecks")


def test_null_estimated_minutes_reads_as_zero_no_crash():
    # estimated_minutes is nullable; a genuine NULL must count as 0, not 500.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add_all([
        _sched("PS-1", 1, "A", 100, 480, "Scheduled"),
        _sched("PS-2", 1, "A", 50, 0, "Running"),   # forced to NULL below
    ])
    db.commit()
    # The ORM column default fills an explicit None, so drop to raw SQL for a real
    # NULL — the case the `or 0` guard exists for.
    db.execute(text("UPDATE production_schedules SET estimated_minutes = NULL WHERE schedule_no = 'PS-2'"))
    db.commit()
    db.expire_all()

    out = get_production_schedule_analytics(db=db, current_user=USER)
    assert out["total_minutes"] == 480          # 480 + NULL(0)
    assert out["machine_load"] == {"CNC-01": 480}
    assert out["total_quantity"] == 150         # quantity still summed
    print("PASS NULL estimated_minutes reads as 0 in total and per-machine load")


def test_empty_factory_is_all_zeros_no_crash():
    out = get_production_schedule_analytics(db=_fresh_session(), current_user=USER)
    assert out["total_schedules"] == 0
    assert out["total_quantity"] == 0 and out["total_minutes"] == 0
    assert out["machine_load"] == {} and out["shift_load"] == {}
    assert out["bottlenecks"] == []
    print("PASS empty factory -> zeros, no crash")


def test_null_status_counted_in_total_but_not_a_named_bucket():
    # status is String default="Scheduled" (not NOT NULL), so a raw-SQL / migration
    # write can store NULL. SQL GROUP BY yields a None key we never read, so a
    # NULL-status row lands in no named bucket — but func.count(id) still includes
    # it, exactly like the old `len([... == "Scheduled"])` scan that also skipped it.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add_all([
        _sched("PS-1", 1, "A", 100, 480, "Scheduled"),
        _sched("PS-2", 1, "A", 40, 120, "Running"),
    ])
    db.commit()
    db.execute(text("UPDATE production_schedules SET status = NULL WHERE schedule_no = 'PS-2'"))
    db.commit()
    db.expire_all()

    out = get_production_schedule_analytics(db=db, current_user=USER)
    assert out["total_schedules"] == 2          # NULL-status row still counted
    assert out["scheduled"] == 1
    assert out["running"] == 0                   # the row is NULL, not "Running"
    # totals and their breakdowns still cover every row, NULL status included
    assert out["total_quantity"] == 140
    assert out["total_minutes"] == 600
    assert sum(out["shift_load"].values()) == out["total_quantity"]
    assert sum(out["machine_load"].values()) == out["total_minutes"]
    print("PASS NULL status counted in total_schedules but in no named status bucket")


def test_breakdowns_reconcile_to_headline_totals():
    # Independently-derived expected numbers over a larger book, asserting the
    # per-machine and per-shift parts sum to the headline whole (shared basis).
    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    rows = []
    expected_qty = 0
    expected_min = 0
    for i in range(1, 13):
        machine_id = 1 if i % 2 else 2
        shift = "A" if i % 3 else "B"
        qty = 10 * i          # 10..120
        minutes = 15 * i      # 15..180
        expected_qty += qty
        expected_min += minutes
        rows.append(_sched(f"PS-{i}", machine_id, shift, qty, minutes, "Scheduled"))
    db.add_all(rows)
    db.commit()

    out = get_production_schedule_analytics(db=db, current_user=USER)
    assert out["total_schedules"] == 12
    assert out["total_quantity"] == expected_qty == 780
    assert out["total_minutes"] == expected_min == 1170
    # the parts reconcile to the whole on both bases
    assert sum(out["shift_load"].values()) == out["total_quantity"]
    assert sum(out["machine_load"].values()) == out["total_minutes"]
    # bottlenecks list every loaded machine, heaviest first, and reconcile too
    assert sum(b["load_minutes"] for b in out["bottlenecks"]) == out["total_minutes"]
    loads = [b["load_minutes"] for b in out["bottlenecks"]]
    assert loads == sorted(loads, reverse=True)
    print("PASS per-machine / per-shift breakdowns reconcile to the headline totals")


def test_scan_is_bounded_in_sql_not_hydrated_into_python():
    # The old code pulled the whole (growing) production_schedules table into Python
    # (`db.query(models.ProductionSchedule).all()`) and bucketed with list
    # comprehensions — the rule-4 anti-pattern. Pin that it's now aggregated in SQL.
    src = inspect.getsource(get_production_schedule_analytics)
    assert "db.query(models.ProductionSchedule).all()" not in src, \
        "must not hydrate the whole production_schedules table into Python"
    assert "for row in schedules" not in src, \
        "must not loop over a fully-hydrated schedule list"
    assert ".group_by(models.ProductionSchedule.status)" in src, \
        "status split must be a SQL GROUP BY"
    assert "func.coalesce(func.sum(models.ProductionSchedule.planned_quantity)" in src \
        and "func.coalesce(func.sum(models.ProductionSchedule.estimated_minutes)" in src, \
        "totals must be NULL-safe SQL SUMs"
    print("PASS schedule analytics aggregates in SQL (bounded), not a full-table scan")


if __name__ == "__main__":
    test_summary_totals_load_and_bottlenecks()
    test_null_estimated_minutes_reads_as_zero_no_crash()
    test_empty_factory_is_all_zeros_no_crash()
    test_null_status_counted_in_total_but_not_a_named_bucket()
    test_breakdowns_reconcile_to_headline_totals()
    test_scan_is_bounded_in_sql_not_hydrated_into_python()
    print("ALL PRODUCTION-SCHEDULE ANALYTICS TESTS PASSED")
