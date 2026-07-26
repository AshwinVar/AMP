"""Maintenance forward-schedule read-model tests (ADR-0007).

`build_maintenance_forecast` lays open, dated maintenance out day by day over the
next 14 days, carries the overdue backlog separately (never folded into a future
day), and rolls up the busiest day and per-machine load. These tests pin the
numbers and cover the edges the correctness rules call out: the overdue/upcoming
split, the horizon boundary (in at +13, out at +14), completed/undated exclusion,
a machineless row counting toward the totals but not a machine, the priority
tiebreak in the upcoming list, and the per-day-counts-sum-to-`scheduled`
reconciliation (rule 3).

Run:  python backend/test_maintenance_forecast.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import maintenance

TODAY = datetime.utcnow().date()


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machines(db):
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))


def _task(no, machine_id, day_offset, priority="Medium", status="Open", ttype="Preventive"):
    return models.MaintenanceTask(
        task_no=no, machine_id=machine_id, task_type=ttype, priority=priority,
        assigned_to="tech", planned_date=TODAY + timedelta(days=day_offset), status=status,
    )


def test_forecast_splits_overdue_from_upcoming_and_lays_out_the_days():
    db = _fresh_session()
    _machines(db)
    db.add_all([
        # overdue backlog (planned before today)
        _task("T1", 1, -3, "Critical", "Open"),
        _task("T2", 2, -1, "High", "In Progress"),
        # upcoming inside the 14-day horizon
        _task("T3", 1, 0, "High"),              # due today
        _task("T4", 1, 1, "Medium"),            # +1
        _task("T5", 2, 1, "Critical"),          # +1 (same day as T4)
        _task("T6", 2, 8, "Low", "Proposed"),   # +8, agent-proposed still counts (open)
        # excluded: beyond the horizon, and an already-completed job
        _task("T7", 1, 20, "High"),
        _task("T8", 1, 2, "Medium", "Completed"),
    ])
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")

    assert f["scheduled"] == 4                              # T3,T4,T5,T6
    assert f["overdue"] == 2                                # T1,T2
    assert f["due_today"] == 1                              # T3
    assert f["due_next_7"] == 3                             # T3,T4,T5 (<= today+6); T6 is +8
    assert f["peak"] == {"date": (TODAY + timedelta(days=1)).isoformat(), "count": 2}

    # rule 3: the per-day counts sum to the scheduled total, and the series is the
    # full 14-day horizon starting today
    assert len(f["series"]) == maintenance.FORECAST_WINDOW_DAYS
    assert f["series"][0]["is_today"] is True and f["series"][0]["count"] == 1
    assert sum(d["count"] for d in f["series"]) == f["scheduled"]
    # +1 day has two tasks, one of them urgent (Critical T5; Medium T4 is not)
    assert f["series"][1]["count"] == 2 and f["series"][1]["urgent"] == 1

    # by_machine: both carry 3 (1 overdue + 2 upcoming); tie broken by machine_id
    assert [m["machine_id"] for m in f["by_machine"]] == [1, 2]
    assert f["by_machine"][0] == {"machine_id": 1, "name": "PRESS-01", "count": 3, "overdue": 1}

    # upcoming list: by date, then priority — on +1, Critical (T5) sorts before Medium (T4)
    assert [r["task_no"] for r in f["upcoming"]] == ["T3", "T5", "T4", "T6"]
    assert f["upcoming"][0]["days_until"] == 0 and f["upcoming"][3]["days_until"] == 8

    # overdue list: most overdue first
    assert [r["task_no"] for r in f["overdue_tasks"]] == ["T1", "T2"]
    assert f["overdue_tasks"][0]["days_overdue"] == 3

    assert f["verdict"] == "2 tasks overdue to clear first, then 4 scheduled over the next 14 days."
    assert f["tone"] == "warn"


def test_empty_is_safe_and_reads_as_nothing_scheduled():
    db = _fresh_session()
    _machines(db)
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")
    assert f["scheduled"] == 0 and f["overdue"] == 0
    assert f["peak"] is None
    assert f["by_machine"] == [] and f["upcoming"] == [] and f["overdue_tasks"] == []
    assert len(f["series"]) == maintenance.FORECAST_WINDOW_DAYS
    assert sum(d["count"] for d in f["series"]) == 0
    assert f["verdict"] == "No open maintenance scheduled in the next 14 days."
    assert f["tone"] == "good"


def test_upcoming_only_reads_as_good_with_due_today():
    db = _fresh_session()
    _machines(db)
    db.add_all([_task("U1", 1, 0, "Medium"), _task("U2", 1, 0, "Low")])
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")
    assert f["scheduled"] == 2 and f["overdue"] == 0 and f["due_today"] == 2
    assert f["peak"] == {"date": TODAY.isoformat(), "count": 2}
    assert f["verdict"] == "2 maintenance tasks scheduled over the next 14 days, 2 due today."
    assert f["tone"] == "good"


def test_overdue_with_nothing_scheduled_says_so():
    db = _fresh_session()
    _machines(db)
    db.add_all([_task("O1", 1, -2, "High"), _task("O2", 2, -5, "Critical")])
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")
    assert f["scheduled"] == 0 and f["overdue"] == 2
    assert f["verdict"] == "2 tasks overdue to clear first; nothing else scheduled in the next 14 days."
    assert f["tone"] == "warn"


def test_five_or_more_overdue_reads_as_bad():
    db = _fresh_session()
    _machines(db)
    db.add_all([_task(f"B{i}", 1, -(i + 1), "High") for i in range(5)])
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")
    assert f["overdue"] == 5
    assert f["tone"] == "bad"
    assert f["verdict"].startswith("5 tasks overdue to clear first")


def test_horizon_boundary_and_machineless_row():
    db = _fresh_session()
    _machines(db)
    db.add_all([
        _task("H1", 1, 13, "Medium"),          # exactly at the horizon -> included
        _task("H2", None, 2, "Medium"),        # machineless -> in totals, not a machine
        _task("H3", 1, 14, "Medium"),          # one past the horizon -> excluded
    ])
    db.commit()

    f = maintenance.build_maintenance_forecast(db, "DEFAULT")
    assert f["scheduled"] == 2                              # H1 + H2, H3 excluded
    assert sum(d["count"] for d in f["series"]) == 2
    assert f["due_next_7"] == 1                             # only H2 (+2); H1 is +13
    # the machineless task is in the totals but not attributed to any machine
    assert len(f["by_machine"]) == 1 and f["by_machine"][0]["machine_id"] == 1
    assert f["by_machine"][0]["count"] == 1


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
