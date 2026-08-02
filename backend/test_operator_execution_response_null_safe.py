"""GET /operator/executions survives a NULL job_status (response heal).

OperatorJobExecution.job_status is Column(String, default="Started") declared
WITHOUT nullable=False — the ORM default only fills a value the *inserter*
omitted, so a row written by raw SQL, a migration, or a PATCH that cleared the
field can legitimately hold NULL. OperatorJobExecutionResponse types job_status as
a non-optional str, so a single such NULL row raised Pydantic ValidationError
during response serialisation and 500-ed the WHOLE GET /operator/executions list —
one bad row hiding every good execution.

The NULL is API-reachable: OperatorJobExecutionUpdate types job_status
Optional[str]=None, and the PATCH handler's setattr loop over model_dump(
exclude_unset=True) persists an explicit {"job_status": null}. So this isn't only
a legacy-row hazard — a live client can poison the list for the whole tenant.

The response model now coalesces a NULL job_status to the column's own declared
default "Started" (the same heal EscalationResponse and MaintenanceTaskResponse
already apply to their nullable string-default status columns, and the twin of the
in-handler good_count/rejected_count heal). A real recorded status is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_operator_execution_response_null_safe.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import operator_routes as OR
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _insert_execution(db, tenant, execution_no, job_status, good_count=5, rejected_count=1):
    """Insert an execution row directly. job_status may be a quoted literal or the
    bare token NULL — the raw path is how a legacy / migration / cleared-field row
    comes to hold a genuine NULL (bypassing the ORM default)."""
    status_sql = "NULL" if job_status is None else "'" + job_status + "'"
    db.execute(
        text(
            "INSERT INTO operator_job_executions "
            "(tenant_code, execution_no, operator_name, machine_id, job_status, "
            " good_count, rejected_count) "
            "VALUES (:t, :no, 'Op A', 1, " + status_sql + ", :g, :r)"
        ),
        {"t": tenant, "no": execution_no, "g": good_count, "r": rejected_count},
    )
    db.commit()
    db.expire_all()


def test_response_heals_null_job_status():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    _insert_execution(db, "DEFAULT", "EX-OK", job_status="Running")
    _insert_execution(db, "DEFAULT", "EX-NULL", job_status=None)

    by_no = {e.execution_no: schemas.OperatorJobExecutionResponse.model_validate(e)
             for e in db.query(models.OperatorJobExecution)
                        .order_by(models.OperatorJobExecution.id).all()}
    assert set(by_no) == {"EX-OK", "EX-NULL"}, set(by_no)

    # The NULL row heals to the column's declared default.
    assert by_no["EX-NULL"].job_status == "Started", by_no["EX-NULL"].job_status
    # A real recorded status is NOT rewritten (mode="before" only touches None).
    assert by_no["EX-OK"].job_status == "Running", by_no["EX-OK"].job_status
    print("PASS OperatorJobExecutionResponse heals a NULL job_status to 'Started', preserves a real status")


def test_list_endpoint_survives_null_row():
    # End-to-end: the whole GET /operator/executions list serialises without a 500
    # even when a NULL-job_status row is present, and every good row still comes back.
    db = _iso_session()
    _insert_execution(db, "DEFAULT", "EX-OK", job_status="Completed")
    _insert_execution(db, "DEFAULT", "EX-NULL", job_status=None)

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = OR.get_operator_executions(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.OperatorJobExecutionResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    nos = {s.execution_no for s in serialised}
    assert nos == {"EX-OK", "EX-NULL"}, nos
    for s in serialised:
        assert isinstance(s.job_status, str) and s.job_status
    print("PASS GET /operator/executions returns the full list with a NULL job_status row (no 500)")


def test_patch_null_job_status_is_reachable_and_healed():
    # API-reachability: PATCH {"job_status": null} persists a NULL through the
    # handler's setattr loop, and the PATCH response — plus every later GET —
    # heals it to "Started" rather than 500-ing.
    db = _iso_session()
    _insert_execution(db, "DEFAULT", "EX-PATCH", job_status="Running", good_count=7, rejected_count=2)

    tok = T.set_current_tenant("DEFAULT")
    try:
        row = db.query(models.OperatorJobExecution).filter_by(execution_no="EX-PATCH").one()
        # job_status explicitly set to None -> included by exclude_unset (mirrors {"job_status": null}).
        updated = OR.update_operator_execution(
            execution_id=row.id,
            payload=schemas.OperatorJobExecutionUpdate(job_status=None),
            db=db,
            current_user={"tenant": "DEFAULT", "role": "Operator"},
        )
        # The handler committed a genuine NULL to the column...
        db.expire_all()
        raw_status = db.execute(
            text("SELECT job_status FROM operator_job_executions WHERE execution_no = 'EX-PATCH'")
        ).scalar()
        # ...and the good_count/rejected_count are untouched by this patch.
        resp = schemas.OperatorJobExecutionResponse.model_validate(updated)
    finally:
        T.reset_current_tenant(tok)

    assert raw_status is None, f"expected NULL persisted, got {raw_status!r}"
    # The PATCH response heals the NULL to the column default rather than 500-ing.
    assert resp.job_status == "Started", resp.job_status
    # The unrelated counts survive the patch (independently derived: 7 and 2).
    assert resp.good_count == 7, resp.good_count
    assert resp.rejected_count == 2, resp.rejected_count
    print("PASS PATCH {job_status: null} persists NULL and the response heals it to 'Started' (no 500)")


if __name__ == "__main__":
    test_response_heals_null_job_status()
    test_list_endpoint_survives_null_row()
    test_patch_null_job_status_is_reachable_and_healed()
    print("ALL OPERATOR-EXECUTION-RESPONSE NULL-SAFE TESTS PASSED")
