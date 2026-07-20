import asyncio
import csv
import secrets
import io
import os
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
    calculate_fallback_oee,
    calculate_oee_from_record,
    generate_alerts,
    parse_duration_to_minutes,
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
import analytics_routes
from analytics_routes import analytics_summary
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
analytics_routes.register(app)

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
