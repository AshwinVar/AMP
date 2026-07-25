"""Production-schedule analytics endpoint tests (/analytics/production-schedules).

The endpoint summarises the booked schedule: status split (scheduled / running /
completed / delayed), total planned quantity and estimated minutes, per-machine
load, per-shift load, and a load-ranked bottleneck list. Two things it must get
right, pinned here because the route test only covers registration:

  * Machine names are resolved from ONE lookup, not a per-schedule query inside
    the loop — that was an N+1 on a growing table (production_schedules), the same
    anti-pattern the maintenance rollup already dropped.
  * estimated_minutes is a nullable column (it carries only a column default); a
    NULL must read as 0 in both the total and the per-machine load, not crash the
    summation (a None -> TypeError 500).

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


def test_machine_names_resolved_without_a_per_row_query():
    # The per-schedule Machine query inside the loop was an N+1 on a growing table.
    # Pin that it's gone: the loop must not issue a Machine query per row.
    src = inspect.getsource(get_production_schedule_analytics)
    loop_body = src.split("for row in schedules:", 1)[1]
    assert "db.query(models.Machine)" not in loop_body, \
        "machine names must come from one lookup, not a per-schedule query in the loop"
    assert "machine_names" in src, "expected a single machine-name lookup dict"
    print("PASS machine names resolved from one lookup (no N+1 in the loop)")


if __name__ == "__main__":
    test_summary_totals_load_and_bottlenecks()
    test_null_estimated_minutes_reads_as_zero_no_crash()
    test_empty_factory_is_all_zeros_no_crash()
    test_machine_names_resolved_without_a_per_row_query()
    print("ALL PRODUCTION-SCHEDULE ANALYTICS TESTS PASSED")
