"""Industrial-IoT routes — telemetry, devices, signals and PLC mappings.

The edge/OT-facing surface: IoT telemetry ingest + read, industrial devices,
their signals, and PLC signal mappings. Plain CRUD; tenant scoping is handled by
the ORM chokepoint (ADR-0002). Peeled out of main.py per ADR-0009.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import paging
import schemas
import telemetry_coverage
from auth import get_current_user, require_roles
from database import SessionLocal
from machine_status import clamp_utilization, normalize_machine_status


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(tags=["Industrial IoT"])


@router.get("/iot/telemetry", response_model=List[schemas.IoTTelemetryResponse])
def get_iot_telemetry(response: Response = None, limit: Optional[int] = None, offset: int = 0,
                      db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return paging.page(response, db.query(models.IoTTelemetry).order_by(models.IoTTelemetry.id.desc()), 500, limit, offset)


@router.post("/iot/telemetry", response_model=schemas.IoTTelemetryResponse)
def create_iot_telemetry(telemetry: schemas.IoTTelemetryCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == telemetry.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    row = models.IoTTelemetry(**telemetry.model_dump())
    db.add(row)

    signal = telemetry.signal_name.lower()
    if signal in ["utilization", "load", "efficiency"]:
        util = clamp_utilization(telemetry.numeric_value)
        if util is not None:
            machine.utilization = util

    if signal in ["status", "machine_status"]:
        # ADR-0021: the status history of source "iot", from every status
        # signal, recorded raw (an unrecognised value is disputed downstream).
        telemetry_coverage.record_message(
            db, machine.tenant_code, machine.id, telemetry_coverage.IOT,
            telemetry.signal_value, telemetry_coverage.received_at())
        # Only apply a RECOGNISED status; an unknown string would drop the machine
        # from every status-based report, so leave the machine's status untouched.
        new_status = normalize_machine_status(telemetry.signal_value)
        old_status = machine.status
        if new_status and new_status != old_status:
            machine.status = new_status
            db.add(models.MachineEvent(
                machine_id=machine.id,
                machine_name=machine.name,
                old_status=old_status,
                new_status=machine.status,
                utilization=machine.utilization,
                source="iot",
            ))

    db.commit()
    db.refresh(row)
    return row


@router.get("/industrial/devices", response_model=List[schemas.IndustrialDeviceResponse])
def get_industrial_devices(response: Response = None, limit: Optional[int] = None, offset: int = 0,
                           db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return paging.page(response, db.query(models.IndustrialDevice).order_by(models.IndustrialDevice.id.desc()), 300, limit, offset)


@router.post("/industrial/devices", response_model=schemas.IndustrialDeviceResponse)
def create_industrial_device(device: schemas.IndustrialDeviceCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.device_code == device.device_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device code already exists")
    row = models.IndustrialDevice(**device.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Device code is already in use")
    db.refresh(row)
    return row


@router.patch("/industrial/devices/{device_id}", response_model=schemas.IndustrialDeviceResponse)
def update_industrial_device(device_id: int, payload: schemas.IndustrialDeviceUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Industrial device not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.get("/industrial/signals", response_model=List[schemas.IndustrialSignalResponse])
def get_industrial_signals(response: Response = None, limit: Optional[int] = None, offset: int = 0,
                           db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return paging.page(response, db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.desc()), 500, limit, offset)


@router.post("/industrial/signals", response_model=schemas.IndustrialSignalResponse)
def create_industrial_signal(signal: schemas.IndustrialSignalCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    device = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.id == signal.device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Industrial device not found")

    row = models.IndustrialSignal(**signal.model_dump())
    db.add(row)

    if signal.machine_id:
        machine = db.query(models.Machine).filter(models.Machine.id == signal.machine_id).first()
        if machine:
            field = signal.signal_name.lower()
            if field in ["status", "machine_status", "state"]:
                # ADR-0021: the status history of source "industrial_gateway".
                telemetry_coverage.record_message(
                    db, machine.tenant_code, machine.id,
                    telemetry_coverage.INDUSTRIAL_GATEWAY, signal.signal_value,
                    telemetry_coverage.received_at())
                new_status = normalize_machine_status(signal.signal_value)
                old_status = machine.status
                if new_status and new_status != old_status:
                    machine.status = new_status
                    db.add(models.MachineEvent(
                        machine_id=machine.id,
                        machine_name=machine.name,
                        old_status=old_status,
                        new_status=machine.status,
                        utilization=machine.utilization,
                        source="industrial_gateway",
                    ))
            if field in ["utilization", "load", "efficiency"]:
                util = clamp_utilization(signal.numeric_value)
                if util is not None:
                    machine.utilization = util
            if field == "downtime":
                machine.downtime = signal.signal_value

    db.commit()
    db.refresh(row)
    return row


@router.get("/industrial/mappings", response_model=List[schemas.PlcSignalMappingResponse])
def get_plc_signal_mappings(response: Response = None, limit: Optional[int] = None, offset: int = 0,
                            db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return paging.page(response, db.query(models.PlcSignalMapping).order_by(models.PlcSignalMapping.id.desc()), 300, limit, offset)


@router.post("/industrial/mappings", response_model=schemas.PlcSignalMappingResponse)
def create_plc_signal_mapping(mapping: schemas.PlcSignalMappingCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.PlcSignalMapping).filter(models.PlcSignalMapping.mapping_code == mapping.mapping_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Mapping code already exists")
    row = models.PlcSignalMapping(**mapping.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mapping code is already in use")
    db.refresh(row)
    return row
