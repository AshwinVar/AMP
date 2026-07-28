"""Operator routes — the operator-app job-execution log.

Operator job executions (list / create / update / delete) — the shop-floor
operator's record of running a job. Plain CRUD; completing an execution stamps
completed_at (datetime.utcnow). Tenant scoping is handled by the ORM chokepoint
(ADR-0002). Peeled out of main.py per ADR-0009.
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/operator", tags=["Operator"])


@router.get("/executions", response_model=List[schemas.OperatorJobExecutionResponse])
def get_operator_executions(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.OperatorJobExecution).order_by(models.OperatorJobExecution.id.desc()).limit(500).all()


@router.post("/executions", response_model=schemas.OperatorJobExecutionResponse)
def create_operator_execution(execution: schemas.OperatorJobExecutionCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    existing = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.execution_no == execution.execution_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Execution number already exists")

    machine = db.query(models.Machine).filter(models.Machine.id == execution.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    # Reject physically-impossible rows at the boundary — the same guard every
    # other counted ingest already applies (production-record #266, quality #324,
    # order/PO #346, production-plan #351, work-order #354). good_count /
    # rejected_count are plain ints in the schema (no ge=0), so a negative slips
    # past validation and silently corrupts the workforce read-model
    # (ai/workforce): its quality rate is good / (good + rejected), so a negative
    # rejected drives an operator's yield ABOVE 100% and a negative good below 0% —
    # the exact honesty violation (a metric can't exceed the bound the data
    # supports), and the daily/plant totals it reconciles to go with it.
    if min(execution.good_count, execution.rejected_count) < 0:
        raise HTTPException(status_code=400, detail="counts must be non-negative")

    row = models.OperatorJobExecution(**execution.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Execution number is already in use")
    db.refresh(row)
    return row


@router.patch("/executions/{execution_id}", response_model=schemas.OperatorJobExecutionResponse)
def update_operator_execution(execution_id: int, payload: schemas.OperatorJobExecutionUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Operator execution not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)

    # good_count / rejected_count are Column(Integer, default=0) WITHOUT
    # nullable=False, and OperatorJobExecutionUpdate types each Optional[int]=None —
    # so a client can PATCH an explicit {"good_count": null} (exclude_unset keeps an
    # explicit null) and a legacy/raw-SQL row can already carry NULL. Left as None,
    # the response 500s because OperatorJobExecutionResponse types these fields as
    # non-optional int; a NEGATIVE patch value corrupts the yield window exactly as
    # on create. Coalesce a NULL to the column's own default of 0 (also healing a
    # pre-existing NULL row on any update), then reject a negative — the same guard
    # the quality-inspection PATCH (#324/#356) and the create path above apply.
    row.good_count = row.good_count or 0
    row.rejected_count = row.rejected_count or 0
    if min(row.good_count, row.rejected_count) < 0:
        raise HTTPException(status_code=400, detail="counts must be non-negative")

    if row.job_status == "Completed" and row.completed_at is None:
        row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


@router.delete("/executions/{execution_id}")
def delete_operator_execution(execution_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Operator execution not found")
    db.delete(row)
    db.commit()
    return {"message": "Operator execution deleted successfully"}
