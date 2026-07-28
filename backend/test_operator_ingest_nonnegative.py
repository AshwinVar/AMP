"""Operator job-execution ingest rejects impossible rows at the boundary.

The operator terminal log (operator_job_executions) is the source the workforce
read-model (ai/workforce) trusts: an operator's first-pass quality rate is
good / (good + rejected). good_count / rejected_count are plain ints in the
schema (no ge=0) and Column(Integer, default=0) WITHOUT nullable=False, so
nothing but the handler stops a negative or a NULL from being stored. This was
the ONE counted ingest missing the non-negative guard its siblings already have
(production-record #266, quality #324, order/PO #346, production-plan #351,
work-order #354).

A negative rejected drives the yield ABOVE 100%; a NULL count 500s the response
(OperatorJobExecutionResponse types these as non-optional int). Both are pinned
below with independently-derived numbers.

Run:  python backend/test_operator_ingest_nonnegative.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import schemas
import operator_routes
from ai.workforce import _quality_rate
from database import Base

_ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.commit()
    return db


def _exec(**kw):
    base = dict(execution_no="EX-1", operator_name="Sam", machine_id=1,
                job_status="Started", good_count=90, rejected_count=10)
    base.update(kw)
    return schemas.OperatorJobExecutionCreate(**base)


def test_why_it_matters_negative_reject_breaks_yield_honesty():
    # The read-model's own quality-rate definition, fed the value the OLD ingest
    # would have stored. good=10, rejected=-4 -> total booked = 6, and the yield
    # reads 10/6 = 167% — a first-pass quality rate ABOVE 100%, which the data
    # cannot support. The ingest guard exists precisely to keep this row out.
    assert _quality_rate(10, 10 + (-4)) == 167, _quality_rate(10, 6)
    # A clean row of the same good volume reads an honest, bounded rate.
    assert _quality_rate(10, 10 + 2) == 83, _quality_rate(10, 12)
    print("PASS a negative rejected would drive the operator yield to 167% (>100%, dishonest)")


def test_create_rejects_negative_counts():
    db = _sess()
    for bad in (dict(good_count=-5, rejected_count=15),
                dict(good_count=90, rejected_count=-1)):
        try:
            operator_routes.create_operator_execution(_exec(**bad), db=db, current_user=_ADMIN)
            assert False, f"negative count should 400: {bad}"
        except HTTPException as e:
            assert e.status_code == 400, e.status_code
    # a valid row still succeeds and stores the exact counts
    r = operator_routes.create_operator_execution(_exec(), db=db, current_user=_ADMIN)
    assert r.good_count == 90 and r.rejected_count == 10, (r.good_count, r.rejected_count)
    print("PASS create rejects negative good/rejected; a valid row still stores exactly")


def test_patch_rejects_negative_counts():
    db = _sess()
    operator_routes.create_operator_execution(_exec(), db=db, current_user=_ADMIN)
    row = db.query(models.OperatorJobExecution).first()
    try:
        operator_routes.update_operator_execution(
            row.id, schemas.OperatorJobExecutionUpdate(rejected_count=-3),
            db=db, current_user=_ADMIN)
        assert False, "negative PATCH should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    # the rejected PATCH did not mutate the stored value (no commit ran)
    db.refresh(row)
    assert row.rejected_count == 10, row.rejected_count
    print("PASS PATCH rejects a negative count and leaves the stored row unchanged")


def test_patch_explicit_null_heals_to_zero():
    # A client sending {"good_count": null} used to store None, then 500 on the
    # response (good_count is a non-optional int there). It now heals to 0.
    db = _sess()
    operator_routes.create_operator_execution(_exec(), db=db, current_user=_ADMIN)
    row = db.query(models.OperatorJobExecution).first()
    out = operator_routes.update_operator_execution(
        row.id, schemas.OperatorJobExecutionUpdate(good_count=None),
        db=db, current_user=_ADMIN)
    assert out.good_count == 0, out.good_count
    assert out.rejected_count == 10, out.rejected_count   # untouched field preserved
    print("PASS PATCH {good_count: null} heals to 0 (no 500) and leaves other counts intact")


def test_patch_heals_preexisting_null_row_on_any_update():
    # A legacy/raw-SQL row already carrying NULL is healed to 0 by any update,
    # even one that only changes an unrelated field.
    db = _sess()
    operator_routes.create_operator_execution(_exec(), db=db, current_user=_ADMIN)
    row = db.query(models.OperatorJobExecution).first()
    row.good_count = None            # simulate a pre-existing NULL from raw SQL
    db.commit()
    out = operator_routes.update_operator_execution(
        row.id, schemas.OperatorJobExecutionUpdate(job_status="Completed"),
        db=db, current_user=_ADMIN)
    assert out.good_count == 0, out.good_count
    assert out.job_status == "Completed"
    print("PASS a pre-existing NULL good_count heals to 0 on any update")


if __name__ == "__main__":
    test_why_it_matters_negative_reject_breaks_yield_honesty()
    test_create_rejects_negative_counts()
    test_patch_rejects_negative_counts()
    test_patch_explicit_null_heals_to_zero()
    test_patch_heals_preexisting_null_row_on_any_update()
    print("OPERATOR INGEST OK: negatives rejected on create+PATCH; NULL counts healed to 0")
