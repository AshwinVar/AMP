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
import csv
import io
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

import models
from csv_safe import read_upload_text
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal
from events import event_bus, DowntimeStarted
from machine_status import VALID_MACHINE_STATUSES, normalize_machine_status
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(tags=["Machines"])
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
    # Canonicalise the manual status exactly like the ingest paths (IoT / MQTT /
    # industrial gateway) already do, through the one shared vocabulary
    # (machine_status). Every status-based rollup — build_management_summary's
    # breakdown_count, build_smart_alerts, the Machine-Health twin's bands, the
    # predictive risk score — matches these EXACT strings, so a raw "RUNNING" /
    # "faulted" / typo written straight onto machine.status silently removed the
    # machine from all of them. Unlike a device burst (which the ingest paths
    # leave the value untouched on rather than reject), a manual UI/API action
    # gets an honest 400 with the valid set; a recognised-but-noncanonical value
    # ("running") is stored in its canonical form ("Running").
    canonical = normalize_machine_status(status)
    if canonical is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid machine status. Must be one of: {', '.join(VALID_MACHINE_STATUSES)}",
        )
    old_status = machine.status
    machine.status = canonical
    db.commit()
    db.refresh(machine)
    if old_status != canonical:
        db.add(models.MachineEvent(
            machine_id=machine.id, machine_name=machine.name,
            old_status=old_status, new_status=canonical,
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
    # Reject physically-impossible rows at the boundary — the same non-negative
    # guard every other counted ingest already applies (production-record #266,
    # quality #324, order/PO #346, production-plan #351, work-order #354, operator
    # #374). target_output / actual_output are plain ints in the schema (no ge=0),
    # so a negative slips past validation and silently corrupts every shift metric
    # that divides them: build_shift_kpis' efficiency is actual/target, so a
    # negative target inverts it into a negative percentage and a negative actual
    # prints a below-zero efficiency — a rate the data can't support (ADR-0010
    # honesty). The pooled shift attainment (analytics_summary, executive-oee,
    # build_management_summary sum these) goes with it. A target of 0 is a
    # legitimate UNPLANNED shift (the pooled efficiency already handles it), so only
    # negatives are rejected — never zero.
    if min(shift.target_output, shift.actual_output) < 0:
        raise HTTPException(status_code=400, detail="target_output and actual_output must be non-negative")
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
    # Reject physically-impossible rows at the boundary. Negative minutes/counts
    # are nonsensical and silently corrupt every window that includes them (a
    # negative good_count drags good-rate, scrap and OEE). The schema types are
    # plain ints, so nothing else stops them. (runtime > planned is intentionally
    # allowed — a job can run over its plan; the OEE/cost math handles it, ADR-0010.)
    if min(record.planned_minutes, record.runtime_minutes, record.ideal_cycle_time_seconds,
           record.total_count, record.good_count, record.rejected_count) < 0:
        raise HTTPException(status_code=400, detail="minutes and counts must be non-negative")
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


@router.post("/machines/import-csv")
async def import_machines_csv(
    file: UploadFile = File(...),
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Onboarding import: the machine roster from a CSV (the mirror of the
    /reports exports, in the same style as the inventory Tally import). Upserts
    by machine NAME within the tenant (machines carry no separate code column),
    accepts flexible headers (name/Machine, line/Line, status/Status,
    utilization/Utilization), defaults a new machine to Idle / 0% so the sim or
    MQTT feed takes over from real signals, and reports per-row errors instead
    of failing the whole file. Auto-scoped (ADR-0002): the query layer stamps and
    filters tenant_code, so an import can only ever touch the caller's roster."""
    text, encoding = await read_upload_text(file)
    reader = csv.DictReader(io.StringIO(text))
    created = updated = skipped = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        try:
            name = (row.get("name") or row.get("Name") or row.get("Machine") or "").strip()
            if not name:
                skipped += 1
                continue
            line = (row.get("line") or row.get("Line") or "").strip()
            # Unknown status vocabulary falls back to Idle rather than failing the
            # row — an import is bulk onboarding, not a live status change (the
            # PATCH endpoint keeps its strict 400 for manual edits).
            status = normalize_machine_status(
                (row.get("status") or row.get("Status") or "Idle").strip() or "Idle") or "Idle"
            try:
                utilization = int(float((row.get("utilization") or row.get("Utilization") or "0").strip() or 0))
            except (TypeError, ValueError):
                utilization = 0
            utilization = max(0, min(100, utilization))
            existing = db.query(models.Machine).filter(models.Machine.name == name).first()
            if existing:
                if line:
                    existing.line = line
                existing.status = status
                existing.utilization = utilization
                updated += 1
            else:
                db.add(models.Machine(name=name, status=status,
                                      utilization=utilization, downtime="0 min", line=line))
                created += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors[:10], "encoding": encoding}
