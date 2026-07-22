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
from security import hash_password
from live_ws import manager
from mqtt_service import start_mqtt_service

import models
import schemas
import tenancy
import sim_state
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
import recommendations_routes
import core_routes
import industrial_adapters
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
_ensure_column("tenant_configs", "unit_value_gbp", "ALTER TABLE tenant_configs ADD COLUMN unit_value_gbp FLOAT")
# The windowed read-models filter these by created_at in SQL — index them so the
# window stays fast as the tables grow.
_ensure_index("production_records", "created_at")
_ensure_index("downtime_logs", "created_at")
_ensure_index("cost_records", "created_at")
_ensure_index("quality_inspections", "created_at")
_ensure_index("shift_data", "created_at")
_ensure_index("machine_events", "created_at")   # risk-window scans (ai/prediction)
# The edge-connectivity and inventory read-models poll these on a ~30s refresh and
# grow fastest of all — iot_telemetry every sim tick, inventory_transactions every
# issue/receipt — yet were unindexed. Index the columns they filter/group by so the
# poll stays a range scan, not a full-table scan.
_ensure_index("iot_telemetry", "created_at")        # connectivity freshness window
_ensure_index("iot_telemetry", "machine_id")        # per-machine + DARK-vs-STALE probe
_ensure_index("inventory_transactions", "created_at")  # coverage burn-rate window
_ensure_index("inventory_transactions", "item_id")     # per-item burn / part-runway drill-down
_ensure_index("production_plans", "plan_date")      # schedule-adherence window (now filtered in SQL)
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
app.include_router(enterprise_inventory_routes.router)

# Register GMATS tenant-scoped enterprise inventory (4-bucket stock, aliases,
# proforma reservation, tax-invoice deduction, free-spares material issue note).
app.include_router(gmats_inventory_routes.router)

# Register the platform layer: per-tenant licensing/feature-flags, white-label
# branding, audit log and health check.
app.include_router(platform_routes.router)

# Register the read-model projection endpoints (ADR-0007) — the pillar summaries,
# briefing, scorecard, twin, search, weekly report and rule-first copilot.
app.include_router(read_model_routes.router)

# Register the agent oversight endpoints (ADR-0004/0005) — activity log + approval
# queue, roster, autonomy policy, impact, trend, and human approve/reject.
app.include_router(agent_routes.router)

# Register the SaaS / tenant-lifecycle endpoints (ADR-0008) — the founder's
# control plane: registry, onboarding, admin provisioning, plan/status, delete.
app.include_router(saas_routes.router)

# Register the costing endpoints — cost-record CRUD + costing analytics.
app.include_router(costing_routes.router)

# Register the machine & telemetry CRUD (ADR-0009) — machines, downtime, shifts,
# production records, and the machine-event stream.
app.include_router(machines_routes.router)

# Register the orders & procurement CRUD (ADR-0009) — customer orders, suppliers,
# purchase orders, their analytics, CSV export, and escalation generation.
app.include_router(orders_routes.router)

# Register the factory-ops CRUD (ADR-0009) — escalations, factory layout,
# documents, maintenance tasks, notifications (+ their generators).
app.include_router(factory_ops_routes.router)
app.include_router(work_orders_routes.router)
app.include_router(inventory_routes.router)
app.include_router(quality_routes.router)
app.include_router(production_planning_routes.router)
app.include_router(industrial_iot_routes.router)
app.include_router(operator_routes.router)
app.include_router(users_routes.router)
app.include_router(reports_routes.router)
app.include_router(analytics_routes.router)
app.include_router(recommendations_routes.router)
app.include_router(core_routes.router)

# Register the AI Factory Copilot behind the platform (off until ANTHROPIC_API_KEY is set).
ai.copilot.register(app)

# Register the industrial connectivity adapter framework (OPC UA, Modbus, S7,
# Allen-Bradley, Beckhoff, Omron) — GET /industrial/protocols.
app.include_router(industrial_adapters.router)


# Simulator heartbeat state — which tenants are animated, last tick, tick count —
# lives in sim_state so /platform/status (core_routes) can read it without either
# module importing the other (ADR-0009).


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
            for sim_tenant in sim_state.tenants:
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
            sim_state.last_tick = datetime.utcnow()
            sim_state.tick_count += 1

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
        for uname, tcode in tenancy.CLIENT_TENANTS.items():
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
