"""Maintenance overdue-escalation generator tests (POST /maintenance/generate-overdue-escalations).

The generator raises one Escalation per OVERDUE maintenance task — planned in the
past and not yet finished — and is idempotent (an unresolved escalation with the
same title is not duplicated). This is the SAME "overdue" set the
/analytics/maintenance count reports, so the two must agree.

Pinned here (the route test only covers registration):
  * an overdue task raises exactly one escalation, and a re-run is idempotent;
  * a genuine NULL status is unfinished -> still overdue -> raises an escalation.
    status is String default="Open" (not NOT NULL), so a raw-SQL / migration /
    cleared-field row can hold a real NULL; SQL's `status != 'Completed'` is NULL —
    not TRUE — for that row, so the bare predicate silently DROPPED it, leaving the
    generator raising fewer escalations than the analytics count claimed overdue.
    Regression guard for the or_(status IS NULL, ...) fix that reconciles the two.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_maintenance_overdue_escalations.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from factory_ops_routes import generate_maintenance_overdue_escalations

USER = {"tenant": "DEFAULT", "sub": "tester"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _task(no, machine_id, status, planned_date):
    return models.MaintenanceTask(
        task_no=no, machine_id=machine_id, task_type="Preventive", status=status,
        priority="High", assigned_to="tech", downtime_minutes=0, planned_date=planned_date,
    )


def _open_titles(db):
    return {e.title for e in db.query(models.Escalation).all()}


def test_overdue_task_raises_one_escalation_idempotently():
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(_task("MT-1", 1, "Open", today - timedelta(days=2)))          # overdue
    db.add(_task("MT-2", 1, "Completed", today - timedelta(days=3)))     # past but done -> no
    db.add(_task("MT-3", 1, "In Progress", today + timedelta(days=1)))   # future -> no
    db.commit()

    first = generate_maintenance_overdue_escalations(db=db, current_user=USER)
    assert first == {"created": 1}, first
    assert _open_titles(db) == {"Maintenance overdue: MT-1"}, _open_titles(db)

    # Idempotent: an unresolved escalation with the same title is not re-created.
    second = generate_maintenance_overdue_escalations(db=db, current_user=USER)
    assert second == {"created": 0}, second
    assert db.query(models.Escalation).count() == 1
    print("PASS overdue task raises one escalation; re-run is idempotent")


def test_null_status_past_task_raises_an_escalation():
    """A NULL-status past-dated task is unfinished -> overdue -> gets an escalation,
    reconciling the generator with the analytics 'overdue' count (both OR the NULL
    in). The bare `status != 'Completed'` filter dropped it (SQL three-valued logic)."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(_task("MT-1", 1, "Open", today - timedelta(days=2)))     # overdue
    db.add(_task("MT-2", 1, "Open", today - timedelta(days=1)))     # NULL'd below -> overdue
    db.add(_task("MT-3", 1, "Open", today + timedelta(days=2)))     # NULL'd below but FUTURE -> no
    db.commit()
    # Force genuine NULLs (the ORM default would refill an explicit None).
    db.execute(text("UPDATE maintenance_tasks SET status = NULL WHERE task_no IN ('MT-2', 'MT-3')"))
    db.commit()
    db.expire_all()

    out = generate_maintenance_overdue_escalations(db=db, current_user=USER)
    # MT-1 (Open, past) + MT-2 (NULL, past) = 2. MT-3 is NULL but future -> excluded.
    assert out == {"created": 2}, out
    assert _open_titles(db) == {"Maintenance overdue: MT-1", "Maintenance overdue: MT-2"}, _open_titles(db)
    print("PASS NULL-status past task raises an escalation (2, not 1); future NULL excluded")


def test_null_status_existing_escalation_is_deduped_not_duplicated():
    """The dedup guard must treat a NULL-status EXISTING escalation as still open,
    matching the "NULL is not terminal" convention the overdue-task selection above
    and every open-escalation reader already use. status is String default="Open"
    (not NOT NULL), so a raw-SQL / migration / cleared-field row can hold a real
    NULL; SQL's `status != 'Resolved'` is NULL — not TRUE — for it, so the bare
    predicate MISSED the open escalation and raised a DUPLICATE, inflating the very
    open-escalation count the readers report. Regression guard for the
    or_(status IS NULL, ...) dedup fix."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(_task("MT-1", 1, "Open", today - timedelta(days=2)))     # overdue
    # A pre-existing open escalation for MT-1 whose status is a genuine NULL.
    db.add(models.Escalation(
        title="Maintenance overdue: MT-1", severity="Medium", owner="tech",
        department="Maintenance", status="Open", source="Maintenance"))
    db.commit()
    # Force a genuine NULL (the ORM default would refill an explicit None).
    db.execute(text("UPDATE escalations SET status = NULL "
                    "WHERE title = 'Maintenance overdue: MT-1'"))
    db.commit()
    db.expire_all()

    out = generate_maintenance_overdue_escalations(db=db, current_user=USER)
    assert out == {"created": 0}, out                     # NULL-status one is still open
    assert db.query(models.Escalation).count() == 1       # no duplicate raised
    print("PASS a NULL-status open escalation is deduped, not duplicated")


def test_empty_and_all_completed_raise_nothing():
    db = _fresh_session()
    today = datetime.utcnow().date()
    # empty
    assert generate_maintenance_overdue_escalations(db=db, current_user=USER) == {"created": 0}
    # only a completed past task -> nothing overdue
    db.add(_task("MT-9", 1, "Completed", today - timedelta(days=5)))
    db.commit()
    assert generate_maintenance_overdue_escalations(db=db, current_user=USER) == {"created": 0}
    assert db.query(models.Escalation).count() == 0
    print("PASS empty factory and all-completed raise no escalations")


if __name__ == "__main__":
    test_overdue_task_raises_one_escalation_idempotently()
    test_null_status_past_task_raises_an_escalation()
    test_null_status_existing_escalation_is_deduped_not_duplicated()
    test_empty_and_all_completed_raise_nothing()
    print("ALL MAINTENANCE OVERDUE-ESCALATION TESTS PASSED")
