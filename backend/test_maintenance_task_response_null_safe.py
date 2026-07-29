"""GET /maintenance/tasks survives NULL-valued nullable columns (response heal).

MaintenanceTask.status (String, default="Open"), MaintenanceTask.priority
(String, default="Medium") and MaintenanceTask.downtime_minutes (Integer,
default=0) are all declared WITHOUT nullable=False — the ORM default only fills a
value the *inserter* omitted, so a row written by raw SQL, a migration, or an
update that cleared the field can legitimately hold NULL. MaintenanceTaskResponse
types all three as non-optional (str / int), so a single such NULL row raised
Pydantic ValidationError during response serialisation and 500-ed the WHOLE
GET /maintenance/tasks list — one bad row hiding every good task. Same class
already healed on the machine / production-plan / operator-execution lists
(#375/#395) and on EscalationResponse (NULL status -> "Open").

The response model now coalesces each NULL to the column's own declared default on
the way out (status -> "Open", priority -> "Medium", downtime_minutes -> 0). Those
values keep the per-task list on the SAME basis as the /analytics/maintenance
rollup (rule-3): that endpoint treats a NULL status as still-overdue/open and reads
a NULL downtime as 0, so the healed row agrees with the headline it sums into. A
real recorded value — including a real 0 downtime — is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_maintenance_task_response_null_safe.py   (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import analytics_routes as AR
import factory_ops_routes as FOR
import models
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_task(db, tenant, planned_date, downtime_minutes=45, status="Open"):
    """A fully-populated task — proves real values (incl. a real 0 downtime) survive."""
    tok = T.set_current_tenant(tenant)
    try:
        t = models.MaintenanceTask(
            task_no="MT-OK",
            machine_id=1,
            task_type="Preventive",
            priority="High",
            assigned_to="Tech A",
            planned_date=planned_date,
            downtime_minutes=downtime_minutes,
            status=status,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
    finally:
        T.reset_current_tenant(tok)
    return t


def _insert_null_task(db, tenant, task_no, planned_date, downtime_minutes="NULL"):
    """A legacy row that left status / priority / downtime_minutes NULL.

    downtime_minutes can be pinned to a real value (to test the reconciliation of a
    NULL-STATUS but real-downtime task) or left NULL.
    """
    db.execute(
        text(
            "INSERT INTO maintenance_tasks "
            "(tenant_code, task_no, machine_id, task_type, priority, assigned_to, "
            " planned_date, downtime_minutes, status) "
            "VALUES (:t, :no, 1, 'Breakdown', NULL, 'Tech B', :pd, " + str(downtime_minutes) + ", NULL)"
        ),
        {"t": tenant, "no": task_no, "pd": planned_date.isoformat()},
    )
    db.commit()
    db.expire_all()


def test_response_heals_null_columns():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    today = datetime.utcnow().date()
    _seed_normal_task(db, "DEFAULT", planned_date=today, downtime_minutes=0)  # real 0 downtime
    _insert_null_task(db, "DEFAULT", "MT-NULL", planned_date=today)

    by_no = {t.task_no: schemas.MaintenanceTaskResponse.model_validate(t)
             for t in db.query(models.MaintenanceTask).order_by(models.MaintenanceTask.id).all()}
    assert set(by_no) == {"MT-OK", "MT-NULL"}, set(by_no)

    # The NULL row heals to each column's declared default.
    null_row = by_no["MT-NULL"]
    assert null_row.status == "Open", null_row.status
    assert null_row.priority == "Medium", null_row.priority
    assert null_row.downtime_minutes == 0, null_row.downtime_minutes

    # The real row is untouched — crucially a real recorded 0 downtime is NOT a
    # healed NULL: the mode="before" heal only rewrites None.
    ok_row = by_no["MT-OK"]
    assert ok_row.status == "Open", ok_row.status
    assert ok_row.priority == "High", ok_row.priority
    assert ok_row.downtime_minutes == 0, ok_row.downtime_minutes
    print("PASS MaintenanceTaskResponse heals NULL status/priority/downtime, preserves real values")


def test_list_endpoint_survives_null_row():
    # End-to-end: the whole GET /maintenance/tasks list serialises without a 500 even
    # when a legacy NULL row is present, and every good task still comes back.
    db = _iso_session()
    today = datetime.utcnow().date()
    _seed_normal_task(db, "DEFAULT", planned_date=today)
    _insert_null_task(db, "DEFAULT", "MT-NULL", planned_date=today)

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = FOR.get_maintenance_tasks(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.MaintenanceTaskResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    nos = {s.task_no for s in serialised}
    assert nos == {"MT-OK", "MT-NULL"}, nos
    for s in serialised:
        assert isinstance(s.status, str) and s.status
        assert isinstance(s.priority, str) and s.priority
        assert isinstance(s.downtime_minutes, int)
    print("PASS GET /maintenance/tasks returns the full list with a NULL row present (no 500)")


def test_list_reconciles_with_analytics_maintenance():
    # rule-3: the per-task list and the /analytics/maintenance headline must use the
    # SAME basis for a NULL-status row. A past-dated NULL-status task with a real
    # 30-minute downtime must be (a) shown "Open" by the list heal, (b) counted as
    # OVERDUE by the analytics headline (NULL status treated as not-Completed), and
    # (c) have its 30 minutes inside the analytics total_downtime — the two surfaces
    # agree on the identical row.
    db = _iso_session()
    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)

    # One completed task (NOT overdue) with 10 min, and one NULL-status past-dated
    # task with 30 min. Independently-derived expectations: overdue == 1 (only the
    # NULL-status past-dated one), total_downtime == 40 (10 + 30).
    _seed_normal_task(db, "DEFAULT", planned_date=yesterday, downtime_minutes=10, status="Completed")
    _insert_null_task(db, "DEFAULT", "MT-OVERDUE", planned_date=yesterday, downtime_minutes=30)

    tok = T.set_current_tenant("DEFAULT")
    try:
        analytics = AR.get_maintenance_analytics(db=db, current_user={"tenant": "DEFAULT"})
        rows = FOR.get_maintenance_tasks(db=db, current_user={"tenant": "DEFAULT"})
        serialised = {s.task_no: s
                      for s in (schemas.MaintenanceTaskResponse.model_validate(r) for r in rows)}
    finally:
        T.reset_current_tenant(tok)

    # Headline: the NULL-status past-dated task is counted as overdue.
    assert analytics["overdue"] == 1, analytics["overdue"]
    # Headline downtime sums both tasks' minutes, reading the NULL-status task's real 30.
    assert analytics["total_downtime_minutes"] == 40, analytics["total_downtime_minutes"]
    # List: the same NULL-status row is displayed "Open" (not-Completed = open),
    # reconciling with the headline that counted it as overdue.
    assert serialised["MT-OVERDUE"].status == "Open", serialised["MT-OVERDUE"].status
    assert serialised["MT-OVERDUE"].downtime_minutes == 30, serialised["MT-OVERDUE"].downtime_minutes
    print("PASS the task list and /analytics/maintenance agree on a NULL-status row (overdue=1, downtime=40)")


if __name__ == "__main__":
    test_response_heals_null_columns()
    test_list_endpoint_survives_null_row()
    test_list_reconciles_with_analytics_maintenance()
    print("ALL MAINTENANCE-TASK-RESPONSE NULL-SAFE TESTS PASSED")
