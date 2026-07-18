"""Maintenance work summary read-model tests (ADR-0007).

Open maintenance load: counts by priority, overdue + pending-approval totals, and
the tasks to do next (overdue first, then priority). Run:
    python backend/test_maintenance.py     (exit 0 = pass)
"""
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import maintenance


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _task(no, machine_id, priority, status, planned):
    return models.MaintenanceTask(task_no=no, machine_id=machine_id, task_type="Predictive (auto)",
                                  priority=priority, assigned_to="Maintenance team",
                                  planned_date=planned, status=status)


def test_maintenance_summary_rolls_up_open_load():
    db = _fresh_session()
    today = date.today()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0))
    db.add_all([
        _task("M-1", 1, "Critical", "Proposed", today - timedelta(days=1)),   # overdue + pending approval
        _task("M-2", 1, "High", "Open", today + timedelta(days=1)),
        _task("M-3", 1, "Medium", "In Progress", today + timedelta(days=3)),
        _task("M-4", 1, "Low", "Completed", today - timedelta(days=5)),       # done -> excluded
    ])
    db.commit()

    s = maintenance.build_maintenance_summary(db, "DEFAULT")
    assert s["open"] == 3                                    # Completed excluded
    assert s["pending_approval"] == 1                        # only the Proposed one
    assert s["overdue"] == 1                                 # M-1 planned yesterday
    # by priority (only priorities present, worst first)
    assert s["by_priority"] == [{"priority": "Critical", "count": 1},
                                {"priority": "High", "count": 1},
                                {"priority": "Medium", "count": 1}]
    # to-do order: overdue Critical first, then High, then Medium
    assert [t["task_no"] for t in s["tasks"]] == ["M-1", "M-2", "M-3"]
    assert s["tasks"][0]["overdue"] is True and s["tasks"][0]["proposed"] is True
    assert s["tasks"][0]["machine"] == "SMT-Reflow-01"

    # empty -> zeros, no crash
    empty = maintenance.build_maintenance_summary(_fresh_session(), "DEFAULT")
    assert empty["open"] == 0 and empty["tasks"] == [] and empty["by_priority"] == []


if __name__ == "__main__":
    test_maintenance_summary_rolls_up_open_load()
    print("MAINTENANCE OK: open tasks by priority; overdue + pending-approval counts; "
          "to-do order (overdue then priority); empty-safe")
