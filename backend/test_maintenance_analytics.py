"""Maintenance analytics endpoint tests (/analytics/maintenance).

The endpoint reports the maintenance backlog (open / in-progress / completed /
overdue, task-type split), a total-downtime figure, mean-time-to-repair, and a
per-machine task count. Two things it must get right, pinned here because the
route test only covers registration:

  * MTTR is a per-COMPLETED-repair average. Its numerator is the completed
    tasks' downtime, NOT the all-task downtime total divided by the completed
    count — otherwise an open task that already carries accumulated downtime
    inflates the "average repair time" of jobs that aren't finished.
  * downtime_minutes is a nullable column; a NULL must read as 0, not crash the
    summation (a None -> TypeError 500).

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_maintenance_analytics.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from analytics_routes import get_maintenance_analytics

USER = {"tenant": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _task(no, machine_id, task_type, status, downtime, planned_date):
    return models.MaintenanceTask(
        task_no=no, machine_id=machine_id, task_type=task_type, status=status,
        assigned_to="tech", downtime_minutes=downtime, planned_date=planned_date,
    )


def test_mttr_uses_completed_downtime_over_completed_count():
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Breakdown", utilization=0, line="IC"))
    db.add_all([
        # two COMPLETED repairs on PRESS-01: 60 + 40 minutes of repair time
        _task("MT-1", 1, "Preventive", "Completed", 60, today - timedelta(days=2)),
        _task("MT-2", 1, "Breakdown", "Completed", 40, today - timedelta(days=1)),
        # an OPEN task on CNC-02 already carrying 100 accumulated downtime minutes,
        # planned in the past -> overdue, and NOT a finished repair
        _task("MT-3", 2, "Breakdown", "Open", 100, today - timedelta(days=3)),
        # an IN-PROGRESS task; its downtime column is forced to a genuine NULL
        # below (as a raw/bulk insert or a migration-added column would leave it)
        _task("MT-4", 2, "Preventive", "In Progress", 0, today + timedelta(days=1)),
    ])
    db.commit()
    # The ORM's column default=0 fills an explicit None, so drop to raw SQL to get
    # a real NULL in the column — the case the `or 0` guard exists for.
    db.execute(text("UPDATE maintenance_tasks SET downtime_minutes = NULL WHERE task_no = 'MT-4'"))
    db.commit()
    db.expire_all()

    out = get_maintenance_analytics(db=db, current_user=USER)

    # backlog counts
    assert out["total_tasks"] == 4
    assert out["open"] == 1 and out["in_progress"] == 1 and out["completed"] == 2
    assert out["overdue"] == 1                       # only MT-3 (past date, not completed)
    assert out["preventive"] == 2 and out["breakdown"] == 2

    # total downtime is the honest all-task sum: 60 + 40 + 100 + 0 (NULL) = 200
    assert out["total_downtime_minutes"] == 200

    # MTTR: completed repair time / completed count = (60 + 40) / 2 = 50.
    # The old code divided the ALL-task total by the completed count:
    # 200 / 2 = 100 — double the true average, inflated by the open task's 100.
    assert out["avg_repair_minutes"] == 50, out["avg_repair_minutes"]

    # per-machine counts resolve real names (not an N+1 fallback string)
    assert out["machine_counts"] == {"PRESS-01": 2, "CNC-02": 2}
    print("PASS MTTR = completed downtime / completed count (50, not 100); NULL downtime -> 0")


def test_unknown_machine_falls_back_to_id_label():
    db = _fresh_session()
    today = datetime.utcnow().date()
    # a task pointing at a machine row that doesn't exist -> "Machine {id}" label
    db.add(_task("MT-9", 77, "Breakdown", "Completed", 30, today))
    db.commit()

    out = get_maintenance_analytics(db=db, current_user=USER)
    assert out["machine_counts"] == {"Machine 77": 1}
    assert out["avg_repair_minutes"] == 30            # single completed 30-min repair
    print("PASS unknown machine_id -> 'Machine {id}' label")


def test_empty_factory_is_all_zeros_no_crash():
    out = get_maintenance_analytics(db=_fresh_session(), current_user=USER)
    assert out["total_tasks"] == 0
    assert out["completed"] == 0
    assert out["total_downtime_minutes"] == 0
    assert out["avg_repair_minutes"] == 0            # zero denominator -> 0, no ZeroDivision
    assert out["machine_counts"] == {}
    print("PASS empty factory -> zeros, no divide-by-zero")


def test_distinct_unknown_machines_are_not_collapsed_by_group_by():
    """The GROUP-BY-in-SQL machine breakdown must key on machine_id, then label —
    two DIFFERENT unknown machine_ids stay two rows ('Machine 77' + 'Machine 88'),
    never merged into one bucket. Regression guard for the .all()->GROUP BY move."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add_all([
        _task("MT-A", 77, "Breakdown", "Completed", 10, today),
        _task("MT-B", 88, "Preventive", "Open", 20, today),
        _task("MT-C", 88, "Preventive", "Open", 30, today),
    ])
    db.commit()

    out = get_maintenance_analytics(db=db, current_user=USER)
    assert out["machine_counts"] == {"Machine 77": 1, "Machine 88": 2}, out["machine_counts"]
    # per-machine counts sum to the total (reconciliation across the breakdown)
    assert sum(out["machine_counts"].values()) == out["total_tasks"] == 3
    print("PASS distinct unknown machine_ids stay separate buckets, sum to total")


def test_total_reconciles_and_overdue_excludes_completed_and_future():
    """total_tasks counts EVERY row (incl. a status outside the open/in-progress/
    completed buckets), and 'overdue' is exactly the past-dated, not-completed
    tasks — a completed-but-past task and a future-dated task are NOT overdue."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="LATHE-1", status="Running", utilization=70, line="A"))
    db.add_all([
        _task("R-1", 1, "Preventive", "Open", 0, today - timedelta(days=5)),        # overdue
        _task("R-2", 1, "Breakdown", "In Progress", 0, today - timedelta(days=1)),  # overdue
        _task("R-3", 1, "Preventive", "Completed", 15, today - timedelta(days=2)),  # past but done -> not overdue
        _task("R-4", 1, "Preventive", "Open", 0, today + timedelta(days=3)),        # future -> not overdue
        _task("R-5", 1, "Breakdown", "Cancelled", 0, today - timedelta(days=9)),    # unbucketed status, past+not-completed -> overdue
    ])
    db.commit()

    out = get_maintenance_analytics(db=db, current_user=USER)
    assert out["total_tasks"] == 5
    # open + in_progress + completed do NOT have to equal total (Cancelled is neither)
    assert out["open"] == 2 and out["in_progress"] == 1 and out["completed"] == 1
    # overdue = R-1, R-2, R-5 (past-dated AND not completed) = 3
    assert out["overdue"] == 3, out["overdue"]
    # only the single completed 15-min repair drives MTTR
    assert out["avg_repair_minutes"] == 15
    assert out["total_downtime_minutes"] == 15
    print("PASS total counts all statuses; overdue = past & not-completed only")


def test_overdue_counts_a_null_status_past_dated_task():
    """A past-dated task whose status column is a genuine NULL is unfinished and
    must count as overdue. status is String default="Open" (not NOT NULL), so a
    raw-SQL / migration / cleared-field row can hold a real NULL. The bare SQL
    `status != 'Completed'` predicate is NULL — not TRUE — for a NULL status and
    silently dropped it (three-valued logic), undercounting the backlog even though
    a Cancelled task in the same shape IS counted. Regression guard for the
    or_(status IS NULL, ...) fix; the escalation generator uses the same set."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="GRIND-1", status="Running", utilization=55, line="A"))
    db.add_all([
        _task("N-1", 1, "Preventive", "Open", 0, today - timedelta(days=4)),        # overdue
        _task("N-2", 1, "Breakdown", "Completed", 20, today - timedelta(days=2)),   # past but done -> not overdue
        _task("N-3", 1, "Preventive", "Open", 0, today - timedelta(days=1)),        # NULL'd below -> overdue
        _task("N-4", 1, "Breakdown", "Open", 0, today + timedelta(days=2)),         # NULL'd below but FUTURE -> not overdue
    ])
    db.commit()
    # Force genuine NULLs (the ORM's column default=0/"Open" would refill an
    # explicit None) — the exact raw/migration state the guard exists for.
    db.execute(text("UPDATE maintenance_tasks SET status = NULL WHERE task_no IN ('N-3', 'N-4')"))
    db.commit()
    db.expire_all()

    out = get_maintenance_analytics(db=db, current_user=USER)

    # overdue = N-1 (Open, past) + N-3 (NULL, past) = 2. N-2 is completed; N-4 is
    # NULL but future. The old bare `status != 'Completed'` dropped N-3 -> 1.
    assert out["overdue"] == 2, out["overdue"]
    # N-3/N-4 were NULL'd, so only N-1 stays "Open"; the NULL statuses fall outside
    # every named bucket (a NULL lands in its own GROUP BY key we never read).
    assert out["open"] == 1 and out["completed"] == 1
    assert out["total_tasks"] == 4
    print("PASS overdue counts a NULL-status past-dated task (2, not 1); future NULL excluded")


if __name__ == "__main__":
    test_mttr_uses_completed_downtime_over_completed_count()
    test_unknown_machine_falls_back_to_id_label()
    test_empty_factory_is_all_zeros_no_crash()
    test_distinct_unknown_machines_are_not_collapsed_by_group_by()
    test_total_reconciles_and_overdue_excludes_completed_and_future()
    test_overdue_counts_a_null_status_past_dated_task()
    print("ALL MAINTENANCE ANALYTICS TESTS PASSED")
