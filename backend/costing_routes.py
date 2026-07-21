"""Costing routes — cost-record CRUD + the costing analytics rollup.

The financial cost-tracking domain: manual cost records (list / create / update /
delete) and a costing analytics summary (cost per good unit, spend by type and
department). Fully self-contained — only CostRecord / PurchaseOrder /
ProductionRecord. Peeled out of main.py per ADR-0009 (register(app) pattern).
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
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


router = APIRouter(tags=["Costing"])


@router.get("/cost-records", response_model=List[schemas.CostRecordResponse])
def get_cost_records(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.CostRecord).order_by(models.CostRecord.id.desc()).limit(500).all()


@router.post("/cost-records", response_model=schemas.CostRecordResponse)
def create_cost_record(cost: schemas.CostRecordCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.CostRecord).filter(models.CostRecord.cost_no == cost.cost_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Cost number already exists")
    row = models.CostRecord(**cost.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/cost-records/{cost_id}", response_model=schemas.CostRecordResponse)
def update_cost_record(cost_id: int, payload: schemas.CostRecordUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = db.query(models.CostRecord).filter(models.CostRecord.id == cost_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cost record not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/cost-records/{cost_id}")
def delete_cost_record(cost_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.CostRecord).filter(models.CostRecord.id == cost_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cost record not found")
    db.delete(row)
    db.commit()
    return {"message": "Cost record deleted successfully"}


@router.get("/analytics/costing")
def get_costing_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    costs = db.query(models.CostRecord).all()
    pos = db.query(models.PurchaseOrder).all()
    production = db.query(models.ProductionRecord).all()

    material_spend = sum(po.received_quantity for po in pos)
    manual_cost = sum(row.amount for row in costs)
    production_units = sum(row.good_count for row in production)

    by_type = {}
    by_department = {}
    for row in costs:
        by_type[row.cost_type] = by_type.get(row.cost_type, 0) + row.amount
        department = row.department or "Unassigned"
        by_department[department] = by_department.get(department, 0) + row.amount

    cost_per_good_unit = round(manual_cost / production_units) if production_units else 0

    return {
        "total_cost_records": len(costs),
        "manual_cost_total": manual_cost,
        "production_units": production_units,
        "cost_per_good_unit": cost_per_good_unit,
        "supplier_receipt_units": material_spend,
        "by_type": by_type,
        "by_department": by_department,
    }
