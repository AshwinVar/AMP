import asyncio
import csv
import secrets
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
import onboard_tenant
import offboard_tenant
import plan_gate


# Request tenant resolution lives in tenancy.py (so route modules can import it
# without depending on main). Kept as `_tenant` here for the many call sites.
_tenant = tenancy.request_tenant

import enterprise_inventory_routes
import gmats_inventory_routes
import platform_routes
from platform_routes import log_audit
import read_model_routes
import agent_routes
import saas_routes
import costing_routes
import machines_routes
import orders_routes
import factory_ops_routes
import work_orders_routes
import inventory_routes
import quality_routes
import production_planning_routes
import industrial_iot_routes
import operator_routes
import users_routes
import reports_routes
import industrial_adapters
from bom import PART_BOM
from events import event_bus, ProductionCompleted, DowntimeStarted, InventoryLow, QualityInspectionFailed
import subscribers
import ai
import ai.subscribers
import ai.agents

# Wire domain-event subscribers to the in-process event bus (ADR-0001).
subscribers.register(event_bus)
# The AI platform subscribes to the same event stream (ADR-0003).
ai.subscribers.register(event_bus)
# AI agents act on the stream - autonomy, not just advice (ADR-0004).
ai.agents.register(event_bus)


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


def _ensure_column(table: str, column: str, ddl: str):
    """Idempotent migration: add a column to an existing table (create_all only
    creates missing tables, never alters existing ones)."""
    from sqlalchemy import inspect, text
    try:
        cols = [c["name"] for c in inspect(engine).get_columns(table)]
        if column not in cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            print(f"[MIGRATE] {table}.{column} added")
    except Exception as e:
        print(f"[MIGRATE] {table}.{column} skipped: {e}")


def _ensure_index(table: str, column: str):
    """Idempotent migration: index a column on an existing table (create_all only
    creates missing tables, so existing prod tables need it added explicitly).
    CREATE INDEX IF NOT EXISTS works on both PostgreSQL and SQLite."""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))
    except Exception as e:
        print(f"[MIGRATE] index {table}.{column} skipped: {e}")


_ensure_user_tenant_column()
_ensure_column("machines", "line", "ALTER TABLE machines ADD COLUMN line VARCHAR DEFAULT ''")
_ensure_column("work_orders", "material_state", "ALTER TABLE work_orders ADD COLUMN material_state VARCHAR DEFAULT 'RAW'")
# The windowed read-models filter these by created_at in SQL — index them so the
# window stays fast as the tables grow.
_ensure_index("production_records", "created_at")
_ensure_index("downtime_logs", "created_at")
_ensure_index("cost_records", "created_at")
_ensure_index("quality_inspections", "created_at")
_ensure_index("shift_data", "created_at")
_ensure_index("machine_events", "created_at")   # risk-window scans (ai/prediction)
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

# Register the read-model projection endpoints (ADR-0007) — the pillar summaries,
# briefing, scorecard, twin, search, weekly report and rule-first copilot.
read_model_routes.register(app)

# Register the agent oversight endpoints (ADR-0004/0005) — activity log + approval
# queue, roster, autonomy policy, impact, trend, and human approve/reject.
agent_routes.register(app)

# Register the SaaS / tenant-lifecycle endpoints (ADR-0008) — the founder's
# control plane: registry, onboarding, admin provisioning, plan/status, delete.
saas_routes.register(app)

# Register the costing endpoints — cost-record CRUD + costing analytics.
costing_routes.register(app)

# Register the machine & telemetry CRUD (ADR-0009) — machines, downtime, shifts,
# production records, and the machine-event stream.
machines_routes.register(app)

# Register the orders & procurement CRUD (ADR-0009) — customer orders, suppliers,
# purchase orders, their analytics, CSV export, and escalation generation.
orders_routes.register(app)

# Register the factory-ops CRUD (ADR-0009) — escalations, factory layout,
# documents, maintenance tasks, notifications (+ their generators).
factory_ops_routes.register(app)
work_orders_routes.register(app)
inventory_routes.register(app)
quality_routes.register(app)
production_planning_routes.register(app)
industrial_iot_routes.register(app)
operator_routes.register(app)
users_routes.register(app)
reports_routes.register(app)

# Register the AI Factory Copilot behind the platform (off until ANTHROPIC_API_KEY is set).
ai.copilot.register(app)

# Register the industrial connectivity adapter framework (OPC UA, Modbus, S7,
# Allen-Bradley, Beckhoff, Omron) — GET /industrial/protocols.
industrial_adapters.register(app)


# Tenants whose factories are ANIMATED by the simulator (comma-separated env,
# default: only the founder demo workspace). A customer tenant with real
# machine data must never be ticked — the sim would overwrite real statuses
# with random ones. Opt a demo tenant in via SIM_TENANTS=DEFAULT,APEX.
SIM_TENANTS = [t.strip() for t in os.environ.get("SIM_TENANTS", tenancy.DEFAULT_TENANT).split(",") if t.strip()]

# Sim-loop heartbeat, surfaced (founder-only) in /platform/status so "is the
# sim running, and over which tenants?" is answerable from the app instead of
# from Railway logs.
_SIM_LAST_TICK = None
_SIM_TICK_COUNT = 0


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
    from tenancy import set_current_tenant, reset_current_tenant
    await asyncio.sleep(10)  # let the server fully start first
    while True:
        try:
            db = SessionLocal()
            # Each sim-enabled tenant is ticked under its own scope, so every
            # query and every new row inside the ticks stays in that tenant.
            for sim_tenant in SIM_TENANTS:
                scope = set_current_tenant(sim_tenant)
                try:
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
                except Exception as tick_err:
                    db.rollback()
                    print(f"[SIM TICK ERROR] {sim_tenant}: {tick_err}")
                finally:
                    reset_current_tenant(scope)
            global _SIM_LAST_TICK, _SIM_TICK_COUNT
            _SIM_LAST_TICK = datetime.utcnow()
            _SIM_TICK_COUNT += 1

            # Proactive briefing: the Escalation agent raises the most urgent
            # briefing alert for each tenant on its own (deduped, so it won't
            # repeat). Bind the tenant per pass so the read-models see only that
            # tenant's data (ADR-0002 auto-scoping is a no-op in this background task).
            if random.random() < 0.3:
                from tenancy import set_current_tenant, reset_current_tenant
                tenants = [t for (t,) in db.query(models.Machine.tenant_code).distinct().all() if t]
                for tc in tenants:
                    token = set_current_tenant(tc)
                    try:
                        ai.agents.escalate_from_briefing(db, tc)
                        db.commit()
                    except Exception as esc_err:
                        db.rollback()
                        print(f"[SIM ESCALATE ERROR] {tc}: {esc_err}")
                    finally:
                        reset_current_tenant(token)
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
        # One-time factory rebuild: set RESEED_FACTORY=<any value> to rebuild the
        # DEFAULT tenant as the SMT->IC plant. SINGLE-SHOT: each flag value is
        # consumed exactly once (recorded in the append-only event_log, which the
        # wipe never touches), so a forgotten flag can no longer silently reseed
        # on every deploy — that wiped prod ~41 times on 2026-07-18. To reseed
        # again, set a NEW value (e.g. a date). DEFAULT-only; GMATS untouched.
        reseed_flag = os.environ.get("RESEED_FACTORY")
        if reseed_flag:
            import json as _json
            consumed = (db.query(models.EventLog)
                        .filter(models.EventLog.event_type == "FactoryReseeded",
                                models.EventLog.payload.contains(f'"flag": "{reseed_flag}"'))
                        .first())
            if consumed:
                print(f"[RESEED] flag '{reseed_flag}' already consumed — skipping "
                      "(set a new value to reseed again, and remove the variable when done)")
            else:
                try:
                    from reset_factory import rebuild_factory
                    rebuild_factory(db)
                    db.add(models.EventLog(tenant_code="DEFAULT", event_type="FactoryReseeded",
                                           event_version=1,
                                           payload=_json.dumps({"flag": reseed_flag})))
                    db.commit()
                    print(f"[RESEED] DEFAULT rebuilt to the SMT->IC factory "
                          f"(flag '{reseed_flag}' consumed; future boots skip it)")
                except Exception as e:
                    db.rollback()
                    print(f"[RESEED] factory reset failed: {e}")
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


# Locked-down CORS. Extra production origins can be added via ALLOWED_ORIGINS
# (comma-separated) in the Railway env; the regex keeps Vercel preview deploys
# and any marx8.com host working. The live domain (app.marx8.com) is baked into
# the default so the app keeps working even if ALLOWED_ORIGINS is never set.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "https://app.marx8.com,https://flow-mes.vercel.app").split(",")
    if o.strip()
]

# Middleware order note: Starlette runs the LAST-added middleware FIRST
# (outermost). The plan gate must sit INSIDE CORS — its 403 responses need
# CORS headers or cross-origin browsers report an opaque network error
# instead of a readable 403 — so it is added BEFORE CORSMiddleware.
app.add_middleware(plan_gate.PlanGateMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://[a-z0-9-]+-ashwinvars-projects\.vercel\.app|https://([a-z0-9-]+\.)?marx8\.com|http://localhost:3000|http://127\.0\.0\.1:3000",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Bind the caller's tenant (from the JWT) per request so the ORM auto-scopes
# core-table queries (ADR-0002). Pure-ASGI (tenancy.TenantScopeMiddleware) to
# avoid BaseHTTPMiddleware's request-body deadlock and to propagate contextvars.
app.add_middleware(tenancy.TenantScopeMiddleware)

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

    # Subscription enforcement: a cancelled company can no longer sign in.
    # Only applies when the tenant has a registry row that says Cancelled —
    # tenants outside the registry (legacy) and Trial/Active/Past Due all pass.
    if tenant != tenancy.DEFAULT_TENANT:
        reg = db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == tenant).first()
        if reg and reg.subscription_status == "Cancelled":
            log_audit(db, db_user.username, "login_blocked", "user", db_user.id, f"tenant={tenant} cancelled")
            raise HTTPException(status_code=403, detail="Subscription inactive — contact your provider")
        if reg and reg.trial_expired:
            log_audit(db, db_user.username, "login_blocked", "user", db_user.id, f"tenant={tenant} trial expired")
            raise HTTPException(status_code=403, detail="Trial expired — contact your provider to activate your subscription")

    log_audit(db, db_user.username, "login", "user", db_user.id, f"tenant={tenant}")
    token = create_access_token(data={"sub": db_user.username, "role": db_user.role, "tenant": tenant})

    return {
        "access_token": token,
        "token_type": "bearer",
        "role": db_user.role,
        "tenant": tenant,
    }


def _same_tenant_or_403(user, current_user):
    tenant = _tenant(current_user)
    user_tenant = user.tenant_code or "DEFAULT"
    if user_tenant != tenant:
        raise HTTPException(status_code=403, detail="You can only manage users in your own company")


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


@app.post("/briefing/escalate")
def escalate_briefing(db: Session = Depends(get_db),
                      current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    # Proactive briefing (ADR-0005): the Escalation agent turns the briefing's most
    # urgent (high-severity) alert into a proposed escalation in the approval queue.
    result = ai.agents.escalate_from_briefing(db, _tenant(current_user))
    db.commit()
    return result


@app.post("/auth/refresh")
def refresh_token(current_user: dict = Depends(get_current_user)):
    # Sliding session: a valid (not-yet-expired) token can be exchanged for a
    # fresh one carrying the same identity claims. The frontend calls this when
    # the token nears expiry, so an active user is never logged out mid-shift —
    # while idle sessions still expire naturally.
    token = create_access_token(data={
        "sub": current_user.get("sub"),
        "role": current_user.get("role"),
        "tenant": current_user.get("tenant", "DEFAULT"),
    })
    return {"access_token": token, "token_type": "bearer"}


@app.post("/auth/change-password")
def change_password(payload: schemas.ChangePasswordRequest, db: Session = Depends(get_db),
                    current_user: dict = Depends(get_current_user)):
    """Any signed-in user can rotate their own password (used after receiving a
    provisioned temporary password, or routinely)."""
    db_user = db.query(models.User).filter(models.User.username == current_user.get("sub")).first()
    if not db_user or not verify_password(payload.current_password, db_user.password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    db_user.password = hash_password(payload.new_password)
    db.commit()
    log_audit(db, db_user.username, "change_password", "user", db_user.id, "self-service")
    return {"message": "Password changed"}


# NOTE: /health is owned by platform_routes.register() (registered first, so it
# wins routing) and returns a truthful status code — 200 healthy / 503 DB down.
# A second /health used to be defined here; it was dead (shadowed) and always
# returned 200, so removing it changes nothing served while eliminating a
# duplicate that could silently disable DB monitoring if registration order
# ever changed. See platform_routes.py.


@app.get("/platform/status")
def platform_status(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # AI platform self-report (ADR-0003): registered read-models, the agent roster,
    # copilot connectivity, and the tenant's logged agent actions.
    result = ai.platform_status.build_platform_status(db, _tenant(current_user))
    # Sim-loop diagnostics are founder-only: the allowlist names other tenants,
    # which a client workspace must not see.
    if current_user.get("tenant", tenancy.DEFAULT_TENANT) == tenancy.DEFAULT_TENANT:
        result["sim"] = {
            "tenants": SIM_TENANTS,
            "last_tick_utc": _SIM_LAST_TICK.isoformat() if _SIM_LAST_TICK else None,
            "tick_count": _SIM_TICK_COUNT,
        }
    return result


@app.get("/machine-health/{machine_id}")
def get_machine_detail(machine_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Machine Health detail (ADR-0006): the single-machine cockpit — the twin
    # snapshot plus a risk-factor breakdown, a unified event timeline, and the
    # agent actions awaiting approval for this machine.
    detail = ai.twin.build_machine_detail(db, _tenant(current_user), machine_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    return detail


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


@app.get("/ops-trends")
def get_ops_trends(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Ops trends (ADR-0007): last-7-days daily series across the four pillars —
    # production, downtime, quality, and agent activity — tenant-scoped.
    return ai.trends.build_ops_trends(db, _tenant(current_user))


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
