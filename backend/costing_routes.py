"""Costing routes — cost-record CRUD + the costing analytics rollup.

The financial cost-tracking domain: manual cost records (list / create / update /
delete) and a costing analytics summary (cost per good unit, spend by type and
department). Fully self-contained — only CostRecord / PurchaseOrder /
ProductionRecord. Peeled out of main.py per ADR-0009 (register(app) pattern).
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Cost number is already in use")
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
    # Aggregate in SQL, NULL-safe. amount / received_quantity are nullable Integer
    # columns (Column(Integer, default=0) with no nullable=False — the default only
    # fills a value the *inserter* omitted, so a raw-SQL / migration / cleared-field
    # write can store NULL). A plain Python sum(...) then did int + None ->
    # TypeError -> an unhandled 500 on the whole costing rollup — the same NULL bug
    # already fixed for these very columns in orders_routes / saas_routes. Doing the
    # sums in SQL with func.coalesce(func.sum(...), 0) fixes that AND bounds the
    # scan: the old code pulled all of cost_records / purchase_orders /
    # production_records into Python just to add them up (bound growing-table scans
    # in SQL). All three models are tenant-scoped (tenancy.SCOPED_MODELS), so the
    # do_orm_execute hook still filters each aggregate to the request's tenant.
    total_cost_records, manual_cost = db.query(
        func.count(models.CostRecord.id),
        func.coalesce(func.sum(models.CostRecord.amount), 0),
    ).one()
    material_spend = db.query(
        func.coalesce(func.sum(models.PurchaseOrder.received_quantity), 0)
    ).scalar()
    production_units = db.query(
        func.coalesce(func.sum(models.ProductionRecord.good_count), 0)
    ).scalar()

    total_cost_records = int(total_cost_records)
    manual_cost = int(manual_cost)
    material_spend = int(material_spend)
    production_units = int(production_units)

    # Spend by type / department, grouped in SQL (one row per distinct key — a
    # small, naturally-bounded result set), NULL-safe on the amount. The parts sum
    # to manual_cost by construction: both coalesce amount and cover every row, so
    # the headline and its breakdowns reconcile. department keeps the endpoint's
    # existing rule — a NULL *or empty* label collapses to "Unassigned" — merged in
    # Python so both land in one bucket (SQL COALESCE alone would not fold "").
    by_type = {}
    for cost_type, total in (
        db.query(models.CostRecord.cost_type,
                 func.coalesce(func.sum(models.CostRecord.amount), 0))
        .group_by(models.CostRecord.cost_type)
        .all()
    ):
        by_type[cost_type] = by_type.get(cost_type, 0) + int(total)

    by_department = {}
    for department, total in (
        db.query(models.CostRecord.department,
                 func.coalesce(func.sum(models.CostRecord.amount), 0))
        .group_by(models.CostRecord.department)
        .all()
    ):
        key = department or "Unassigned"
        by_department[key] = by_department.get(key, 0) + int(total)

    # Per-unit cost to pence precision, not whole pounds: manufacturing per-unit
    # costs are usually sub-£1, so round(0.50) -> 0 fabricated a £0/unit. And with
    # no production the metric is undefined (you can't divide real cost by zero
    # units) -> None, shown as "—", never a misleading £0 while costs exist.
    cost_per_good_unit = round(manual_cost / production_units, 2) if production_units else None

    return {
        "total_cost_records": total_cost_records,
        "manual_cost_total": manual_cost,
        "production_units": production_units,
        "cost_per_good_unit": cost_per_good_unit,
        "supplier_receipt_units": material_spend,
        "by_type": by_type,
        "by_department": by_department,
    }
