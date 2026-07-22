"""Maintenance execution / PM compliance read-model tests (ADR-0007).

The maintenance summary answers "what is open". This one answers "are we keeping
the maintenance promise": the share of completed work that landed on or before
plan, how late the rest ran, the planned-vs-reactive mix, and how stale the
overdue backlog has gone. Run:
    python backend/test_maintenance_execution.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import maintenance


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _task(no, machine_id, planned, status="Completed", completed=None,
          task_type="Preventive", priority="Medium", downtime=0):
    return models.MaintenanceTask(
        task_no=no, machine_id=machine_id, task_type=task_type, priority=priority,
        assigned_to="Maintenance team", planned_date=planned, completed_date=completed,
        status=status, downtime_minutes=downtime)


def test_compliance_counts_on_time_completions_and_prices_the_slip():
    db = _fresh_session()
    # UTC like the read-model — date.today() is local and would drift the window.
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90))
    db.add_all([
        # machine 1: two on time (one early, one exactly on plan), one 4 days late
        _task("MX-1", 1, today - timedelta(days=6), completed=today - timedelta(days=8), downtime=30),
        _task("MX-2", 1, today - timedelta(days=4), completed=today - timedelta(days=4), downtime=45),
        _task("MX-3", 1, today - timedelta(days=9), completed=today - timedelta(days=5),
              task_type="Corrective", downtime=60),
        # machine 2: one on time
        _task("MX-4", 2, today - timedelta(days=2), completed=today - timedelta(days=2)),
        # completed but outside the 30-day window -> not scored
        _task("MX-5", 2, today - timedelta(days=60), completed=today - timedelta(days=59)),
        # completed with no completion date -> can't be timed, reported separately
        _task("MX-6", 2, today - timedelta(days=3), completed=None),
        # still open -> not a completion at all
        _task("MX-7", 2, today + timedelta(days=2), status="Open"),
    ])
    db.commit()

    e = maintenance.build_maintenance_execution(db, "DEFAULT")
    assert e["days"] == 30
    assert e["completed"] == 4 and e["timed"] == 4          # MX-1..4; MX-5 windowed out, MX-6 undated
    assert e["on_time"] == 3 and e["late"] == 1
    assert e["compliance_rate"] == 75                       # 3/4
    assert e["avg_days_late"] == 4.0 and e["worst_days_late"] == 4
    assert e["undated_completions"] == 1
    # planned vs reactive over the same completed set: only MX-3 is Corrective
    assert e["reactive_count"] == 1 and e["planned_count"] == 3
    assert e["planned_share"] == 75
    # maintenance downtime consumed by the window's completions
    assert e["downtime_minutes"] == 135

    # per-machine, worst compliance first: machine 1 is 2/3, machine 2 is 1/1
    assert [m["name"] for m in e["by_machine"]] == ["SMT-Reflow-01", "IC-Test-01"]
    m1 = e["by_machine"][0]
    assert m1["completed"] == 3 and m1["on_time"] == 2 and m1["compliance_rate"] == 67
    assert e["tone"] == "warn" and "75%" in e["verdict"]


def test_backlog_ages_overdue_work_and_ranks_the_chase_list():
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0))
    db.add_all([
        _task("MB-1", 1, today - timedelta(days=3), status="Open"),                       # 1-7 days
        _task("MB-2", 1, today - timedelta(days=20), status="In Progress",
              task_type="Corrective", priority="Critical"),                                # 8-30 days
        _task("MB-3", 1, today - timedelta(days=45), status="Proposed"),                   # 30+ days
        _task("MB-4", 1, today + timedelta(days=5), status="Open"),                        # not due yet
        _task("MB-5", 1, today - timedelta(days=10), completed=today - timedelta(days=10)),  # done, not backlog
    ])
    db.commit()

    e = maintenance.build_maintenance_execution(db, "DEFAULT")
    b = e["backlog"]
    assert b["open"] == 4 and b["overdue"] == 3          # MB-4 is open but not yet due
    assert b["oldest_days"] == 45
    assert b["aging"] == [{"bucket": "1-7 days", "count": 1},
                          {"bucket": "8-30 days", "count": 1},
                          {"bucket": "30+ days", "count": 1}]
    # chase list: oldest overdue first, and it carries the age + reactive flag
    assert [t["task_no"] for t in e["chase"]] == ["MB-3", "MB-2", "MB-1"]
    assert e["chase"][0]["days_overdue"] == 45 and e["chase"][0]["machine"] == "SMT-Reflow-01"
    assert e["chase"][1]["reactive"] is True and e["chase"][1]["priority"] == "Critical"
    # one on-time completion, but the backlog keeps the verdict off "good"
    assert e["compliance_rate"] == 100 and e["tone"] == "warn"
    assert "overdue" in e["verdict"]

    # the machine row carries its overdue backlog alongside its compliance
    assert e["by_machine"][0]["overdue"] == 3


def test_reactive_classification_and_clean_and_empty_plants():
    # Firefighting types read reactive; scheduled and unknown types read planned.
    assert maintenance.is_reactive("Corrective") is True
    assert maintenance.is_reactive("Breakdown repair") is True
    assert maintenance.is_reactive("Quality (auto)") is True
    assert maintenance.is_reactive("Preventive") is False
    assert maintenance.is_reactive("Predictive (auto)") is False   # predicted, not reacted to
    assert maintenance.is_reactive("Calibration") is False
    assert maintenance.is_reactive(None) is False                  # unknown is never firefighting

    today = datetime.utcnow().date()
    clean = _fresh_session()
    clean.add(models.Machine(id=1, name="SMT-Printer-01", status="Running", utilization=95))
    clean.add_all([
        _task("MC-1", 1, today - timedelta(days=5), completed=today - timedelta(days=5)),
        _task("MC-2", 1, today - timedelta(days=2), completed=today - timedelta(days=3)),
    ])
    clean.commit()
    c = maintenance.build_maintenance_execution(clean, "DEFAULT")
    assert c["compliance_rate"] == 100 and c["tone"] == "good"
    assert c["backlog"]["overdue"] == 0 and c["backlog"]["aging"] == [] and c["chase"] == []
    assert c["planned_share"] == 100 and c["avg_days_late"] == 0.0

    # A plant with nothing recorded: a zeroed shape, never a crash or a divide.
    empty = maintenance.build_maintenance_execution(_fresh_session(), "DEFAULT")
    assert empty["completed"] == 0 and empty["compliance_rate"] is None
    assert empty["planned_share"] is None and empty["tone"] == "warn"
    assert empty["backlog"]["open"] == 0 and empty["backlog"]["oldest_days"] is None
    assert empty["by_machine"] == [] and empty["chase"] == []


def test_broken_discipline_reads_bad():
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="IC-Bond-01", status="Running", utilization=80))
    db.add_all([
        _task("MD-1", 1, today - timedelta(days=20), completed=today - timedelta(days=6),
              task_type="Breakdown"),
        _task("MD-2", 1, today - timedelta(days=18), completed=today - timedelta(days=4),
              task_type="Corrective"),
        _task("MD-3", 1, today - timedelta(days=10), completed=today - timedelta(days=10)),
        _task("MD-4", 1, today - timedelta(days=40), status="Open"),
    ])
    db.commit()

    e = maintenance.build_maintenance_execution(db, "DEFAULT")
    assert e["compliance_rate"] == 33 and e["tone"] == "bad"      # 1 of 3 on plan
    assert "broken down" in e["verdict"] and "40 days" in e["verdict"]
    assert e["reactive_count"] == 2 and e["planned_share"] == 33


def test_a_single_late_job_is_reported_not_condemned():
    # One late completion out of two is a 50% rate, but two data points are not a
    # collapse in discipline — below the minimum sample the tone stays "warn".
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(models.Machine(id=1, name="SMT-Place-01", status="Running", utilization=90))
    db.add_all([
        _task("MT-1", 1, today - timedelta(days=7), completed=today - timedelta(days=7)),
        _task("MT-2", 1, today - timedelta(days=5), completed=today - timedelta(days=3)),
    ])
    db.commit()

    e = maintenance.build_maintenance_execution(db, "DEFAULT")
    assert e["compliance_rate"] == 50 and e["timed"] == 2
    assert e["tone"] == "warn" and "too thin to judge" in e["verdict"]


if __name__ == "__main__":
    test_compliance_counts_on_time_completions_and_prices_the_slip()
    test_backlog_ages_overdue_work_and_ranks_the_chase_list()
    test_reactive_classification_and_clean_and_empty_plants()
    test_broken_discipline_reads_bad()
    test_a_single_late_job_is_reported_not_condemned()
    print("MAINTENANCE EXECUTION OK: PM compliance over 30 days (on-time vs late, avg slip); "
          "planned-vs-reactive mix; overdue backlog aging + oldest; worst-first per-machine "
          "compliance; oldest-first chase list; verdict tone; empty-safe")
