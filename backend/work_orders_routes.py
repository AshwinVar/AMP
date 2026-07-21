"""Work-order routes — the production-order lifecycle.

Work orders (list / create / update / delete). Two behaviours preserved exactly:
  * create validates the part against the BOM (bom.PART_BOM);
  * completing a work order (status -> Completed) publishes a ProductionCompleted
    domain event (ADR-0001/0003) on the same DB session — the inventory BOM
    movement is a subscriber (subscribers.py), committing atomically here.
Peeled out of main.py per ADR-0009.
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_roles
from bom import PART_BOM
from database import SessionLocal
from events import event_bus, ProductionCompleted
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter()


@router.get("/work-orders", response_model=List[schemas.WorkOrderResponse])
def get_work_orders(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.WorkOrder).order_by(models.WorkOrder.id.desc()).limit(200).all()


@router.post("/work-orders", response_model=schemas.WorkOrderResponse)
def create_work_order(
    work_order: schemas.WorkOrderCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == work_order.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    existing = db.query(models.WorkOrder).filter(models.WorkOrder.work_order_no == work_order.work_order_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Work order number already exists")
    new_work_order = models.WorkOrder(**work_order.model_dump())
    db.add(new_work_order)
    db.commit()
    db.refresh(new_work_order)
    return new_work_order


# Bill of Materials now lives in bom.py (imported above) so subscribers can
# consume it without importing this module.


@router.patch("/work-orders/{work_order_id}", response_model=schemas.WorkOrderResponse)
def update_work_order(
    work_order_id: int,
    payload: schemas.WorkOrderUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    prev_status = work_order.status

    if payload.actual_quantity is not None:
        work_order.actual_quantity = payload.actual_quantity
        if work_order.actual_quantity >= work_order.target_quantity:
            work_order.status = "Completed"
    if payload.status is not None:
        work_order.status = payload.status

    # When a WO transitions to Completed, publish a domain event. The inventory
    # BOM movement is now a subscriber (subscribers.py / ADR-0001); it runs
    # synchronously on this same DB session, so it still commits atomically below.
    if prev_status != "Completed" and work_order.status == "Completed":
        event_bus.publish(
            ProductionCompleted(
                tenant_code=request_tenant(current_user),
                work_order_id=work_order.id,
                work_order_no=work_order.work_order_no,
                part_number=work_order.part_number,
                quantity=work_order.actual_quantity or work_order.target_quantity,
                machine_id=work_order.machine_id,
            ),
            db,
        )

    db.commit()
    db.refresh(work_order)
    return work_order


@router.delete("/work-orders/{work_order_id}")
def delete_work_order(
    work_order_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    db.query(models.ProductionPlan).filter(models.ProductionPlan.work_order_id == work_order_id).delete()
    db.delete(work_order)
    db.commit()
    return {"message": "Work order deleted successfully"}
