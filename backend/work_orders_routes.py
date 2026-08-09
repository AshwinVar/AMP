"""Work-order routes — the production-order lifecycle.

Work orders (list / create / update / delete). The behaviour that matters here:
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
    # Create does NOT publish ProductionCompleted, so an order posted directly as
    # Completed has never moved its BOM — and must not start moving it on some
    # later unrelated PATCH. Stamping completed_at here keeps that pre-existing
    # behaviour exactly, and keeps the invariant "Completed => completed_at set"
    # true from the moment the row exists.
    if new_work_order.status == "Completed" and new_work_order.completed_at is None:
        new_work_order.completed_at = datetime.utcnow()
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
        # Guard the auto-complete against a NULL target_quantity. The column is
        # nullable=False, but that constraint is not retro-applied to a raw-SQL /
        # migration / legacy row (the same reason WorkOrderResponse heals it, and
        # the sibling production_planning_routes guards this exact comparison —
        # production_planning_routes.py:104), and `int >= None` raised TypeError
        # here — 500-ing the PATCH. With no known target we cannot say the order is
        # complete, so leave the status untouched rather than fabricate a
        # "Completed" from a missing target (ADR-0010: a state the data can't
        # support must not be invented).
        if work_order.target_quantity is not None and work_order.actual_quantity >= work_order.target_quantity:
            work_order.status = "Completed"
    if payload.status is not None:
        work_order.status = payload.status

    # When a WO reaches Completed for the FIRST time, publish a domain event. The
    # inventory BOM movement is a subscriber (subscribers.py / ADR-0001); it runs
    # synchronously on this same DB session, so it still commits atomically below.
    #
    # Gated on completed_at, not on prev_status. prev_status is read from the row
    # at the top of THIS request, so it only ever remembers one PATCH back —
    # reopening a finished order and finishing it again looked like a first
    # completion and moved the whole BOM a second time. The subscriber applies
    # the event's CUMULATIVE actual_quantity as an absolute move, so there is no
    # delta semantics that would make a re-fire harmless. Measured on a 10-unit
    # SHAFT-001 order (2 kg steel per unit), driven entirely from the work-order
    # table UI, which renders all four statuses for an already-Completed row:
    #
    #   PATCH actual_quantity=10     raw 100->80, fg 0->10   correct
    #   reopen (Running) -> Completed  raw 80->60, fg 10->20
    #   reopen -> Completed again      raw 60->40, fg 20->30
    #
    # 40 kg written off and 30 units booked for 10 units of real production, and
    # unbounded in the number of cycles. Each re-fire also wrote a fresh
    # "Issue 20 / Receive 10" pair, so the ledger corroborated the corrupted
    # figure instead of exposing it — the same shape as the repeat cycle-count
    # approve fixed in enterprise_inventory_routes.
    #
    # This condition SUBSUMES the old one: a repeat PATCH with status=Completed
    # and no intervening reopen never republished before (prev_status was already
    # "Completed") and still does not, because completed_at is set.
    if work_order.status == "Completed" and work_order.completed_at is None:
        work_order.completed_at = datetime.utcnow()
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
