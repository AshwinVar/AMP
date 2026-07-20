"""Operator routes — the operator-app job-execution log.

Operator job executions (list / create / update / delete) — the shop-floor
operator's record of running a job. Plain CRUD; completing an execution stamps
completed_at (datetime.utcnow). Tenant scoping is handled by the ORM chokepoint
(ADR-0002). Peeled out of main.py per ADR-0009.
"""
from datetime import datetime
from typing import List

from fastapi import Depends, HTTPException
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


def register(app):
    @app.get("/operator/executions", response_model=List[schemas.OperatorJobExecutionResponse])
    def get_operator_executions(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        return db.query(models.OperatorJobExecution).order_by(models.OperatorJobExecution.id.desc()).limit(500).all()

    @app.post("/operator/executions", response_model=schemas.OperatorJobExecutionResponse)
    def create_operator_execution(execution: schemas.OperatorJobExecutionCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
        existing = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.execution_no == execution.execution_no).first()
        if existing:
            raise HTTPException(status_code=400, detail="Execution number already exists")

        machine = db.query(models.Machine).filter(models.Machine.id == execution.machine_id).first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        row = models.OperatorJobExecution(**execution.model_dump())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @app.patch("/operator/executions/{execution_id}", response_model=schemas.OperatorJobExecutionResponse)
    def update_operator_execution(execution_id: int, payload: schemas.OperatorJobExecutionUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
        row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Operator execution not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, key, value)
        if row.job_status == "Completed" and row.completed_at is None:
            row.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
        return row

    @app.delete("/operator/executions/{execution_id}")
    def delete_operator_execution(execution_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
        row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Operator execution not found")
        db.delete(row)
        db.commit()
        return {"message": "Operator execution deleted successfully"}

