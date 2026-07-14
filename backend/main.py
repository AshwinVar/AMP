import asyncio
import csv
import io
import os
import re
from datetime import datetime
from typing import List

from dotenv import load_dotenv
load_dotenv()

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import engine, SessionLocal, Base
from security import hash_password, verify_password, needs_rehash
from auth import create_access_token, get_current_user, require_roles
from live_ws import manager
from mqtt_service import start_mqtt_service
from analytics_engine import (
    build_management_summary,
    build_shift_kpis,
    build_oee_trends,
    build_smart_alerts,
)
from report_generator import build_daily_summary_text

import models
import schemas
import tenancy
import enterprise_inventory_routes
import gmats_inventory_routes
import platform_routes
from platform_routes import log_audit
import ai_copilot
import industrial_adapters
from bom import PART_BOM
from events import event_bus, ProductionCompleted
import subscribers
import ai
import ai.subscribers

# Wire domain-event subscribers to the in-process event bus (ADR-0001).
subscribers.register(event_bus)
# The AI platform subscribes to the same event stream (ADR-0003).
ai.subscribers.register(event_bus)


Base.metadata.create_all(bind=engine)


def _ensure_user_tenant_column():
    """Idempotent migration: add users.tenant_code to an existing table.
    create_all only creates missing tables, it never alters existing ones."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("users")]
        if "tenant_code" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN tenant_code VARCHAR DEFAULT 'DEFAULT'"))
            print("[MIGRATE] users.tenant_code added")
    except Exception as e:
        print(f"[MIGRATE] tenant_code skipped: {e}")


_ensure_user_tenant_column()
tenancy.ensure_tenant_columns(engine)  # ADR-0002: tenant_code on core tables
tenancy.install_scoping()              # ADR-0002: auto-enforce tenant scoping

# Optional error monitoring — active only when SENTRY_DSN is set in the env.
_SENTRY_DSN = os.environ.get("SENTRY_DSN")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_SENTRY_DSN, traces_sample_rate=0.1, environment=os.environ.get("ENV", "production"))
        print("[sentry] error monitoring enabled")
    except Exception as e:
        print(f"[sentry] init skipped: {e}")

app = FastAPI(title="AMP API")

# Register enterprise inventory routes at import time (remnants, issue slips,
# GRN, cycle count, variance report, CSV import).
enterprise_inventory_routes.register(app)

# Register GMATS tenant-scoped enterprise inventory (4-bucket stock, aliases,
# proforma reservation, tax-invoice deduction, free-spares material issue note).
gmats_inventory_routes.register(app)

# Register the platform layer: per-tenant licensing/feature-flags, white-label
# branding, audit log and health check.
platform_routes.register(app)

# Register the AI Factory Copilot (off until ANTHROPIC_API_KEY is set).
ai_copilot.register(app)

# Register the industrial connectivity adapter framework (OPC UA, Modbus, S7,
# Allen-Bradley, Beckhoff, Omron) — GET /industrial/protocols.
industrial_adapters.register(app)


async def _simulation_loop():
    """Background task: runs factory simulation ticks every 45 seconds."""
    import random
    from factory_simulator import (
        tick_work_order_progress,
        tick_shift_entry,
        tick_quality,
        tick_operator,
        tick_iot,
        tick_inventory,
        tick_production,
        tick_machine_status,
        MACHINES,
    )
    await asyncio.sleep(10)  # let the server fully start first
    while True:
        try:
            db = SessionLocal()
            tick_work_order_progress(db)
            tick_iot(db)
            industrial_adapters.tick_industrial(db)   # poll PLCs -> live signals
            tick_production(db)              # keep OEE trends live
            if random.random() < 0.2:
                tick_machine_status(db)      # occasional status change -> timeline event
            if random.random() < 0.15:
                tick_inventory(db)
            if random.random() < 0.5:
                tick_quality(db)
            if random.random() < 0.4:
                tick_shift_entry(db)
            if random.random() < 0.3:
                tick_operator(db)

            # Randomly vary machine utilization to keep dashboard alive
            machines = db.query(models.Machine).filter(
                models.Machine.status == "Running"
            ).all()
            for m in machines:
                m.utilization = max(40, min(99, m.utilization + random.randint(-5, 5)))
            db.commit()
            db.close()
        except Exception as e:
            print(f"[SIM TICK ERROR] {e}")
        await asyncio.sleep(45)


@app.on_event("startup")
async def startup_event():
    start_mqtt_service()
    asyncio.create_task(_simulation_loop())
    try:
        db = SessionLocal()
        gmats_inventory_routes.seed_gmats(db)
        # Core MES: ensure OEE + timeline have data (production records & machine events).
        from factory_simulator import _production_records, _machine_events
        _production_records(db)
        _machine_events(db)
        # Seed per-tenant config (licensing + branding) for DEFAULT and GMATS.
        platform_routes.seed_tenant_configs(db)
        # Seed one demo PLC per industrial protocol.
        industrial_adapters.seed_industrial(db)
        # Seed a dedicated GMATS client login (Supervisor — full access to GMATS inventory)
        if not db.query(models.User).filter(models.User.username == "gmats").first():
            db.add(models.User(username="gmats", password=hash_password("gmats@2026"), role="Supervisor", tenant_code="GMATS"))
            db.commit()
            print("[SEED] GMATS client login (gmats / gmats@2026)")
        # Seed a GMATS Admin from env (password never hardcoded — set GMATS_ADMIN_PASSWORD in Railway).
        gmats_admin_user = os.environ.get("GMATS_ADMIN_USERNAME", "gmats_admin")
        gmats_admin_pw = os.environ.get("GMATS_ADMIN_PASSWORD")
        if gmats_admin_pw and not db.query(models.User).filter(models.User.username == gmats_admin_user).first():
            db.add(models.User(username=gmats_admin_user, password=hash_password(gmats_admin_pw), role="Admin", tenant_code="GMATS"))
            db.commit()
            print(f"[SEED] GMATS Admin '{gmats_admin_user}' created from GMATS_ADMIN_PASSWORD env")
        # Reconcile client logins to their correct tenant. Users created before the
        # tenant_code column existed were backfilled to DEFAULT by the migration.
        for uname, tcode in CLIENT_TENANTS.items():
            u = db.query(models.User).filter(models.User.username == uname).first()
            if u and (u.tenant_code or "DEFAULT") != tcode:
                u.tenant_code = tcode
                db.commit()
                print(f"[MIGRATE] {uname} tenant_code -> {tcode}")
        db.close()
    except Exception as e:
        print(f"[GMATS SEED ERROR] {e}")


# Locked-down CORS. Production origins come from ALLOWED_ORIGINS (comma-separated);
# the regex keeps Vercel preview deploys working. Add a custom domain by setting
# ALLOWED_ORIGINS in the Railway env.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "https://flow-mes.vercel.app").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://flow-[a-z0-9-]+-ashwinvars-projects\.vercel\.app|http://localhost:3000|http://127\.0\.0\.1:3000",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Bind the caller's tenant (from the JWT) per request so the ORM auto-scopes
# core-table queries (ADR-0002). Pure-ASGI (tenancy.TenantScopeMiddleware) to
# avoid BaseHTTPMiddleware's request-body deadlock and to propagate contextvars.
app.add_middleware(tenancy.TenantScopeMiddleware)

VALID_ROLES = ["Admin", "Supervisor", "Operator"]

# Maps a client login to its tenant/company. Added to the JWT so the frontend
# lands that user on their own company's data. Extend per onboarded client.
CLIENT_TENANTS = {"gmats": "GMATS"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def parse_duration_to_minutes(value: str):
    if not value:
        return 0

    lower = value.lower()
    total = 0

    hour_match = re.search(r"(\d+)\s*h", lower)
    minute_match = re.search(r"(\d+)\s*m", lower)

    if hour_match:
        total += int(hour_match.group(1)) * 60

    if minute_match:
        total += int(minute_match.group(1))

    if not hour_match and not minute_match:
        plain = re.sub(r"\D", "", lower)
        total += int(plain) if plain else 0

    return total


def calculate_oee_from_record(record: models.ProductionRecord):
    availability = record.runtime_minutes / record.planned_minutes if record.planned_minutes else 0
    runtime_seconds = record.runtime_minutes * 60
    performance = (
        (record.ideal_cycle_time_seconds * record.total_count) / runtime_seconds
        if runtime_seconds else 0
    )
    quality = record.good_count / record.total_count if record.total_count else 0

    return {
        "availability": round(availability * 100),
        "performance": round(min(performance, 1) * 100),
        "quality": round(quality * 100),
        "oee": round(availability * min(performance, 1) * quality * 100),
    }


def calculate_fallback_oee(utilization: int):
    return round((utilization / 100) * 0.9 * 0.95 * 100)


def generate_alerts(db: Session):
    machines = db.query(models.Machine).all()
    production_records = (
        db.query(models.ProductionRecord)
        .order_by(models.ProductionRecord.id.desc())
        .limit(50)
        .all()
    )

    dynamic_alerts = []
    seen = set()

    def add_alert(alert_type: str, severity: str, machine_name: str, message: str):
        key = f"{machine_name}:{alert_type}"
        if key in seen:
            return
        seen.add(key)
        dynamic_alerts.append(
            {
                "type": alert_type,
                "severity": severity,
                "machine": machine_name,
                "message": message,
            }
        )

    for machine in machines:
        if machine.status == "Breakdown":
            add_alert("Breakdown", "High", machine.name, f"{machine.name} is currently in breakdown")

        if machine.utilization < 50:
            add_alert("Low Utilization", "Medium", machine.name, f"{machine.name} utilization is below 50%")

    latest_by_machine = {}

    for record in production_records:
        if record.machine_id not in latest_by_machine:
            latest_by_machine[record.machine_id] = record

    for record in latest_by_machine.values():
        oee = calculate_oee_from_record(record)
        machine_name = record.machine.name if record.machine else f"Machine {record.machine_id}"

        if oee["oee"] < 60:
            add_alert("Low OEE", "High", machine_name, f"{machine_name} OEE is below target at {oee['oee']}%")

        if record.rejected_count > 0 and record.total_count:
            reject_rate = (record.rejected_count / record.total_count) * 100

            if reject_rate > 5:
                add_alert("Quality Loss", "Medium", machine_name, f"{machine_name} reject rate is above 5%")

    return dynamic_alerts


@app.get("/")
def root():
    return {"message": "AMP Backend Running"}


@app.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return current_user


@app.post("/register", response_model=schemas.UserResponse)
def register_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    """Bootstrap only: creates the very first Admin when the system has no users.
    Once any user exists, self-registration is disabled — an Admin must add employees."""
    if db.query(models.User).count() > 0:
        raise HTTPException(status_code=403, detail="Self-registration is disabled. Ask your Admin to add you.")

    try:
        new_user = models.User(
            username=user.username,
            password=hash_password(user.password),
            role="Admin",            # the first account is always the Admin
            tenant_code="DEFAULT",
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return new_user
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Database integrity error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Register failed: {str(e)}")


@app.post("/users", response_model=schemas.UserResponse)
def create_employee(
    user: schemas.UserCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Admin adds an employee. New employee inherits the Admin's company (tenant)."""
    if user.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")

    tenant = current_user.get("tenant", "DEFAULT")
    try:
        new_user = models.User(
            username=user.username,
            password=hash_password(user.password),
            role=user.role,
            tenant_code=tenant,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        log_audit(db, current_user.get("sub"), "create_employee", "user", new_user.id, f"{user.username} ({user.role}) in {tenant}")
        return new_user
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Database integrity error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Create employee failed: {str(e)}")


@app.post("/login")
def login(user: schemas.UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()

    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid username")

    if not verify_password(user.password, db_user.password):
        raise HTTPException(status_code=401, detail="Invalid password")

    # Transparently upgrade legacy SHA-256 hashes to bcrypt on successful login.
    if needs_rehash(db_user.password):
        try:
            db_user.password = hash_password(user.password)
            db.commit()
        except Exception:
            db.rollback()

    tenant = getattr(db_user, "tenant_code", None) or CLIENT_TENANTS.get(db_user.username.lower(), "DEFAULT")
    log_audit(db, db_user.username, "login", "user", db_user.id, f"tenant={tenant}")
    token = create_access_token(data={"sub": db_user.username, "role": db_user.role, "tenant": tenant})

    return {
        "access_token": token,
        "token_type": "bearer",
        "role": db_user.role,
        "tenant": tenant,
    }


@app.get("/users", response_model=List[schemas.UserResponse])
def list_users(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = current_user.get("tenant", "DEFAULT")
    q = db.query(models.User)
    if tenant == "DEFAULT":
        q = q.filter((models.User.tenant_code == "DEFAULT") | (models.User.tenant_code.is_(None)))
    else:
        q = q.filter(models.User.tenant_code == tenant)
    return q.order_by(models.User.id.asc()).all()


def _same_tenant_or_403(user, current_user):
    tenant = current_user.get("tenant", "DEFAULT")
    user_tenant = user.tenant_code or "DEFAULT"
    if user_tenant != tenant:
        raise HTTPException(status_code=403, detail="You can only manage users in your own company")


@app.patch("/users/{user_id}/role", response_model=schemas.UserResponse)
def update_user_role(
    user_id: int,
    payload: schemas.UserRoleUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    user.role = payload.role
    db.commit()
    db.refresh(user)

    return user


@app.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    if user.username == current_user.get("sub"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    deleted_name = user.username
    db.delete(user)
    db.commit()
    log_audit(db, current_user.get("sub"), "delete_user", "user", user_id, deleted_name)

    return {"message": "User deleted successfully"}


@app.patch("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Admin resets an employee's password (within their own company)."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    new_password = (payload.get("password") or "").strip()
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    user.password = hash_password(new_password)
    db.commit()
    return {"message": "Password reset successfully"}


@app.get("/machines", response_model=List[schemas.MachineResponse])
def get_machines(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.Machine).order_by(models.Machine.id.asc()).all()


@app.post("/machines", response_model=schemas.MachineResponse)
def create_machine(
    machine: schemas.MachineCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    new_machine = models.Machine(**machine.model_dump())
    db.add(new_machine)
    db.commit()
    db.refresh(new_machine)
    return new_machine


@app.delete("/machines/{machine_id}")
def delete_machine(
    machine_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    db.delete(machine)
    db.commit()
    return {"message": "Machine deleted successfully"}


@app.patch("/machines/{machine_id}/status")
def update_machine_status(
    machine_id: int,
    status: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    old_status = machine.status
    machine.status = status
    db.commit()
    db.refresh(machine)

    if old_status != status:
        event = models.MachineEvent(
            machine_id=machine.id,
            machine_name=machine.name,
            old_status=old_status,
            new_status=status,
            utilization=machine.utilization,
            source="manual",
        )
        db.add(event)
        db.commit()

    return machine


@app.get("/downtime-logs", response_model=List[schemas.DowntimeResponse])
def get_downtime_logs(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(100).all()


@app.post("/downtime-logs", response_model=schemas.DowntimeResponse)
def create_downtime_log(
    downtime: schemas.DowntimeCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == downtime.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    new_log = models.DowntimeLog(**downtime.model_dump())
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return new_log


@app.get("/shifts", response_model=List[schemas.ShiftResponse])
def get_shifts(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(100).all()


@app.post("/shifts", response_model=schemas.ShiftResponse)
def create_shift(
    shift: schemas.ShiftCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    new_shift = models.ShiftData(**shift.model_dump())
    db.add(new_shift)
    db.commit()
    db.refresh(new_shift)
    return new_shift


@app.get("/production-records", response_model=List[schemas.ProductionResponse])
def get_production_records(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()


@app.post("/production-records", response_model=schemas.ProductionResponse)
def create_production_record(
    record: schemas.ProductionCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
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


@app.get("/oee/summary")
def oee_summary(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()
    data = []
    for record in records:
        oee = calculate_oee_from_record(record)
        data.append(
            {
                "id": record.id,
                "machine_id": record.machine_id,
                "machine_name": record.machine.name if record.machine else f"Machine {record.machine_id}",
                "availability": oee["availability"],
                "performance": oee["performance"],
                "quality": oee["quality"],
                "oee": oee["oee"],
                "created_at": record.created_at,
            }
        )
    return data


@app.get("/analytics/summary")
def analytics_summary(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    records = db.query(models.ProductionRecord).all()

    running = len([m for m in machines if m.status == "Running"])
    idle = len([m for m in machines if m.status == "Idle"])
    breakdown = len([m for m in machines if m.status == "Breakdown"])
    maintenance = len([m for m in machines if m.status == "Maintenance"])
    avg_utilization = round(sum(m.utilization for m in machines) / len(machines)) if machines else 0
    total_downtime_minutes = sum(parse_duration_to_minutes(log.duration) for log in logs)

    avg_oee = 0
    avg_availability = 0
    avg_performance = 0
    avg_quality = 0
    if records:
        oee_rows = [calculate_oee_from_record(record) for record in records]
        avg_oee = round(sum(row["oee"] for row in oee_rows) / len(oee_rows))
        avg_availability = round(sum(row["availability"] for row in oee_rows) / len(oee_rows))
        avg_performance = round(sum(row["performance"] for row in oee_rows) / len(oee_rows))
        avg_quality = round(sum(row["quality"] for row in oee_rows) / len(oee_rows))
    elif machines:
        avg_oee = round(sum(calculate_fallback_oee(m.utilization) for m in machines) / len(machines))

    avg_shift_efficiency = (
        round(sum((s.actual_output / s.target_output) * 100 if s.target_output else 0 for s in shifts) / len(shifts))
        if shifts else 0
    )

    reason_counts = {}
    machine_downtime = {}
    for log in logs:
        reason_counts[log.reason] = reason_counts.get(log.reason, 0) + 1
        machine_downtime[log.machine_id] = machine_downtime.get(log.machine_id, 0) + parse_duration_to_minutes(log.duration)

    top_reason = max(reason_counts.items(), key=lambda x: x[1])[0] if reason_counts else "No data"
    top_machine_id = max(machine_downtime.items(), key=lambda x: x[1])[0] if machine_downtime else None
    top_machine_name = "No data"
    if top_machine_id:
        machine = db.query(models.Machine).filter(models.Machine.id == top_machine_id).first()
        if machine:
            top_machine_name = machine.name

    alerts = generate_alerts(db)

    return {
        "machines": len(machines),
        "running": running,
        "idle": idle,
        "breakdown": breakdown,
        "maintenance": maintenance,
        "avg_utilization": avg_utilization,
        "avg_oee": avg_oee,
        "avg_availability": avg_availability,
        "avg_performance": avg_performance,
        "avg_quality": avg_quality,
        "downtime_events": len(logs),
        "total_downtime_minutes": total_downtime_minutes,
        "avg_shift_efficiency": avg_shift_efficiency,
        "top_reason": top_reason,
        "top_machine": top_machine_name,
        "reason_counts": reason_counts,
        "alerts": alerts,
    }


@app.get("/alerts")
def get_alerts(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return generate_alerts(db)


@app.get("/machine-events")
def get_machine_events(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(200).all()


@app.get("/analytics/machine-timeline")
def get_machine_timeline(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    events = db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(200).all()
    return [
        {
            "id": event.id,
            "machine_id": event.machine_id,
            "machine_name": event.machine_name,
            "old_status": event.old_status,
            "new_status": event.new_status,
            "utilization": event.utilization,
            "source": event.source,
            "created_at": event.created_at,
        }
        for event in events
    ]


@app.get("/analytics/machine-state-summary")
def get_machine_state_summary(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    events = db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(300).all()
    summary = {}
    for event in events:
        machine = summary.setdefault(
            event.machine_name,
            {"machine_name": event.machine_name, "Running": 0, "Idle": 0, "Breakdown": 0, "Maintenance": 0, "total_events": 0},
        )
        if event.new_status in machine:
            machine[event.new_status] += 1
        machine["total_events"] += 1
    return list(summary.values())


@app.get("/analytics/oee-trends")
def get_oee_trends(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.asc()).limit(200).all()
    return build_oee_trends(records)


@app.get("/analytics/shift-kpis")
def get_shift_kpis(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(50).all()
    return build_shift_kpis(shifts)


@app.get("/analytics/management")
def get_management_dashboard(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    production_records = db.query(models.ProductionRecord).all()
    return build_management_summary(machines, downtime_logs, shifts, production_records)


@app.get("/alerts/smart")
def get_smart_alerts(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    production_records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()
    downtime_logs = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(100).all()
    return build_smart_alerts(machines, production_records, downtime_logs)


@app.get("/analytics/predictive-maintenance")
def get_predictive_maintenance(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Predictive maintenance now runs through the AI platform (ADR-0003) rather
    # than the engine directly - same rule-based result today, swappable for
    # ML/LLM behind ai.prediction without touching this endpoint.
    return ai.prediction.assess_from_db(db)


@app.get("/work-orders", response_model=List[schemas.WorkOrderResponse])
def get_work_orders(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.WorkOrder).order_by(models.WorkOrder.id.desc()).limit(200).all()


@app.post("/work-orders", response_model=schemas.WorkOrderResponse)
def create_work_order(
    work_order: schemas.WorkOrderCreate,
    db: Session = Depends(get_db),
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

@app.get("/bom")
def get_bom(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    rows = []
    for part_code, bom in PART_BOM.items():
        raw_item = None
        fg_item = None
        if bom["raw"]:
            raw_item = db.query(models.InventoryItem).filter(
                models.InventoryItem.item_code == bom["raw"]
            ).first()
        if bom["fg"]:
            fg_item = db.query(models.InventoryItem).filter(
                models.InventoryItem.item_code == bom["fg"]
            ).first()
        rows.append({
            "part_number": part_code,
            "raw_material_code": bom["raw"] or "—",
            "raw_material_name": raw_item.item_name if raw_item else "—",
            "consume_per_unit": bom["consume_per_unit"],
            "raw_unit": raw_item.unit if raw_item else "—",
            "finished_goods_code": bom["fg"] or "—",
            "finished_goods_name": fg_item.item_name if fg_item else "—",
            "raw_current_stock": raw_item.current_stock if raw_item else None,
            "raw_reorder_level": raw_item.reorder_level if raw_item else None,
        })
    return rows


@app.patch("/work-orders/{work_order_id}", response_model=schemas.WorkOrderResponse)
def update_work_order(
    work_order_id: int,
    payload: schemas.WorkOrderUpdate,
    db: Session = Depends(get_db),
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
                tenant_code=current_user.get("tenant", "DEFAULT"),
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


@app.delete("/work-orders/{work_order_id}")
def delete_work_order(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    db.query(models.ProductionPlan).filter(models.ProductionPlan.work_order_id == work_order_id).delete()
    db.delete(work_order)
    db.commit()
    return {"message": "Work order deleted successfully"}


@app.get("/analytics/work-orders")
def get_work_order_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    work_orders = db.query(models.WorkOrder).all()
    planned = len([wo for wo in work_orders if wo.status == "Planned"])
    running = len([wo for wo in work_orders if wo.status == "Running"])
    completed = len([wo for wo in work_orders if wo.status == "Completed"])
    delayed = len([wo for wo in work_orders if wo.status == "Delayed"])
    total_target = sum(wo.target_quantity for wo in work_orders)
    total_actual = sum(wo.actual_quantity for wo in work_orders)
    achievement = round((total_actual / total_target) * 100) if total_target else 0
    return {
        "total_work_orders": len(work_orders),
        "planned": planned,
        "running": running,
        "completed": completed,
        "delayed": delayed,
        "total_target": total_target,
        "total_actual": total_actual,
        "achievement": achievement,
    }


@app.get("/production-plans", response_model=List[schemas.ProductionPlanResponse])
def get_production_plans(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ProductionPlan).order_by(models.ProductionPlan.id.desc()).limit(200).all()


@app.post("/production-plans", response_model=schemas.ProductionPlanResponse)
def create_production_plan(
    plan: schemas.ProductionPlanCreate,
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Production plan not found")
    db.delete(plan)
    db.commit()
    return {"message": "Production plan deleted successfully"}


@app.get("/analytics/production-plans")
def get_production_plan_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    plans = db.query(models.ProductionPlan).all()
    planned_quantity = sum(plan.planned_quantity for plan in plans)
    actual_quantity = sum(plan.actual_quantity for plan in plans)
    achievement = round((actual_quantity / planned_quantity) * 100) if planned_quantity else 0
    return {
        "total_plans": len(plans),
        "planned_quantity": planned_quantity,
        "actual_quantity": actual_quantity,
        "achievement": achievement,
        "planned": len([plan for plan in plans if plan.status == "Planned"]),
        "running": len([plan for plan in plans if plan.status == "Running"]),
        "completed": len([plan for plan in plans if plan.status == "Completed"]),
        "behind": len([plan for plan in plans if plan.status == "Behind"]),
    }


@app.get("/reports/downtime.csv")
def export_downtime_csv(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    logs = db.query(models.DowntimeLog).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "reason", "duration", "notes", "created_at"])
    for log in logs:
        machine = db.query(models.Machine).filter(models.Machine.id == log.machine_id).first()
        writer.writerow([log.id, log.machine_id, machine.name if machine else "", log.reason, log.duration, log.notes or "", log.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=downtime_report.csv"})


@app.get("/reports/shifts.csv")
def export_shifts_csv(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    shifts = db.query(models.ShiftData).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "shift_name", "target_output", "actual_output", "efficiency_percent", "created_at"])
    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        writer.writerow([shift.id, shift.shift_name, shift.target_output, shift.actual_output, efficiency, shift.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=shift_report.csv"})


@app.get("/reports/oee.csv")
def export_oee_csv(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    records = db.query(models.ProductionRecord).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "availability", "performance", "quality", "oee", "planned_minutes", "runtime_minutes", "total_count", "good_count", "rejected_count", "created_at"])
    for record in records:
        oee = calculate_oee_from_record(record)
        writer.writerow([record.id, record.machine_id, record.machine.name if record.machine else "", oee["availability"], oee["performance"], oee["quality"], oee["oee"], record.planned_minutes, record.runtime_minutes, record.total_count, record.good_count, record.rejected_count, record.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=oee_report.csv"})


@app.get("/reports/daily-summary.txt")
def daily_summary_report(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    summary = analytics_summary(db, current_user)
    report = f"""
AMP Daily Factory Summary
Generated: {datetime.utcnow().isoformat()} UTC

Machines: {summary["machines"]}
Running: {summary["running"]}
Breakdowns: {summary["breakdown"]}
Avg Utilization: {summary["avg_utilization"]}%
Avg OEE: {summary["avg_oee"]}%
Avg Availability: {summary["avg_availability"]}%
Avg Performance: {summary["avg_performance"]}%
Avg Quality: {summary["avg_quality"]}%
Downtime Events: {summary["downtime_events"]}
Total Downtime: {summary["total_downtime_minutes"]} minutes
Shift Efficiency: {summary["avg_shift_efficiency"]}%
Top Downtime Reason: {summary["top_reason"]}
Top Downtime Machine: {summary["top_machine"]}

Alerts:
{chr(10).join([f'- [{a["severity"]}] {a["message"]}' for a in summary["alerts"]]) or "No active alerts"}
"""
    return Response(content=report, media_type="text/plain", headers={"Content-Disposition": "attachment; filename=daily_summary_report.txt"})


@app.get("/reports/intelligence-summary.txt")
def export_intelligence_summary(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    production_records = db.query(models.ProductionRecord).all()
    summary = build_management_summary(machines, downtime_logs, shifts, production_records)
    shift_kpis = build_shift_kpis(shifts)
    alerts = build_smart_alerts(machines, production_records, downtime_logs)
    report = build_daily_summary_text(summary, shift_kpis, alerts)
    return Response(content=report, media_type="text/plain", headers={"Content-Disposition": "attachment; filename=amp_intelligence_report.txt"})





@app.get("/escalations", response_model=List[schemas.EscalationResponse])
def get_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.Escalation)
        .order_by(models.Escalation.id.desc())
        .limit(300)
        .all()
    )


@app.post("/escalations", response_model=schemas.EscalationResponse)
def create_escalation(
    escalation: schemas.EscalationCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    if escalation.machine_id:
        machine = (
            db.query(models.Machine)
            .filter(models.Machine.id == escalation.machine_id)
            .first()
        )
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    new_escalation = models.Escalation(**escalation.model_dump())
    db.add(new_escalation)
    db.commit()
    db.refresh(new_escalation)

    return new_escalation


@app.patch("/escalations/{escalation_id}", response_model=schemas.EscalationResponse)
def update_escalation(
    escalation_id: int,
    payload: schemas.EscalationUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    escalation = (
        db.query(models.Escalation)
        .filter(models.Escalation.id == escalation_id)
        .first()
    )

    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    if payload.status is not None:
        escalation.status = payload.status
        if payload.status == "Resolved" and escalation.resolved_at is None:
            escalation.resolved_at = datetime.utcnow()

    if payload.owner is not None:
        escalation.owner = payload.owner

    if payload.department is not None:
        escalation.department = payload.department

    if payload.resolution_notes is not None:
        escalation.resolution_notes = payload.resolution_notes

    db.commit()
    db.refresh(escalation)

    return escalation


@app.delete("/escalations/{escalation_id}")
def delete_escalation(
    escalation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    escalation = (
        db.query(models.Escalation)
        .filter(models.Escalation.id == escalation_id)
        .first()
    )

    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    db.delete(escalation)
    db.commit()

    return {"message": "Escalation deleted successfully"}


@app.post("/escalations/from-smart-alerts")
def create_escalations_from_smart_alerts(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    alerts = generate_alerts(db)
    created = 0

    for alert in alerts:
        title = f'{alert.get("type", "Alert")} - {alert.get("machine", "Factory")}'

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        machine = (
            db.query(models.Machine)
            .filter(models.Machine.name == alert.get("machine"))
            .first()
        )

        escalation = models.Escalation(
            machine_id=machine.id if machine else None,
            title=title,
            severity=alert.get("severity", "Medium"),
            owner="Unassigned",
            department="Maintenance",
            status="Open",
            source="Smart Alert",
            notes=alert.get("message"),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}


@app.get("/analytics/escalations")
def get_escalation_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = db.query(models.Escalation).all()

    return {
        "total": len(rows),
        "open": len([row for row in rows if row.status == "Open"]),
        "in_progress": len([row for row in rows if row.status == "In Progress"]),
        "resolved": len([row for row in rows if row.status == "Resolved"]),
        "critical": len([row for row in rows if row.severity == "Critical"]),
        "high": len([row for row in rows if row.severity == "High"]),
        "medium": len([row for row in rows if row.severity == "Medium"]),
        "low": len([row for row in rows if row.severity == "Low"]),
    }





@app.get("/inventory/items", response_model=List[schemas.InventoryItemResponse])
def get_inventory_items(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.InventoryItem)
        .order_by(models.InventoryItem.id.desc())
        .limit(500)
        .all()
    )


@app.post("/inventory/items", response_model=schemas.InventoryItemResponse)
def create_inventory_item(
    item: schemas.InventoryItemCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.item_code == item.item_code)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Item code already exists")

    new_item = models.InventoryItem(**item.model_dump())
    db.add(new_item)
    db.commit()
    db.refresh(new_item)

    return new_item


@app.patch("/inventory/items/{item_id}", response_model=schemas.InventoryItemResponse)
def update_inventory_item(
    item_id: int,
    payload: schemas.InventoryItemUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)

    return item


@app.delete("/inventory/items/{item_id}")
def delete_inventory_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    db.delete(item)
    db.commit()

    return {"message": "Inventory item deleted successfully"}


@app.get("/inventory/transactions", response_model=List[schemas.InventoryTransactionResponse])
def get_inventory_transactions(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.InventoryTransaction)
        .order_by(models.InventoryTransaction.id.desc())
        .limit(300)
        .all()
    )


@app.post("/inventory/transactions", response_model=schemas.InventoryTransactionResponse)
def create_inventory_transaction(
    transaction: schemas.InventoryTransactionCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == transaction.item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    quantity = abs(transaction.quantity)

    if transaction.transaction_type == "Issue":
        if item.current_stock < quantity:
            raise HTTPException(status_code=400, detail="Insufficient stock")
        item.current_stock -= quantity

    elif transaction.transaction_type == "Return":
        item.current_stock += quantity

    elif transaction.transaction_type == "Receive":
        item.current_stock += quantity

    elif transaction.transaction_type == "Adjust":
        item.current_stock = quantity

    else:
        raise HTTPException(status_code=400, detail="Invalid transaction type")

    new_transaction = models.InventoryTransaction(
        item_id=transaction.item_id,
        transaction_type=transaction.transaction_type,
        quantity=quantity,
        reference=transaction.reference,
        notes=transaction.notes,
    )

    db.add(new_transaction)
    db.commit()
    db.refresh(new_transaction)

    return new_transaction


@app.get("/analytics/inventory")
def get_inventory_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    items = db.query(models.InventoryItem).all()
    transactions = db.query(models.InventoryTransaction).all()

    low_stock_items = [
        item for item in items
        if item.current_stock <= item.reorder_level
    ]

    total_stock_units = sum(item.current_stock for item in items)

    category_counts = {}
    supplier_counts = {}

    for item in items:
        category_counts[item.category] = category_counts.get(item.category, 0) + item.current_stock
        supplier = item.supplier or "Unknown"
        supplier_counts[supplier] = supplier_counts.get(supplier, 0) + item.current_stock

    return {
        "total_items": len(items),
        "low_stock_items": len(low_stock_items),
        "total_stock_units": total_stock_units,
        "transactions": len(transactions),
        "category_counts": category_counts,
        "supplier_counts": supplier_counts,
    }


@app.post("/inventory/generate-low-stock-escalations")
def generate_low_stock_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    low_stock_items = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.current_stock <= models.InventoryItem.reorder_level)
        .all()
    )

    created = 0

    for item in low_stock_items:
        title = f"Low stock: {item.item_code} - {item.item_name}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="High" if item.current_stock == 0 else "Medium",
            owner="Stores",
            department="Inventory",
            status="Open",
            source="Inventory",
            notes=f"Current stock {item.current_stock} {item.unit}; reorder level {item.reorder_level} {item.unit}",
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}





@app.get("/quality/inspections", response_model=List[schemas.QualityInspectionResponse])
def get_quality_inspections(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.QualityInspection)
        .order_by(models.QualityInspection.id.desc())
        .limit(300)
        .all()
    )


@app.post("/quality/inspections", response_model=schemas.QualityInspectionResponse)
def create_quality_inspection(
    inspection: schemas.QualityInspectionCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    existing = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.inspection_no == inspection.inspection_no)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Inspection number already exists")

    if inspection.machine_id:
        machine = (
            db.query(models.Machine)
            .filter(models.Machine.id == inspection.machine_id)
            .first()
        )
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    if inspection.work_order_id:
        work_order = (
            db.query(models.WorkOrder)
            .filter(models.WorkOrder.id == inspection.work_order_id)
            .first()
        )
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if inspection.production_plan_id:
        production_plan = (
            db.query(models.ProductionPlan)
            .filter(models.ProductionPlan.id == inspection.production_plan_id)
            .first()
        )
        if not production_plan:
            raise HTTPException(status_code=404, detail="Production plan not found")

    if inspection.passed_quantity + inspection.failed_quantity > inspection.inspected_quantity:
        raise HTTPException(
            status_code=400,
            detail="passed_quantity + failed_quantity cannot exceed inspected_quantity",
        )

    new_inspection = models.QualityInspection(**inspection.model_dump())
    db.add(new_inspection)
    db.commit()
    db.refresh(new_inspection)

    return new_inspection


@app.patch("/quality/inspections/{inspection_id}", response_model=schemas.QualityInspectionResponse)
def update_quality_inspection(
    inspection_id: int,
    payload: schemas.QualityInspectionUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    inspection = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.id == inspection_id)
        .first()
    )

    if not inspection:
        raise HTTPException(status_code=404, detail="Quality inspection not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(inspection, key, value)

    if inspection.passed_quantity + inspection.failed_quantity > inspection.inspected_quantity:
        raise HTTPException(
            status_code=400,
            detail="passed_quantity + failed_quantity cannot exceed inspected_quantity",
        )

    db.commit()
    db.refresh(inspection)

    return inspection


@app.delete("/quality/inspections/{inspection_id}")
def delete_quality_inspection(
    inspection_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    inspection = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.id == inspection_id)
        .first()
    )

    if not inspection:
        raise HTTPException(status_code=404, detail="Quality inspection not found")

    db.delete(inspection)
    db.commit()

    return {"message": "Quality inspection deleted successfully"}


@app.get("/analytics/quality")
def get_quality_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    inspections = db.query(models.QualityInspection).all()

    inspected = sum(row.inspected_quantity for row in inspections)
    passed = sum(row.passed_quantity for row in inspections)
    failed = sum(row.failed_quantity for row in inspections)
    rework = sum(row.rework_quantity for row in inspections)
    scrap = sum(row.scrap_quantity for row in inspections)

    pass_rate = round((passed / inspected) * 100) if inspected else 0
    fail_rate = round((failed / inspected) * 100) if inspected else 0

    defect_counts = {}
    machine_failures = {}

    for row in inspections:
        category = row.defect_category or "No Defect"
        defect_counts[category] = defect_counts.get(category, 0) + row.failed_quantity

        if row.machine_id:
            machine_failures[row.machine_id] = machine_failures.get(row.machine_id, 0) + row.failed_quantity

    return {
        "total_inspections": len(inspections),
        "inspected_quantity": inspected,
        "passed_quantity": passed,
        "failed_quantity": failed,
        "rework_quantity": rework,
        "scrap_quantity": scrap,
        "pass_rate": pass_rate,
        "fail_rate": fail_rate,
        "defect_counts": defect_counts,
        "machine_failures": machine_failures,
    }


@app.post("/quality/generate-defect-escalations")
def generate_defect_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    inspections = db.query(models.QualityInspection).all()
    created = 0

    for inspection in inspections:
        if inspection.inspected_quantity <= 0:
            continue

        fail_rate = (inspection.failed_quantity / inspection.inspected_quantity) * 100

        if fail_rate < 10 and inspection.scrap_quantity <= 0:
            continue

        title = f"Quality issue: {inspection.inspection_no}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=inspection.machine_id,
            title=title,
            severity="Critical" if fail_rate >= 20 else "High",
            owner="Quality Lead",
            department="Quality",
            status="Open",
            source="Quality",
            notes=(
                f"Fail rate {round(fail_rate, 1)}%; "
                f"defect category {inspection.defect_category or 'N/A'}; "
                f"scrap {inspection.scrap_quantity}; rework {inspection.rework_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}





@app.get("/analytics/executive-oee")
def get_executive_oee(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    production_records = db.query(models.ProductionRecord).all()
    shifts = db.query(models.ShiftData).all()
    quality_rows = db.query(models.QualityInspection).all()

    machine_map = {machine.id: machine.name for machine in machines}

    production_by_machine = {}
    for record in production_records:
        production_by_machine.setdefault(record.machine_id, []).append(record)

    downtime_by_machine = {}
    reason_counts = {}

    for log in downtime_logs:
        minutes = parse_duration_to_minutes(log.duration)
        downtime_by_machine[log.machine_id] = downtime_by_machine.get(log.machine_id, 0) + minutes
        reason_counts[log.reason] = reason_counts.get(log.reason, 0) + minutes

    quality_by_machine = {}
    for row in quality_rows:
        if not row.machine_id:
            continue
        bucket = quality_by_machine.setdefault(
            row.machine_id,
            {"inspected": 0, "passed": 0, "failed": 0, "scrap": 0, "rework": 0},
        )
        bucket["inspected"] += row.inspected_quantity
        bucket["passed"] += row.passed_quantity
        bucket["failed"] += row.failed_quantity
        bucket["scrap"] += row.scrap_quantity
        bucket["rework"] += row.rework_quantity

    machine_rows = []

    for machine in machines:
        records = production_by_machine.get(machine.id, [])
        planned_minutes = sum(record.planned_minutes for record in records)
        runtime_minutes = sum(record.runtime_minutes for record in records)
        ideal_cycle_total = sum(
            record.ideal_cycle_time_seconds * record.total_count
            for record in records
        )
        total_count = sum(record.total_count for record in records)
        good_count = sum(record.good_count for record in records)
        rejected_count = sum(record.rejected_count for record in records)

        if planned_minutes > 0:
            availability = round((runtime_minutes / planned_minutes) * 100)
        else:
            availability = max(machine.utilization, 0)

        runtime_seconds = runtime_minutes * 60
        if runtime_seconds > 0:
            performance = round(min((ideal_cycle_total / runtime_seconds), 1) * 100)
        else:
            performance = 90 if machine.status == "Running" else 60

        if total_count > 0:
            quality = round((good_count / total_count) * 100)
        else:
            q = quality_by_machine.get(machine.id)
            if q and q["inspected"] > 0:
                quality = round((q["passed"] / q["inspected"]) * 100)
            else:
                quality = 95

        oee = round((availability / 100) * (performance / 100) * (quality / 100) * 100)

        machine_rows.append(
            {
                "machine_id": machine.id,
                "machine_name": machine.name,
                "status": machine.status,
                "availability": availability,
                "performance": performance,
                "quality": quality,
                "oee": oee,
                "downtime_minutes": downtime_by_machine.get(machine.id, 0),
                "total_count": total_count,
                "good_count": good_count,
                "rejected_count": rejected_count,
                "utilization": machine.utilization,
            }
        )

    machine_rows.sort(key=lambda row: row["oee"], reverse=True)

    plant_availability = round(sum(row["availability"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_performance = round(sum(row["performance"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_quality = round(sum(row["quality"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_oee = round(sum(row["oee"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0

    total_target = sum(shift.target_output for shift in shifts)
    total_actual = sum(shift.actual_output for shift in shifts)
    plan_achievement = round((total_actual / total_target) * 100) if total_target else 0

    downtime_pareto = [
        {"reason": reason, "minutes": minutes}
        for reason, minutes in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    shift_rows = []
    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        shift_rows.append(
            {
                "shift_name": shift.shift_name,
                "target_output": shift.target_output,
                "actual_output": shift.actual_output,
                "efficiency": efficiency,
            }
        )

    quality_defects = {}
    for row in quality_rows:
        key = row.defect_category or "No Defect"
        quality_defects[key] = quality_defects.get(key, 0) + row.failed_quantity

    quality_trend = [
        {"defect": defect, "failed_quantity": qty}
        for defect, qty in sorted(quality_defects.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "plant_availability": plant_availability,
        "plant_performance": plant_performance,
        "plant_quality": plant_quality,
        "plant_oee": plant_oee,
        "machine_ranking": machine_rows,
        "downtime_pareto": downtime_pareto,
        "shift_oee": shift_rows,
        "quality_trend": quality_trend,
        "production_target": total_target,
        "production_actual": total_actual,
        "production_achievement": plan_achievement,
        "running_machines": len([machine for machine in machines if machine.status == "Running"]),
        "breakdown_machines": len([machine for machine in machines if machine.status == "Breakdown"]),
    }



@app.get("/factory-layout/nodes", response_model=List[schemas.FactoryLayoutNodeResponse])
def get_factory_layout_nodes(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.FactoryLayoutNode).order_by(models.FactoryLayoutNode.id.asc()).all()


@app.post("/factory-layout/nodes", response_model=schemas.FactoryLayoutNodeResponse)
def create_factory_layout_node(
    node: schemas.FactoryLayoutNodeCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    if node.machine_id:
        machine = db.query(models.Machine).filter(models.Machine.id == node.machine_id).first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    new_node = models.FactoryLayoutNode(**node.model_dump())
    db.add(new_node)
    db.commit()
    db.refresh(new_node)
    return new_node


@app.patch("/factory-layout/nodes/{node_id}", response_model=schemas.FactoryLayoutNodeResponse)
def update_factory_layout_node(
    node_id: int,
    payload: schemas.FactoryLayoutNodeUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    node = db.query(models.FactoryLayoutNode).filter(models.FactoryLayoutNode.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Factory layout node not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("machine_id"):
        machine = db.query(models.Machine).filter(models.Machine.id == data["machine_id"]).first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    for key, value in data.items():
        setattr(node, key, value)

    db.commit()
    db.refresh(node)
    return node


@app.delete("/factory-layout/nodes/{node_id}")
def delete_factory_layout_node(
    node_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    node = db.query(models.FactoryLayoutNode).filter(models.FactoryLayoutNode.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Factory layout node not found")

    db.delete(node)
    db.commit()
    return {"message": "Factory layout node deleted successfully"}


@app.post("/factory-layout/auto-generate")
def auto_generate_factory_layout(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machines = db.query(models.Machine).order_by(models.Machine.id.asc()).all()
    existing_count = db.query(models.FactoryLayoutNode).count()

    if existing_count > 0:
        return {"created": 0, "message": "Layout already exists"}

    created = 0
    x = 40
    y = 50
    col = 0

    for machine in machines:
        node = models.FactoryLayoutNode(
            machine_id=machine.id,
            node_name=machine.name,
            node_type="Machine",
            x_position=x,
            y_position=y,
            width=180,
            height=110,
            zone="Production",
        )
        db.add(node)
        created += 1
        col += 1
        x += 220

        if col >= 4:
            col = 0
            x = 40
            y += 160

    db.commit()
    return {"created": created}


@app.get("/analytics/factory-command-center")
def get_factory_command_center(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    work_orders = db.query(models.WorkOrder).all()
    production_plans = db.query(models.ProductionPlan).all()
    escalations = db.query(models.Escalation).all()
    inventory_items = db.query(models.InventoryItem).all()
    quality_rows = db.query(models.QualityInspection).all()
    nodes = db.query(models.FactoryLayoutNode).all()

    total_downtime = sum(parse_duration_to_minutes(log.duration) for log in downtime_logs)
    running = len([machine for machine in machines if machine.status == "Running"])
    breakdown = len([machine for machine in machines if machine.status == "Breakdown"])
    idle = len([machine for machine in machines if machine.status == "Idle"])
    maintenance = len([machine for machine in machines if machine.status == "Maintenance"])
    active_work_orders = len([row for row in work_orders if row.status in ["Running", "Planned"]])
    behind_plans = len([row for row in production_plans if row.status == "Behind"])
    open_escalations = len([row for row in escalations if row.status != "Resolved"])
    low_stock = len([item for item in inventory_items if item.current_stock <= item.reorder_level])

    inspected = sum(row.inspected_quantity for row in quality_rows)
    failed = sum(row.failed_quantity for row in quality_rows)
    quality_fail_rate = round((failed / inspected) * 100) if inspected else 0

    machine_map = {machine.id: machine for machine in machines}
    zone_summary = {}

    for node in nodes:
        zone = zone_summary.setdefault(
            node.zone,
            {"zone": node.zone, "nodes": 0, "running": 0, "breakdown": 0, "idle": 0, "maintenance": 0},
        )
        zone["nodes"] += 1

        if node.machine_id and node.machine_id in machine_map:
            status = machine_map[node.machine_id].status
            if status == "Running":
                zone["running"] += 1
            elif status == "Breakdown":
                zone["breakdown"] += 1
            elif status == "Idle":
                zone["idle"] += 1
            elif status == "Maintenance":
                zone["maintenance"] += 1

    return {
        "machines": len(machines),
        "running": running,
        "breakdown": breakdown,
        "idle": idle,
        "maintenance": maintenance,
        "total_downtime_minutes": total_downtime,
        "active_work_orders": active_work_orders,
        "behind_plans": behind_plans,
        "open_escalations": open_escalations,
        "low_stock_items": low_stock,
        "quality_fail_rate": quality_fail_rate,
        "zone_summary": list(zone_summary.values()),
    }



@app.get("/customer-orders", response_model=List[schemas.CustomerOrderResponse])
def get_customer_orders(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.CustomerOrder)
        .order_by(models.CustomerOrder.id.desc())
        .limit(500)
        .all()
    )


@app.post("/customer-orders", response_model=schemas.CustomerOrderResponse)
def create_customer_order(
    order: schemas.CustomerOrderCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.order_no == order.order_no)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Order number already exists")

    if order.linked_work_order_id:
        work_order = (
            db.query(models.WorkOrder)
            .filter(models.WorkOrder.id == order.linked_work_order_id)
            .first()
        )
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if order.linked_production_plan_id:
        plan = (
            db.query(models.ProductionPlan)
            .filter(models.ProductionPlan.id == order.linked_production_plan_id)
            .first()
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Production plan not found")

    if order.dispatched_quantity > order.order_quantity:
        raise HTTPException(status_code=400, detail="Dispatched quantity cannot exceed order quantity")

    status = order.status
    if order.dispatched_quantity >= order.order_quantity:
        status = "Dispatched"
    elif order.dispatched_quantity > 0:
        status = "Partial"

    new_order = models.CustomerOrder(**order.model_dump())
    new_order.status = status

    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    return new_order


@app.patch("/customer-orders/{order_id}", response_model=schemas.CustomerOrderResponse)
def update_customer_order(
    order_id: int,
    payload: schemas.CustomerOrderUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    order = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.id == order_id)
        .first()
    )

    if not order:
        raise HTTPException(status_code=404, detail="Customer order not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(order, key, value)

    if order.dispatched_quantity > order.order_quantity:
        raise HTTPException(status_code=400, detail="Dispatched quantity cannot exceed order quantity")

    if order.dispatched_quantity >= order.order_quantity:
        order.status = "Dispatched"
    elif order.dispatched_quantity > 0 and order.status != "Cancelled":
        order.status = "Partial"

    db.commit()
    db.refresh(order)

    return order


@app.delete("/customer-orders/{order_id}")
def delete_customer_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    order = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.id == order_id)
        .first()
    )

    if not order:
        raise HTTPException(status_code=404, detail="Customer order not found")

    db.delete(order)
    db.commit()

    return {"message": "Customer order deleted successfully"}


@app.get("/analytics/customer-orders")
def get_customer_order_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    orders = db.query(models.CustomerOrder).all()

    today = datetime.utcnow().date()

    pending = len([row for row in orders if row.status == "Pending"])
    partial = len([row for row in orders if row.status == "Partial"])
    dispatched = len([row for row in orders if row.status == "Dispatched"])
    cancelled = len([row for row in orders if row.status == "Cancelled"])
    late = len([row for row in orders if row.due_date < today and row.status not in ["Dispatched", "Cancelled"]])

    total_order_qty = sum(row.order_quantity for row in orders)
    total_dispatched_qty = sum(row.dispatched_quantity for row in orders)
    dispatch_rate = round((total_dispatched_qty / total_order_qty) * 100) if total_order_qty else 0

    priority_counts = {}
    customer_counts = {}

    for row in orders:
        priority_counts[row.priority] = priority_counts.get(row.priority, 0) + 1
        customer_counts[row.customer_name] = customer_counts.get(row.customer_name, 0) + row.order_quantity

    return {
        "total_orders": len(orders),
        "pending": pending,
        "partial": partial,
        "dispatched": dispatched,
        "cancelled": cancelled,
        "late": late,
        "total_order_qty": total_order_qty,
        "total_dispatched_qty": total_dispatched_qty,
        "dispatch_rate": dispatch_rate,
        "priority_counts": priority_counts,
        "customer_counts": customer_counts,
    }


@app.post("/customer-orders/generate-late-order-escalations")
def generate_late_order_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()

    late_orders = (
        db.query(models.CustomerOrder)
        .filter(
            models.CustomerOrder.due_date < today,
            models.CustomerOrder.status.notin_(["Dispatched", "Cancelled"]),
        )
        .all()
    )

    created = 0

    for order in late_orders:
        title = f"Late customer order: {order.order_no}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="Critical" if order.priority == "Critical" else "High",
            owner="Planning",
            department="Dispatch",
            status="Open",
            source="Orders",
            notes=(
                f"Customer {order.customer_name}; product {order.product_name}; "
                f"due {order.due_date}; dispatched {order.dispatched_quantity}/{order.order_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}



@app.get("/suppliers", response_model=List[schemas.SupplierResponse])
def get_suppliers(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.Supplier).order_by(models.Supplier.id.desc()).limit(500).all()


@app.post("/suppliers", response_model=schemas.SupplierResponse)
def create_supplier(
    supplier: schemas.SupplierCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.Supplier).filter(models.Supplier.supplier_code == supplier.supplier_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Supplier code already exists")

    new_supplier = models.Supplier(**supplier.model_dump())
    db.add(new_supplier)
    db.commit()
    db.refresh(new_supplier)
    return new_supplier


@app.patch("/suppliers/{supplier_id}", response_model=schemas.SupplierResponse)
def update_supplier(
    supplier_id: int,
    payload: schemas.SupplierUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, key, value)

    db.commit()
    db.refresh(supplier)
    return supplier


@app.delete("/suppliers/{supplier_id}")
def delete_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    db.delete(supplier)
    db.commit()
    return {"message": "Supplier deleted successfully"}


@app.get("/purchase-orders", response_model=List[schemas.PurchaseOrderResponse])
def get_purchase_orders(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id.desc()).limit(500).all()


@app.post("/purchase-orders", response_model=schemas.PurchaseOrderResponse)
def create_purchase_order(
    po: schemas.PurchaseOrderCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.po_no == po.po_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="PO number already exists")

    supplier = db.query(models.Supplier).filter(models.Supplier.id == po.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    if po.item_id:
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == po.item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Inventory item not found")

    if po.received_quantity > po.order_quantity:
        raise HTTPException(status_code=400, detail="Received quantity cannot exceed order quantity")

    status = po.status
    if po.received_quantity >= po.order_quantity:
        status = "Received"
    elif po.received_quantity > 0:
        status = "Partial"

    new_po = models.PurchaseOrder(**po.model_dump())
    new_po.status = status

    db.add(new_po)
    db.commit()
    db.refresh(new_po)
    return new_po


@app.patch("/purchase-orders/{po_id}", response_model=schemas.PurchaseOrderResponse)
def update_purchase_order(
    po_id: int,
    payload: schemas.PurchaseOrderUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    old_received = po.received_quantity

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(po, key, value)

    if po.received_quantity > po.order_quantity:
        raise HTTPException(status_code=400, detail="Received quantity cannot exceed order quantity")

    if po.received_quantity >= po.order_quantity:
        po.status = "Received"
    elif po.received_quantity > 0 and po.status != "Cancelled":
        po.status = "Partial"

    received_delta = max(po.received_quantity - old_received, 0)

    if received_delta > 0 and po.item_id:
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == po.item_id).first()
        if item:
            item.current_stock += received_delta

            transaction = models.InventoryTransaction(
                item_id=item.id,
                transaction_type="Receive",
                quantity=received_delta,
                reference=po.po_no,
                notes="Auto stock receipt from purchase order",
            )
            db.add(transaction)

    db.commit()
    db.refresh(po)
    return po


@app.delete("/purchase-orders/{po_id}")
def delete_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    db.delete(po)
    db.commit()
    return {"message": "Purchase order deleted successfully"}


@app.get("/analytics/purchasing")
def get_purchasing_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    suppliers = db.query(models.Supplier).all()
    pos = db.query(models.PurchaseOrder).all()
    today = datetime.utcnow().date()

    open_count = len([row for row in pos if row.status == "Open"])
    partial = len([row for row in pos if row.status == "Partial"])
    received = len([row for row in pos if row.status == "Received"])
    cancelled = len([row for row in pos if row.status == "Cancelled"])
    overdue = len([row for row in pos if row.expected_delivery_date < today and row.status not in ["Received", "Cancelled"]])

    ordered_qty = sum(row.order_quantity for row in pos)
    received_qty = sum(row.received_quantity for row in pos)
    receipt_rate = round((received_qty / ordered_qty) * 100) if ordered_qty else 0

    supplier_pending = {}
    for row in pos:
        supplier = db.query(models.Supplier).filter(models.Supplier.id == row.supplier_id).first()
        name = supplier.supplier_name if supplier else f"Supplier {row.supplier_id}"
        pending = max(row.order_quantity - row.received_quantity, 0)
        supplier_pending[name] = supplier_pending.get(name, 0) + pending

    return {
        "suppliers": len(suppliers),
        "purchase_orders": len(pos),
        "open": open_count,
        "partial": partial,
        "received": received,
        "cancelled": cancelled,
        "overdue": overdue,
        "ordered_qty": ordered_qty,
        "received_qty": received_qty,
        "receipt_rate": receipt_rate,
        "supplier_pending": supplier_pending,
    }


@app.post("/purchase-orders/generate-overdue-escalations")
def generate_overdue_po_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()

    overdue_pos = (
        db.query(models.PurchaseOrder)
        .filter(
            models.PurchaseOrder.expected_delivery_date < today,
            models.PurchaseOrder.status.notin_(["Received", "Cancelled"]),
        )
        .all()
    )

    created = 0

    for po in overdue_pos:
        title = f"Overdue purchase order: {po.po_no}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        supplier = db.query(models.Supplier).filter(models.Supplier.id == po.supplier_id).first()
        supplier_name = supplier.supplier_name if supplier else f"Supplier {po.supplier_id}"

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="High",
            owner="Purchasing",
            department="Supply Chain",
            status="Open",
            source="Purchasing",
            notes=(
                f"Supplier {supplier_name}; item {po.item_name}; "
                f"expected {po.expected_delivery_date}; received {po.received_quantity}/{po.order_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}



@app.get("/documents", response_model=List[schemas.ComplianceDocumentResponse])
def get_documents(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.ComplianceDocument).order_by(models.ComplianceDocument.id.desc()).limit(500).all()


@app.post("/documents", response_model=schemas.ComplianceDocumentResponse)
def create_document(
    document: schemas.ComplianceDocumentCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.document_no == document.document_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Document number already exists")

    new_document = models.ComplianceDocument(**document.model_dump())
    db.add(new_document)
    db.commit()
    db.refresh(new_document)
    return new_document


@app.patch("/documents/{document_id}", response_model=schemas.ComplianceDocumentResponse)
def update_document(
    document_id: int,
    payload: schemas.ComplianceDocumentUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    document = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(document, key, value)

    db.commit()
    db.refresh(document)
    return document


@app.delete("/documents/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    document = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    db.delete(document)
    db.commit()
    return {"message": "Document deleted successfully"}


@app.get("/analytics/documents")
def get_document_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    documents = db.query(models.ComplianceDocument).all()
    today = datetime.utcnow().date()

    draft = len([row for row in documents if row.approval_status == "Draft"])
    approved = len([row for row in documents if row.approval_status == "Approved"])
    under_review = len([row for row in documents if row.approval_status == "Under Review"])
    obsolete = len([row for row in documents if row.approval_status == "Obsolete"])
    review_due = len([row for row in documents if row.review_due_date < today and row.approval_status != "Obsolete"])

    type_counts = {}
    department_counts = {}

    for row in documents:
        type_counts[row.document_type] = type_counts.get(row.document_type, 0) + 1
        department_counts[row.department] = department_counts.get(row.department, 0) + 1

    return {
        "total_documents": len(documents),
        "draft": draft,
        "approved": approved,
        "under_review": under_review,
        "obsolete": obsolete,
        "review_due": review_due,
        "type_counts": type_counts,
        "department_counts": department_counts,
    }


@app.post("/documents/generate-review-escalations")
def generate_document_review_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()
    documents = (
        db.query(models.ComplianceDocument)
        .filter(
            models.ComplianceDocument.review_due_date < today,
            models.ComplianceDocument.approval_status != "Obsolete",
        )
        .all()
    )

    created = 0

    for document in documents:
        title = f"Document review overdue: {document.document_no}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="Medium",
            owner=document.owner,
            department=document.department,
            status="Open",
            source="Compliance",
            notes=f"{document.title} review was due on {document.review_due_date}",
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}


@app.get("/maintenance/tasks", response_model=List[schemas.MaintenanceTaskResponse])
def get_maintenance_tasks(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.MaintenanceTask).order_by(models.MaintenanceTask.id.desc()).limit(500).all()


@app.post("/maintenance/tasks", response_model=schemas.MaintenanceTaskResponse)
def create_maintenance_task(
    task: schemas.MaintenanceTaskCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.task_no == task.task_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Task number already exists")

    machine = db.query(models.Machine).filter(models.Machine.id == task.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    new_task = models.MaintenanceTask(**task.model_dump())
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    return new_task


@app.patch("/maintenance/tasks/{task_id}", response_model=schemas.MaintenanceTaskResponse)
def update_maintenance_task(
    task_id: int,
    payload: schemas.MaintenanceTaskUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    task = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Maintenance task not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, key, value)

    if task.status == "Completed" and task.completed_date is None:
        task.completed_date = datetime.utcnow().date()

    db.commit()
    db.refresh(task)
    return task


@app.delete("/maintenance/tasks/{task_id}")
def delete_maintenance_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    task = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Maintenance task not found")

    db.delete(task)
    db.commit()
    return {"message": "Maintenance task deleted successfully"}


@app.get("/analytics/maintenance")
def get_maintenance_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    tasks = db.query(models.MaintenanceTask).all()
    today = datetime.utcnow().date()

    open_count = len([row for row in tasks if row.status == "Open"])
    in_progress = len([row for row in tasks if row.status == "In Progress"])
    completed = len([row for row in tasks if row.status == "Completed"])
    overdue = len([row for row in tasks if row.planned_date < today and row.status != "Completed"])
    preventive = len([row for row in tasks if row.task_type == "Preventive"])
    breakdown = len([row for row in tasks if row.task_type == "Breakdown"])

    total_downtime = sum(row.downtime_minutes for row in tasks)
    avg_repair = round(total_downtime / completed) if completed else 0

    machine_counts = {}
    for row in tasks:
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        name = machine.name if machine else f"Machine {row.machine_id}"
        machine_counts[name] = machine_counts.get(name, 0) + 1

    return {
        "total_tasks": len(tasks),
        "open": open_count,
        "in_progress": in_progress,
        "completed": completed,
        "overdue": overdue,
        "preventive": preventive,
        "breakdown": breakdown,
        "total_downtime_minutes": total_downtime,
        "avg_repair_minutes": avg_repair,
        "machine_counts": machine_counts,
    }


@app.post("/maintenance/generate-overdue-escalations")
def generate_maintenance_overdue_escalations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()
    tasks = (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.planned_date < today,
            models.MaintenanceTask.status != "Completed",
        )
        .all()
    )

    created = 0

    for task in tasks:
        title = f"Maintenance overdue: {task.task_no}"
        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=task.machine_id,
            title=title,
            severity="High" if task.priority in ["Critical", "High"] else "Medium",
            owner=task.assigned_to,
            department="Maintenance",
            status="Open",
            source="Maintenance",
            notes=f"{task.task_type} task was planned for {task.planned_date}",
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}


@app.get("/production-schedules", response_model=List[schemas.ProductionScheduleResponse])
def get_production_schedules(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id.desc()).limit(500).all()


@app.post("/production-schedules", response_model=schemas.ProductionScheduleResponse)
def create_production_schedule(
    schedule: schemas.ProductionScheduleCreate,
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    schedule = db.query(models.ProductionSchedule).filter(models.ProductionSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Production schedule not found")

    db.delete(schedule)
    db.commit()
    return {"message": "Production schedule deleted successfully"}


@app.get("/analytics/production-schedules")
def get_production_schedule_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    schedules = db.query(models.ProductionSchedule).all()

    scheduled = len([row for row in schedules if row.status == "Scheduled"])
    running = len([row for row in schedules if row.status == "Running"])
    completed = len([row for row in schedules if row.status == "Completed"])
    delayed = len([row for row in schedules if row.status == "Delayed"])

    total_quantity = sum(row.planned_quantity for row in schedules)
    total_minutes = sum(row.estimated_minutes for row in schedules)

    machine_load = {}
    shift_load = {}

    for row in schedules:
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        machine_name = machine.name if machine else f"Machine {row.machine_id}"
        machine_load[machine_name] = machine_load.get(machine_name, 0) + row.estimated_minutes
        shift_load[row.shift_name] = shift_load.get(row.shift_name, 0) + row.planned_quantity

    bottlenecks = [
        {"machine": name, "load_minutes": minutes}
        for name, minutes in sorted(machine_load.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "total_schedules": len(schedules),
        "scheduled": scheduled,
        "running": running,
        "completed": completed,
        "delayed": delayed,
        "total_quantity": total_quantity,
        "total_minutes": total_minutes,
        "machine_load": machine_load,
        "shift_load": shift_load,
        "bottlenecks": bottlenecks,
    }



@app.get("/iot/telemetry", response_model=List[schemas.IoTTelemetryResponse])
def get_iot_telemetry(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.IoTTelemetry).order_by(models.IoTTelemetry.id.desc()).limit(500).all()


@app.post("/iot/telemetry", response_model=schemas.IoTTelemetryResponse)
def create_iot_telemetry(telemetry: schemas.IoTTelemetryCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machine = db.query(models.Machine).filter(models.Machine.id == telemetry.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    row = models.IoTTelemetry(**telemetry.model_dump())
    db.add(row)

    signal = telemetry.signal_name.lower()
    if signal in ["utilization", "load", "efficiency"]:
        machine.utilization = telemetry.numeric_value

    if signal in ["status", "machine_status"]:
        old_status = machine.status
        machine.status = telemetry.signal_value
        if old_status != machine.status:
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


@app.get("/analytics/iot-command")
def get_iot_command_center(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    telemetry = db.query(models.IoTTelemetry).order_by(models.IoTTelemetry.id.desc()).limit(300).all()

    latest = {}
    for row in telemetry:
        key = f"{row.machine_id}:{row.signal_name}"
        if key not in latest:
            latest[key] = row

    latest_rows = []
    for row in latest.values():
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        latest_rows.append({
            "machine_id": row.machine_id,
            "machine_name": machine.name if machine else f"Machine {row.machine_id}",
            "signal_name": row.signal_name,
            "signal_value": row.signal_value,
            "numeric_value": row.numeric_value,
            "unit": row.unit,
            "source": row.source,
            "created_at": row.created_at,
        })

    return {
        "machines": len(machines),
        "signals": len(telemetry),
        "live_machines": len(set([row.machine_id for row in telemetry])),
        "latest_signals": latest_rows,
    }


@app.get("/ai/recommendations", response_model=List[schemas.AIRecommendationResponse])
def get_ai_recommendations(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.AIRecommendation).order_by(models.AIRecommendation.id.desc()).limit(300).all()


@app.patch("/ai/recommendations/{recommendation_id}", response_model=schemas.AIRecommendationResponse)
def update_ai_recommendation(recommendation_id: int, payload: schemas.AIRecommendationUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.AIRecommendation).filter(models.AIRecommendation.id == recommendation_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="AI recommendation not found")
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    db.refresh(row)
    return row


@app.post("/ai/generate-recommendations")
def generate_ai_recommendations(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    inventory_items = db.query(models.InventoryItem).all()
    production_plans = db.query(models.ProductionPlan).all()
    quality_rows = db.query(models.QualityInspection).all()
    created = 0

    def add_rec(kind, severity, title, message, machine_id=None, confidence=78):
        nonlocal created
        existing = db.query(models.AIRecommendation).filter(models.AIRecommendation.title == title, models.AIRecommendation.status != "Closed").first()
        if existing:
            return
        db.add(models.AIRecommendation(
            recommendation_type=kind,
            severity=severity,
            title=title,
            message=message,
            related_machine_id=machine_id,
            confidence=confidence,
            status="Open",
        ))
        created += 1

    for machine in machines:
        machine_downtime = [log for log in downtime_logs if log.machine_id == machine.id]
        downtime_minutes = sum(parse_duration_to_minutes(log.duration) for log in machine_downtime)

        if machine.status == "Breakdown" or downtime_minutes > 120:
            add_rec("Predictive Maintenance", "High", f"Maintenance risk detected on {machine.name}", f"{machine.name} has {downtime_minutes} minutes downtime or breakdown state. Schedule inspection.", machine.id, 86)

        if machine.utilization < 45:
            add_rec("Utilization Optimization", "Medium", f"Low utilization on {machine.name}", f"{machine.name} utilization is {machine.utilization}%. Rebalance schedule.", machine.id, 74)

    for item in inventory_items:
        if item.current_stock <= item.reorder_level:
            add_rec("Inventory Forecast", "High" if item.current_stock == 0 else "Medium", f"Inventory replenishment recommended for {item.item_code}", f"{item.item_name} is at {item.current_stock} {item.unit}; reorder level is {item.reorder_level}.", None, 82)

    for plan in production_plans:
        if plan.status == "Behind":
            add_rec("Production Delay Prediction", "High", f"Delay risk on plan {plan.plan_no}", f"Plan {plan.plan_no} is behind schedule. Review capacity/materials.", plan.machine_id, 80)

    inspected = sum(row.inspected_quantity for row in quality_rows)
    failed = sum(row.failed_quantity for row in quality_rows)
    fail_rate = round((failed / inspected) * 100) if inspected else 0
    if fail_rate >= 10:
        add_rec("Quality Prediction", "High", "Quality failure trend detected", f"Current fail rate is {fail_rate}%. Trigger root-cause analysis.", None, 84)

    db.commit()
    return {"created": created}


@app.get("/analytics/ai-insights")
def get_ai_insights(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.AIRecommendation).all()
    return {
        "total": len(rows),
        "open": len([row for row in rows if row.status == "Open"]),
        "acknowledged": len([row for row in rows if row.status == "Acknowledged"]),
        "closed": len([row for row in rows if row.status == "Closed"]),
        "critical": len([row for row in rows if row.severity == "Critical"]),
        "high": len([row for row in rows if row.severity == "High"]),
        "medium": len([row for row in rows if row.severity == "Medium"]),
        "low": len([row for row in rows if row.severity == "Low"]),
    }



@app.get("/saas/tenants", response_model=List[schemas.CompanyTenantResponse])
def get_company_tenants(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.CompanyTenant).order_by(models.CompanyTenant.id.desc()).limit(300).all()


@app.post("/saas/tenants", response_model=schemas.CompanyTenantResponse)
def create_company_tenant(tenant: schemas.CompanyTenantCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    existing = db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == tenant.company_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Company code already exists")
    row = models.CompanyTenant(**tenant.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.patch("/saas/tenants/{tenant_id}", response_model=schemas.CompanyTenantResponse)
def update_company_tenant(tenant_id: int, payload: schemas.CompanyTenantUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.CompanyTenant).filter(models.CompanyTenant.id == tenant_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/saas/tenants/{tenant_id}")
def delete_company_tenant(tenant_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.CompanyTenant).filter(models.CompanyTenant.id == tenant_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    db.delete(row)
    db.commit()
    return {"message": "Tenant deleted successfully"}


@app.get("/analytics/saas")
def get_saas_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.CompanyTenant).all()
    return {
        "total_tenants": len(rows),
        "trial": len([r for r in rows if r.subscription_status == "Trial"]),
        "active": len([r for r in rows if r.subscription_status == "Active"]),
        "past_due": len([r for r in rows if r.subscription_status == "Past Due"]),
        "cancelled": len([r for r in rows if r.subscription_status == "Cancelled"]),
        "monthly_recurring_revenue": sum(r.monthly_fee for r in rows if r.subscription_status in ["Trial", "Active"]),
        "total_seats": sum(r.seats for r in rows),
    }


@app.get("/cost-records", response_model=List[schemas.CostRecordResponse])
def get_cost_records(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.CostRecord).order_by(models.CostRecord.id.desc()).limit(500).all()


@app.post("/cost-records", response_model=schemas.CostRecordResponse)
def create_cost_record(cost: schemas.CostRecordCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.CostRecord).filter(models.CostRecord.cost_no == cost.cost_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Cost number already exists")
    row = models.CostRecord(**cost.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.patch("/cost-records/{cost_id}", response_model=schemas.CostRecordResponse)
def update_cost_record(cost_id: int, payload: schemas.CostRecordUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = db.query(models.CostRecord).filter(models.CostRecord.id == cost_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cost record not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/cost-records/{cost_id}")
def delete_cost_record(cost_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.CostRecord).filter(models.CostRecord.id == cost_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cost record not found")
    db.delete(row)
    db.commit()
    return {"message": "Cost record deleted successfully"}


@app.get("/analytics/costing")
def get_costing_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    costs = db.query(models.CostRecord).all()
    pos = db.query(models.PurchaseOrder).all()
    production = db.query(models.ProductionRecord).all()

    material_spend = sum(po.received_quantity for po in pos)
    manual_cost = sum(row.amount for row in costs)
    production_units = sum(row.good_count for row in production)

    by_type = {}
    by_department = {}
    for row in costs:
        by_type[row.cost_type] = by_type.get(row.cost_type, 0) + row.amount
        department = row.department or "Unassigned"
        by_department[department] = by_department.get(department, 0) + row.amount

    cost_per_good_unit = round(manual_cost / production_units) if production_units else 0

    return {
        "total_cost_records": len(costs),
        "manual_cost_total": manual_cost,
        "production_units": production_units,
        "cost_per_good_unit": cost_per_good_unit,
        "supplier_receipt_units": material_spend,
        "by_type": by_type,
        "by_department": by_department,
    }


@app.get("/operator/executions", response_model=List[schemas.OperatorJobExecutionResponse])
def get_operator_executions(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.OperatorJobExecution).order_by(models.OperatorJobExecution.id.desc()).limit(500).all()


@app.post("/operator/executions", response_model=schemas.OperatorJobExecutionResponse)
def create_operator_execution(execution: schemas.OperatorJobExecutionCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    existing = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.execution_no == execution.execution_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Execution number already exists")

    machine = db.query(models.Machine).filter(models.Machine.id == execution.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    row = models.OperatorJobExecution(**execution.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.patch("/operator/executions/{execution_id}", response_model=schemas.OperatorJobExecutionResponse)
def update_operator_execution(execution_id: int, payload: schemas.OperatorJobExecutionUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Operator execution not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    if row.job_status == "Completed" and row.completed_at is None:
        row.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


@app.delete("/operator/executions/{execution_id}")
def delete_operator_execution(execution_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    row = db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.id == execution_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Operator execution not found")
    db.delete(row)
    db.commit()
    return {"message": "Operator execution deleted successfully"}


@app.get("/analytics/operator-terminal")
def get_operator_terminal_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.OperatorJobExecution).all()
    good = sum(row.good_count for row in rows)
    rejected = sum(row.rejected_count for row in rows)
    total = good + rejected
    quality_rate = round((good / total) * 100) if total else 0

    return {
        "total_jobs": len(rows),
        "started": len([r for r in rows if r.job_status == "Started"]),
        "paused": len([r for r in rows if r.job_status == "Paused"]),
        "completed": len([r for r in rows if r.job_status == "Completed"]),
        "good_count": good,
        "rejected_count": rejected,
        "quality_rate": quality_rate,
    }



@app.get("/audit-logs", response_model=List[schemas.AuditLogResponse])
def get_audit_logs(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(500).all()


@app.post("/audit-logs", response_model=schemas.AuditLogResponse)
def create_audit_log(payload: schemas.AuditLogCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = models.AuditLog(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/notifications", response_model=List[schemas.NotificationResponse])
def get_notifications(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.Notification).order_by(models.Notification.id.desc()).limit(500).all()


@app.post("/notifications", response_model=schemas.NotificationResponse)
def create_notification(payload: schemas.NotificationCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = models.Notification(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.patch("/notifications/{notification_id}", response_model=schemas.NotificationResponse)
def update_notification(notification_id: int, payload: schemas.NotificationUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.Notification).filter(models.Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    db.refresh(row)
    return row


@app.post("/notifications/generate-system-notifications")
def generate_system_notifications(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    created = 0

    def add_notification(kind, severity, title, message):
        nonlocal created
        existing = db.query(models.Notification).filter(models.Notification.title == title, models.Notification.status != "Read").first()
        if existing:
            return
        db.add(models.Notification(
            notification_type=kind,
            severity=severity,
            title=title,
            message=message,
            status="Unread",
        ))
        created += 1

    breakdowns = db.query(models.Machine).filter(models.Machine.status == "Breakdown").all()
    for machine in breakdowns:
        add_notification("Machine", "Critical", f"Machine breakdown: {machine.name}", f"{machine.name} is currently in Breakdown state.")

    open_escalations = db.query(models.Escalation).filter(models.Escalation.status != "Resolved").all()
    if len(open_escalations) > 0:
        add_notification("Escalation", "Warning", "Open escalations pending", f"{len(open_escalations)} escalation(s) still require action.")

    low_stock = db.query(models.InventoryItem).filter(models.InventoryItem.current_stock <= models.InventoryItem.reorder_level).all()
    if len(low_stock) > 0:
        add_notification("Inventory", "Warning", "Low stock items detected", f"{len(low_stock)} item(s) are below reorder level.")

    db.commit()
    return {"created": created}


@app.get("/reports", response_model=List[schemas.ReportRequestResponse])
def get_reports(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ReportRequest).order_by(models.ReportRequest.id.desc()).limit(300).all()


@app.post("/reports", response_model=schemas.ReportRequestResponse)
def create_report(payload: schemas.ReportRequestCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.ReportRequest).filter(models.ReportRequest.report_no == payload.report_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Report number already exists")
    row = models.ReportRequest(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/analytics/system-health")
def get_system_health(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    users = db.query(models.User).all()
    alerts = db.query(models.Alert).all()
    escalations = db.query(models.Escalation).all()
    notifications = db.query(models.Notification).all()
    audit_logs = db.query(models.AuditLog).all()

    return {
        "api_status": "Healthy",
        "database_status": "Connected",
        "machines": len(machines),
        "users": len(users),
        "alerts": len(alerts),
        "open_escalations": len([row for row in escalations if row.status != "Resolved"]),
        "unread_notifications": len([row for row in notifications if row.status == "Unread"]),
        "audit_logs": len(audit_logs),
        "modules_enabled": [
            "MES",
            "OEE",
            "Digital Twin",
            "Quality",
            "Inventory",
            "Purchasing",
            "Orders",
            "CMMS",
            "Scheduling",
            "IoT",
            "AI",
            "SaaS",
            "Costing",
            "Operator Terminal",
            "Compliance",
        ],
    }


@app.get("/analytics/final-executive-summary")
def get_final_executive_summary(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    work_orders = db.query(models.WorkOrder).all()
    production_plans = db.query(models.ProductionPlan).all()
    quality = db.query(models.QualityInspection).all()
    inventory = db.query(models.InventoryItem).all()
    orders = db.query(models.CustomerOrder).all()
    purchase_orders = db.query(models.PurchaseOrder).all()
    cost_records = db.query(models.CostRecord).all()

    inspected = sum(row.inspected_quantity for row in quality)
    passed = sum(row.passed_quantity for row in quality)
    quality_rate = round((passed / inspected) * 100) if inspected else 0

    order_qty = sum(row.order_quantity for row in orders)
    dispatched_qty = sum(row.dispatched_quantity for row in orders)
    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else 0

    return {
        "machine_count": len(machines),
        "running_machines": len([m for m in machines if m.status == "Running"]),
        "work_orders": len(work_orders),
        "production_plans": len(production_plans),
        "quality_rate": quality_rate,
        "low_stock_items": len([item for item in inventory if item.current_stock <= item.reorder_level]),
        "customer_orders": len(orders),
        "dispatch_rate": dispatch_rate,
        "purchase_orders": len(purchase_orders),
        "total_cost": sum(row.amount for row in cost_records),
    }

@app.get("/industrial/devices", response_model=List[schemas.IndustrialDeviceResponse])
def get_industrial_devices(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.IndustrialDevice).order_by(models.IndustrialDevice.id.desc()).limit(300).all()


@app.post("/industrial/devices", response_model=schemas.IndustrialDeviceResponse)
def create_industrial_device(device: schemas.IndustrialDeviceCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.device_code == device.device_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device code already exists")
    row = models.IndustrialDevice(**device.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.patch("/industrial/devices/{device_id}", response_model=schemas.IndustrialDeviceResponse)
def update_industrial_device(device_id: int, payload: schemas.IndustrialDeviceUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Industrial device not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@app.get("/industrial/signals", response_model=List[schemas.IndustrialSignalResponse])
def get_industrial_signals(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.desc()).limit(500).all()


@app.post("/industrial/signals", response_model=schemas.IndustrialSignalResponse)
def create_industrial_signal(signal: schemas.IndustrialSignalCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
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
                old_status = machine.status
                machine.status = signal.signal_value
                if old_status != machine.status:
                    db.add(models.MachineEvent(
                        machine_id=machine.id,
                        machine_name=machine.name,
                        old_status=old_status,
                        new_status=machine.status,
                        utilization=machine.utilization,
                        source="industrial_gateway",
                    ))
            if field in ["utilization", "load", "efficiency"]:
                machine.utilization = signal.numeric_value
            if field == "downtime":
                machine.downtime = signal.signal_value

    db.commit()
    db.refresh(row)
    return row


@app.get("/industrial/mappings", response_model=List[schemas.PlcSignalMappingResponse])
def get_plc_signal_mappings(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.PlcSignalMapping).order_by(models.PlcSignalMapping.id.desc()).limit(300).all()


@app.post("/industrial/mappings", response_model=schemas.PlcSignalMappingResponse)
def create_plc_signal_mapping(mapping: schemas.PlcSignalMappingCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.PlcSignalMapping).filter(models.PlcSignalMapping.mapping_code == mapping.mapping_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Mapping code already exists")
    row = models.PlcSignalMapping(**mapping.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/analytics/industrial-gateway")
def get_industrial_gateway_analytics(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    devices = db.query(models.IndustrialDevice).all()
    signals = db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.desc()).limit(500).all()
    mappings = db.query(models.PlcSignalMapping).all()

    latest = []
    seen = set()
    for signal in signals:
        key = f"{signal.device_id}:{signal.signal_name}"
        if key in seen:
            continue
        seen.add(key)
        device = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.id == signal.device_id).first()
        machine = db.query(models.Machine).filter(models.Machine.id == signal.machine_id).first() if signal.machine_id else None
        latest.append({
            "device_id": signal.device_id,
            "device_name": device.device_name if device else f"Device {signal.device_id}",
            "machine_name": machine.name if machine else "-",
            "signal_name": signal.signal_name,
            "signal_value": signal.signal_value,
            "numeric_value": signal.numeric_value,
            "unit": signal.unit,
            "quality": signal.quality,
            "source_protocol": signal.source_protocol,
            "created_at": signal.created_at,
        })

    return {
        "devices": len(devices),
        "online_devices": len([d for d in devices if d.status == "Online"]),
        "offline_devices": len([d for d in devices if d.status == "Offline"]),
        "signals": len(signals),
        "mappings": len(mappings),
        "enabled_mappings": len([m for m in mappings if m.enabled == "Yes"]),
        "latest_signals": latest[:30],
    }



@app.websocket("/ws/live")
async def websocket_live_dashboard(websocket: WebSocket):
    # Authenticate the live feed by the JWT passed as ?token= (browsers can't set
    # WS auth headers). The connection then only receives its own tenant's updates.
    tenant = tenancy.tenant_from_token(websocket.query_params.get("token"))
    await manager.connect(websocket, tenant)
    try:
        await websocket.send_json({"event": "connected", "message": "AMP live WebSocket connected"})
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"event": "heartbeat", "message": "alive"})
            except Exception:
                break
    except WebSocketDisconnect:
        print("WebSocket client disconnected")
    except ConnectionResetError:
        print("WebSocket forcibly closed by client")
    except Exception as e:
        print("WebSocket error:", repr(e))
    finally:
        manager.disconnect(websocket)
