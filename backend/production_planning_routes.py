"""Production-planning routes — plans and schedules.

The planning layer above the shop floor: production plans and production
schedules, each plain CRUD (list / create / update / delete). Tenant scoping is
handled by the ORM chokepoint (ADR-0002), so these need no explicit tenant
argument. Peeled out of main.py per ADR-0009.
"""
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
    @app.get("/production-plans", response_model=List[schemas.ProductionPlanResponse])
    def get_production_plans(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
        return db.query(models.ProductionPlan).order_by(models.ProductionPlan.id.desc()).limit(200).all()

    @app.post("/production-plans", response_model=schemas.ProductionPlanResponse)
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

        new_plan = models.ProductionPlan(**plan.model_dump())
        db.add(new_plan)
        db.commit()
        db.refresh(new_plan)
        return new_plan

    @app.patch("/production-plans/{plan_id}", response_model=schemas.ProductionPlanResponse)
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
            plan.actual_quantity = payload.actual_quantity
            if plan.actual_quantity >= plan.planned_quantity:
                plan.status = "Completed"

        if payload.status is not None:
            plan.status = payload.status

        db.commit()
        db.refresh(plan)
        return plan

    @app.delete("/production-plans/{plan_id}")
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

    @app.get("/production-schedules", response_model=List[schemas.ProductionScheduleResponse])
    def get_production_schedules(
        db: Session = Depends(_get_db),
        current_user: dict = Depends(get_current_user),
    ):
        return db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id.desc()).limit(500).all()

    @app.post("/production-schedules", response_model=schemas.ProductionScheduleResponse)
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
        db.commit()
        db.refresh(new_schedule)
        return new_schedule

    @app.patch("/production-schedules/{schedule_id}", response_model=schemas.ProductionScheduleResponse)
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

    @app.delete("/production-schedules/{schedule_id}")
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

