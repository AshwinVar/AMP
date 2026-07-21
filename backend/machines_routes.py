"""Machine & telemetry routes — the MES core CRUD.

The shop-floor primitives: machines (list / create / delete / status), downtime
logs, shift data, production records, and the machine-event stream. Peeled out
of main.py per ADR-0009. Mostly plain CRUD; two behaviours are preserved
exactly:
  * `PATCH /machines/{id}/status` records a `MachineEvent` row on a real change
    (keeps the timeline live);
  * `POST /downtime-logs` publishes a `DowntimeStarted` domain event on the bus
    (ADR-0001/0003) before commit, so the event and any AI reaction commit
    atomically with the log.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal
from events import event_bus, DowntimeStarted
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter()
# ── Machines ──────────────────────────────────────────────────


@router.get("/machines", response_model=List[schemas.MachineResponse])
def get_machines(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.Machine).order_by(models.Machine.id.asc()).all()


@router.post("/machines", response_model=schemas.MachineResponse)
def create_machine(machine: schemas.MachineCreate, db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    new_machine = models.Machine(**machine.model_dump())
    db.add(new_machine)
    db.commit()
    db.refresh(new_machine)
    return new_machine


@router.delete("/machines/{machine_id}")
def delete_machine(machine_id: int, db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    db.delete(machine)
    db.commit()
    return {"message": "Machine deleted successfully"}


@router.patch("/machines/{machine_id}/status")
def update_machine_status(machine_id: int, status: str, db: Session = Depends(_get_db),
                          current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    old_status = machine.status
    machine.status = status
    db.commit()
    db.refresh(machine)
    if old_status != status:
        db.add(models.MachineEvent(
            machine_id=machine.id, machine_name=machine.name,
            old_status=old_status, new_status=status,
            utilization=machine.utilization, source="manual",
        ))
        db.commit()
    return machine

# ── Downtime logs (publishes DowntimeStarted) ─────────────────


@router.get("/downtime-logs", response_model=List[schemas.DowntimeResponse])
def get_downtime_logs(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(100).all()


@router.post("/downtime-logs", response_model=schemas.DowntimeResponse)
def create_downtime_log(downtime: schemas.DowntimeCreate, db: Session = Depends(_get_db),
                        current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == downtime.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    new_log = models.DowntimeLog(**downtime.model_dump())
    db.add(new_log)
    # Widen the event stream: a machine entered downtime (ADR-0003). Published
    # before commit so the event and any AI reaction commit atomically.
    event_bus.publish(DowntimeStarted(
        tenant_code=request_tenant(current_user),
        machine_id=downtime.machine_id,
        reason=downtime.reason,
        duration=downtime.duration,
    ), db)
    db.commit()
    db.refresh(new_log)
    return new_log

# ── Shift data ────────────────────────────────────────────────


@router.get("/shifts", response_model=List[schemas.ShiftResponse])
def get_shifts(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(100).all()


@router.post("/shifts", response_model=schemas.ShiftResponse)
def create_shift(shift: schemas.ShiftCreate, db: Session = Depends(_get_db),
                 current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    new_shift = models.ShiftData(**shift.model_dump())
    db.add(new_shift)
    db.commit()
    db.refresh(new_shift)
    return new_shift

# ── Production records ────────────────────────────────────────


@router.get("/production-records", response_model=List[schemas.ProductionResponse])
def get_production_records(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()


@router.post("/production-records", response_model=schemas.ProductionResponse)
def create_production_record(record: schemas.ProductionCreate, db: Session = Depends(_get_db),
                             current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == record.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if record.good_count + record.rejected_count != record.total_count:
        raise HTTPException(status_code=400, detail="good_count + rejected_count must equal total_count")
    new_record = models.ProductionRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

# ── Machine event stream ──────────────────────────────────────


@router.get("/machine-events")
def get_machine_events(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(200).all()
