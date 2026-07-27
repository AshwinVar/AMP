"""Production-planning routes — plans and schedules.

The planning layer above the shop floor: production plans and production
schedules, each plain CRUD (list / create / update / delete). Tenant scoping is
handled by the ORM chokepoint (ADR-0002), so these need no explicit tenant
argument. Peeled out of main.py per ADR-0009.
"""
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


router = APIRouter(tags=["Production Planning"])


@router.get("/production-plans", response_model=List[schemas.ProductionPlanResponse])
def get_production_plans(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ProductionPlan).order_by(models.ProductionPlan.id.desc()).limit(200).all()


@router.post("/production-plans", response_model=schemas.ProductionPlanResponse)
def create_production_plan(
    plan: schemas.ProductionPlanCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == plan.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == plan.work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    existing = db.query(models.ProductionPlan).filter(models.ProductionPlan.plan_no == plan.plan_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Plan number already exists")

    # Reject physically-impossible rows at the boundary — parity with the
    # production-record (#266), quality (#324) and order/PO ingests, which all
    # already reject negatives here. planned_quantity / actual_quantity are plain
    # ints in the schema (no ge=0), so a negative slips past validation and
    # silently corrupts the plan-attainment read-model (ai/schedule.py): attainment
    # is actual/planned, so a negative actual drags every pooled rate below its true
    # value and a negative planned inverts it. (actual > planned is intentionally
    # allowed — a shift can over-produce its plan, attainment > 100%, unlike an
    # order that cannot dispatch more than was ordered.)
    if min(plan.planned_quantity, plan.actual_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

    new_plan = models.ProductionPlan(**plan.model_dump())
    db.add(new_plan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Plan number is already in use")
    db.refresh(new_plan)
    return new_plan


@router.patch("/production-plans/{plan_id}", response_model=schemas.ProductionPlanResponse)
def update_production_plan(
    plan_id: int,
    payload: schemas.ProductionPlanUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Production plan not found")

    if payload.actual_quantity is not None:
        # Reject a negative actual_quantity PATCH (parity with create and the
        # order/PO update guards): a negative actual corrupts the attainment
        # read-model exactly as on create and would be stored as a
        # physically-impossible value. Checked before it is applied, so a rejected
        # PATCH never mutates the row.
        if payload.actual_quantity < 0:
            raise HTTPException(status_code=400, detail="quantities must be non-negative")
        plan.actual_quantity = payload.actual_quantity
        if plan.actual_quantity >= plan.planned_quantity:
            plan.status = "Completed"

    if payload.status is not None:
        plan.status = payload.status

    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/production-plans/{plan_id}")
def delete_production_plan(
    plan_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Production plan not found")
    db.delete(plan)
    db.commit()
    return {"message": "Production plan deleted successfully"}


@router.get("/production-schedules", response_model=List[schemas.ProductionScheduleResponse])
def get_production_schedules(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id.desc()).limit(500).all()


@router.post("/production-schedules", response_model=schemas.ProductionScheduleResponse)
def create_production_schedule(
    schedule: schemas.ProductionScheduleCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.ProductionSchedule).filter(models.ProductionSchedule.schedule_no == schedule.schedule_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Schedule number already exists")

    machine = db.query(models.Machine).filter(models.Machine.id == schedule.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    if schedule.work_order_id:
        work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == schedule.work_order_id).first()
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if schedule.production_plan_id:
        plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == schedule.production_plan_id).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Production plan not found")

    new_schedule = models.ProductionSchedule(**schedule.model_dump())
    db.add(new_schedule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Schedule number is already in use")
    db.refresh(new_schedule)
    return new_schedule


@router.patch("/production-schedules/{schedule_id}", response_model=schemas.ProductionScheduleResponse)
def update_production_schedule(
    schedule_id: int,
    payload: schemas.ProductionScheduleUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    schedule = db.query(models.ProductionSchedule).filter(models.ProductionSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Production schedule not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, key, value)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/production-schedules/{schedule_id}")
def delete_production_schedule(
    schedule_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    schedule = db.query(models.ProductionSchedule).filter(models.ProductionSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Production schedule not found")

    db.delete(schedule)
    db.commit()
    return {"message": "Production schedule deleted successfully"}
