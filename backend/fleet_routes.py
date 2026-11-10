"""Fleet Ops routes — autonomous material movement + mission control.

The /fleet surface (gated by the `fleet` pack in modules.json):

  GET  /fleet/state               one poll, the whole world: ticks the lazy
                                  simulation, then returns stations, vehicles,
                                  live tasks, open alerts and the KPI strip
  GET  /fleet/tasks               task history (paged, newest first)
  POST /fleet/tasks               raise a movement order by hand
  POST /fleet/tasks/{id}/cancel   withdraw a Pending/Assigned order
  POST /fleet/vehicles            register a vehicle (Admin)
  DELETE /fleet/vehicles/{id}     retire one (Admin; must be task-free)
  POST /fleet/alerts/{id}/advise  answer a vehicle's plea (Admin/Supervisor)
  POST /fleet/seed                six-vehicle starter fleet, idempotent (Admin)
  POST /fleet/sim/tick            advance the world N seconds explicitly
                                  (Admin; demos and tests own the clock)

Thin by design: every behaviour lives in fleet_engine (pure, session-in
functions); this module is validation, roles and shapes. Auto-scoped
(ADR-0002): the query hook stamps and filters tenant_code everywhere, and the
lazy tick keys its clock on the caller's tenant so two tenants' worlds advance
independently.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

import fleet_engine
import models
import paging
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant

router = APIRouter(prefix="/fleet", tags=["Fleet Ops"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Shapes ──────────────────────────────────────────────────────────────

class VehicleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    vehicle_type: str = "AMR"


class TaskCreate(BaseModel):
    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    payload: str = Field(default="", max_length=240)
    priority: int = Field(default=2, ge=1, le=3)


class AdviceBody(BaseModel):
    advice: str


class TickBody(BaseModel):
    # One lazy-tick cap per call keeps an "advance 1e9 s" request honest.
    seconds: float = Field(gt=0, le=fleet_engine.MAX_TICK_SECONDS)


def _vehicle_out(v):
    return {
        "id": v.id, "name": v.name, "vehicle_type": v.vehicle_type,
        "status": v.status, "x": round(v.x or 0, 2), "y": round(v.y or 0, 2),
        "battery_pct": round(v.battery_pct or 0, 1),
        "current_task_id": v.current_task_id,
        "odometer_m": round(v.odometer_m or 0, 1),
        "deliveries_total": v.deliveries_total or 0,
    }


def _task_out(t):
    return {
        "id": t.id, "origin": t.origin, "destination": t.destination,
        "payload": t.payload or "", "priority": t.priority or 2,
        "status": t.status, "vehicle_id": t.vehicle_id, "source": t.source,
        "requested_at": t.requested_at.isoformat() if t.requested_at else None,
        "assigned_at": t.assigned_at.isoformat() if t.assigned_at else None,
        "picked_up_at": t.picked_up_at.isoformat() if t.picked_up_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _alert_out(a):
    return {
        "id": a.id, "vehicle_id": a.vehicle_id, "task_id": a.task_id,
        "kind": a.kind, "severity": a.severity, "message": a.message or "",
        "status": a.status, "advice": a.advice,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


# ── The one poll ────────────────────────────────────────────────────────

@router.get("/state")
def fleet_state(db=Depends(_get_db), current_user: dict = Depends(get_current_user)):
    tenant = request_tenant(current_user)
    fleet_engine.tick(db, tenant=tenant)
    stations = fleet_engine.stations_for(db)
    vehicles = db.query(models.FleetVehicle).order_by(models.FleetVehicle.id.asc()).all()
    live = (db.query(models.TransportTask)
            .filter(models.TransportTask.status.in_(("Pending", "Assigned", "InTransit")))
            .order_by(models.TransportTask.priority.desc(),
                      models.TransportTask.requested_at.asc())
            .limit(50).all())
    alerts = (db.query(models.FleetAlert)
              .filter(models.FleetAlert.status == "Open")
              .order_by(models.FleetAlert.id.desc()).limit(50).all())
    return {
        "stations": stations,
        "vehicles": [_vehicle_out(v) for v in vehicles],
        "tasks": [_task_out(t) for t in live],
        "alerts": [_alert_out(a) for a in alerts],
        "kpis": fleet_engine.kpis(db),
    }


# ── Tasks (idea 1: the movement order) ──────────────────────────────────

@router.get("/tasks")
def list_tasks(response: Response = None, limit: Optional[int] = None, offset: int = 0,
               db=Depends(_get_db), current_user: dict = Depends(get_current_user)):
    rows = paging.page(
        response,
        db.query(models.TransportTask).order_by(models.TransportTask.id.desc()),
        100, limit, offset)
    return [_task_out(t) for t in rows]


@router.post("/tasks")
def create_task(body: TaskCreate, db=Depends(_get_db),
                current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    stations = fleet_engine.stations_for(db)
    for field, name in (("origin", body.origin), ("destination", body.destination)):
        if name not in stations:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown {field} '{name}'. Stations are the fixed anchors "
                       f"(Warehouse, Packing, Dispatch, Charge Dock) and this "
                       f"plant's machine names.")
    if body.origin == body.destination:
        raise HTTPException(status_code=400, detail="origin and destination must differ")
    task = models.TransportTask(origin=body.origin, destination=body.destination,
                                payload=body.payload, priority=body.priority,
                                source="manual")
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_out(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, db=Depends(_get_db),
                current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    task = db.query(models.TransportTask).filter(models.TransportTask.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("Pending", "Assigned"):
        # InTransit means the payload is on a vehicle mid-floor; a human must
        # advise the vehicle (return_task) rather than void the order under it.
        raise HTTPException(status_code=409,
                            detail=f"A task that is {task.status} cannot be cancelled")
    if task.vehicle_id:
        vehicle = db.query(models.FleetVehicle).filter(
            models.FleetVehicle.id == task.vehicle_id).first()
        if vehicle is not None and vehicle.current_task_id == task.id:
            vehicle.current_task_id = None
            vehicle.status = "Idle"
    task.status = "Cancelled"
    task.completed_at = datetime.utcnow()
    db.commit()
    return _task_out(task)


# ── Vehicles (the fleet roster) ─────────────────────────────────────────

@router.get("/vehicles")
def list_vehicles(db=Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return [_vehicle_out(v) for v in
            db.query(models.FleetVehicle).order_by(models.FleetVehicle.id.asc()).all()]


@router.post("/vehicles")
def create_vehicle(body: VehicleCreate, db=Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    if body.vehicle_type not in fleet_engine.SPEED_MPS:
        raise HTTPException(
            status_code=400,
            detail=f"vehicle_type must be one of: {', '.join(fleet_engine.SPEED_MPS)}")
    dock = fleet_engine.ANCHORS["Charge Dock"]
    vehicle = models.FleetVehicle(name=body.name, vehicle_type=body.vehicle_type,
                                  status="Idle", x=dock["x"] + 3.0, y=dock["y"])
    db.add(vehicle)
    # (tenant_code, name) is a UNIQUE constraint; caught rather than
    # pre-checked because a pre-check races (same shape as create_machine).
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A vehicle named '{body.name}' already exists in this workspace.")
    db.refresh(vehicle)
    return _vehicle_out(vehicle)


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, db=Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    vehicle = db.query(models.FleetVehicle).filter(
        models.FleetVehicle.id == vehicle_id).first()
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if vehicle.current_task_id:
        raise HTTPException(status_code=409,
                            detail=f"{vehicle.name} is on task #{vehicle.current_task_id}; "
                                   f"return the task first (advise return_task).")
    db.delete(vehicle)
    db.commit()
    return {"message": "Vehicle retired"}


# ── Assistance (idea 2: the human answers) ──────────────────────────────

@router.get("/alerts")
def list_alerts(response: Response = None, limit: Optional[int] = None, offset: int = 0,
                db=Depends(_get_db), current_user: dict = Depends(get_current_user)):
    rows = paging.page(
        response,
        db.query(models.FleetAlert).order_by(models.FleetAlert.id.desc()),
        100, limit, offset)
    return [_alert_out(a) for a in rows]


@router.post("/alerts/{alert_id}/advise")
def advise_alert(alert_id: int, body: AdviceBody, db=Depends(_get_db),
                 current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    alert = db.query(models.FleetAlert).filter(models.FleetAlert.id == alert_id).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.status != "Open":
        raise HTTPException(status_code=409, detail="This alert is already resolved")
    try:
        fleet_engine.advise(db, alert, body.advice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _alert_out(alert)


# ── Demo & deterministic clock ──────────────────────────────────────────

@router.post("/seed")
def seed_fleet(db=Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    created = fleet_engine.seed_demo_fleet(db)
    return {"created": created}


@router.post("/sim/tick")
def sim_tick(body: TickBody, db=Depends(_get_db),
             current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    applied = fleet_engine.tick(db, tenant=tenant, seconds=body.seconds)
    return {"applied_seconds": applied}
