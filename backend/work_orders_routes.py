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
from sqlalchemy.exc import IntegrityError
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


router = APIRouter(prefix="/work-orders", tags=["Work Orders"])


@router.get("", response_model=List[schemas.WorkOrderResponse])
def get_work_orders(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.WorkOrder).order_by(models.WorkOrder.id.desc()).limit(200).all()


@router.post("", response_model=schemas.WorkOrderResponse)
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
    # Reject physically-impossible rows at the boundary — parity with the
    # production-record (#266), quality (#324), order/PO (#346) and production-plan
    # (#351) ingests, which all already reject negatives here. target_quantity is a
    # plain int and actual_quantity an int defaulting to 0 in the schema (no ge=0),
    # so a negative slips past validation and corrupts every read-model that
    # consumes it: /analytics/work-orders achievement (actual/target), the
    # predictive-risk work-order pressure (target - actual), and — worst — a
    # completing WO publishes ProductionCompleted with a negative quantity, which
    # the inventory subscriber turns into a raw-material stock INCREASE and a
    # finished-goods DECREASE (subscribers.move_bom_on_production_completed). Checked
    # before the row is written so nothing impossible ever persists.
    if min(work_order.target_quantity, work_order.actual_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")
    new_work_order = models.WorkOrder(**work_order.model_dump())
    db.add(new_work_order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Work order number is already in use")
    db.refresh(new_work_order)
    return new_work_order


# Bill of Materials now lives in bom.py (imported above) so subscribers can
# consume it without importing this module.


@router.patch("/{work_order_id}", response_model=schemas.WorkOrderResponse)
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
        # Reject a negative actual_quantity PATCH (parity with create and the
        # order/PO / production-plan update guards): a negative actual corrupts the
        # work-order read-models exactly as on create, and if this PATCH (or a later
        # status->Completed) trips completion it publishes a negative-quantity
        # ProductionCompleted event that mis-moves inventory. Checked before it is
        # applied so a rejected PATCH never mutates the row (value + status intact).
        if payload.actual_quantity < 0:
            raise HTTPException(status_code=400, detail="quantities must be non-negative")
        work_order.actual_quantity = payload.actual_quantity
        if work_order.actual_quantity >= work_order.target_quantity:
            work_order.status = "Completed"
    if payload.status is not None:
        work_order.status = payload.status

    # When a WO transitions to Completed, publish a domain event. The inventory
    # BOM movement is now a subscriber (subscribers.py / ADR-0001); it runs
    # synchronously on this same DB session, so it still commits atomically below.
    if prev_status != "Completed" and work_order.status == "Completed":
        # The BOM movement (subscribers.move_bom_on_production_completed) consumes
        # raw material and receives finished goods for THIS quantity, so it must be
        # what was actually produced. `actual_quantity or target_quantity` was the
        # falsy-zero trap: actual_quantity is Column(Integer, default=0), and a
        # genuine 0 — a status-only "Completed" PATCH before any output was logged,
        # or a batch marked complete after being fully scrapped — is falsy, so the
        # expression fell through to target_quantity. The subscriber then moved the
        # WHOLE order's worth of BOM as if every unit had been made, fabricating
        # inventory movement the production data does not support (ADR-0010: never
        # invent a number the data can't back). A recorded 0 is real data — move 0.
        # An explicit-None check keeps the only legitimate fallback: a raw NULL
        # actual_quantity (raw-SQL / migration / cleared field) has no recorded
        # output at all, so it falls back to the order's target as before.
        completed_quantity = (
            work_order.actual_quantity
            if work_order.actual_quantity is not None
            else work_order.target_quantity
        )
        event_bus.publish(
            ProductionCompleted(
                tenant_code=request_tenant(current_user),
                work_order_id=work_order.id,
                work_order_no=work_order.work_order_no,
                part_number=work_order.part_number,
                quantity=completed_quantity,
                machine_id=work_order.machine_id,
            ),
            db,
        )

    db.commit()
    db.refresh(work_order)
    return work_order


@router.delete("/{work_order_id}")
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
