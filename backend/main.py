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
import connected_equipment_routes
import oem_routes
import tenancy
import ws_auth
import sim_state
import onboard_tenant
import offboard_tenant
import http_security
import logging_config
import plan_gate


# Request tenant resolution lives in tenancy.py (so route modules can import it
# without depending on main). Kept as `_tenant` here for the many call sites.
_tenant = tenancy.request_tenant

import enterprise_inventory_routes
import gmats_inventory_routes
import monitoring
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
import bom_routes
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


# Structured logging before anything else runs: the boot migrations below are
# the first things that report, and their output is exactly what you need when
# a deploy goes wrong. Called again at startup (see startup_event) because
# uvicorn installs its own plain-text handlers AFTER this module is imported.
logging_config.configure_logging()
log = logging_config.get_logger(__name__)

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
            log.info("[MIGRATE] users.tenant_code added")
    except Exception as e:
        log.info(f"[MIGRATE] tenant_code skipped: {e}")


def _ensure_column(table: str, column: str, ddl: str):
    """Idempotent migration: add a column to an existing table (create_all only
    creates missing tables, never alters existing ones)."""
    from sqlalchemy import inspect, text
    try:
        cols = [c["name"] for c in inspect(engine).get_columns(table)]
        if column not in cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            log.info(f"[MIGRATE] {table}.{column} added")
    except Exception as e:
        log.info(f"[MIGRATE] {table}.{column} skipped: {e}")


def _backfill_completed_at():
    """Stamp completed_at on work orders that are ALREADY Completed.

    Without this, every historic Completed order would carry a NULL completed_at,
    and the first PATCH that left it Completed would look like a first completion
    and move its bill of materials a SECOND time — the very bug the column exists
    to stop, fired once per order at deploy.

    created_at is used as the stamp rather than now(), so the backfill does not
    claim the whole order book completed at deploy time. The exact instant is
    unknown for historic rows; what matters is only that it is not NULL.

    This exactly preserves today's behaviour for existing data. An order that
    reached Completed via PATCH already moved its BOM, and now cannot move it
    again; an order created directly as Completed never moved it (create does not
    publish) and still will not.

    Idempotent, so it is safe on every boot: after this change the only way to be
    Completed with a NULL completed_at is to have been created that way, and
    stamping those matches the pre-existing no-publish behaviour too.
    """
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE work_orders SET completed_at = created_at "
                "WHERE status = 'Completed' AND completed_at IS NULL"))
        if result.rowcount:
            log.info(f"[MIGRATE] work_orders.completed_at backfilled for {result.rowcount} row(s)")
    except Exception as e:
        log.info(f"[MIGRATE] work_orders.completed_at backfill skipped: {e}")


def _ensure_index(table: str, column: str):
    """Idempotent migration: index a column on an existing table (create_all only
    creates missing tables, so existing prod tables need it added explicitly).
    CREATE INDEX IF NOT EXISTS works on both PostgreSQL and SQLite."""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))
    except Exception as e:
        log.info(f"[MIGRATE] index {table}.{column} skipped: {e}")


_ensure_user_tenant_column()
_ensure_column("machines", "line", "ALTER TABLE machines ADD COLUMN line VARCHAR DEFAULT ''")
# Half of a machine's identity (tenant, site, name) — see alembic 0002. Added at
# boot as well as by the migration because the app can start before the release
# command's migrate step has run, and every Machine query selects this column;
# a missing column is a 500 on the dashboard, not a degraded feature. The
# migration owns the UNIQUE constraint, which cannot be expressed here.
_ensure_column("machines", "site", "ALTER TABLE machines ADD COLUMN site VARCHAR DEFAULT ''")
_ensure_column("work_orders", "material_state", "ALTER TABLE work_orders ADD COLUMN material_state VARCHAR DEFAULT 'RAW'")
_ensure_column("work_orders", "completed_at", "ALTER TABLE work_orders ADD COLUMN completed_at TIMESTAMP")
_backfill_completed_at()
_ensure_column("tenant_configs", "unit_value_gbp", "ALTER TABLE tenant_configs ADD COLUMN unit_value_gbp FLOAT")
# THE APPROVAL GATE'S REVOCATION FLAG (alembic 0005) — and the reason the block
# below exists at all.
#
# `models.User.is_active` shipped with a migration and NO entry here. Production
# does not run Alembic on deploy (docs/MIGRATIONS.md: migrate.py is run by hand),
# and create_all only creates missing TABLES, never alters an existing one. So
# the column never landed, and because SQLAlchemy names every mapped column in
# its SELECT list, EVERY User query became:
#
#     psycopg2.errors.UndefinedColumn: column users.is_active does not exist
#
# which is a 500 on /login. The whole product was unreachable — nobody could
# sign in — while /health went on answering 200, because it does not go through
# the ORM. Every test passed throughout: they build `users` fresh with
# create_all, which is precisely the case that cannot reproduce this.
#
# NOT NULL DEFAULT TRUE matches the migration and the model: every existing
# login stays active, and revocation stays an explicit act rather than a NULL
# nobody can interpret. On PostgreSQL 11+ a defaulted NOT NULL add is metadata
# only, so this does not rewrite the table.
_ensure_column("users", "is_active",
               "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
# Same migration, same omission — the approval gate's freshness window.
_ensure_column("agent_actions", "expires_at",
               "ALTER TABLE agent_actions ADD COLUMN expires_at TIMESTAMP")
# Same class again, from alembic 0007: `machine_installations` was created by an
# earlier deploy's create_all, so the service-clock columns added afterwards were
# never applied. Nullable with no backfill, exactly as the migration argues —
# NULL means "nobody has recorded a service", and any other value would invent one.
_ensure_column("machine_installations", "last_service_hours",
               "ALTER TABLE machine_installations ADD COLUMN last_service_hours FLOAT")
_ensure_column("machine_installations", "last_service_at",
               "ALTER TABLE machine_installations ADD COLUMN last_service_at TIMESTAMP")
# The windowed read-models filter these by created_at in SQL — index them so the
# window stays fast as the tables grow.
_ensure_index("production_records", "created_at")
_ensure_index("downtime_logs", "created_at")
_ensure_index("cost_records", "created_at")
_ensure_index("quality_inspections", "created_at")
_ensure_index("shift_data", "created_at")
_ensure_index("machine_events", "created_at")   # risk-window scans (ai/prediction)
_ensure_index("machine_events", "machine_name")  # per-machine GROUP BY (/analytics/machine-state-summary)
# The edge-connectivity and inventory read-models poll these on a ~30s refresh and
# grow fastest of all — iot_telemetry every sim tick, inventory_transactions every
# issue/receipt — yet were unindexed. Index the columns they filter/group by so the
# poll stays a range scan, not a full-table scan.
_ensure_index("iot_telemetry", "created_at")        # connectivity freshness window
_ensure_index("iot_telemetry", "machine_id")        # per-machine + DARK-vs-STALE probe
_ensure_index("inventory_transactions", "created_at")  # coverage burn-rate window
_ensure_index("inventory_transactions", "item_id")     # per-item burn / part-runway drill-down
_ensure_index("production_plans", "plan_date")      # schedule-adherence window (now filtered in SQL)
_ensure_index("production_plans", "status")         # delay-rec generator filters status=='Behind' in SQL (/ai/generate-recommendations)
_ensure_index("production_schedules", "scheduled_date")  # schedule-load board window (filtered in SQL)
_ensure_index("operator_job_executions", "started_at")   # operator-performance window (filtered in SQL)
_ensure_index("cycle_count_items", "created_at")   # inventory-record-accuracy window (filtered in SQL)
_ensure_index("cycle_count_items", "item_id")      # scope count lines to the tenant's items (ai/stock_accuracy)
_ensure_index("escalations", "created_at")               # escalation-queue resolution window (filtered in SQL)
_ensure_index("customer_orders", "due_date")             # late-order count + escalation generator filter due_date in SQL
_ensure_index("maintenance_tasks", "planned_date")       # overdue-task count filters planned_date in SQL (/analytics/maintenance)
_ensure_index("work_orders", "status")                   # predictive-risk work-order load filters status IN active in SQL (ai/prediction)
_ensure_index("compliance_documents", "review_due_date") # review-due count filters review_due_date in SQL (/analytics/documents)
# Per-machine FK columns the Machine-Health twin filters in SQL (ADR-0006). The
# /machine-health list (ai.twin.build_twins) fires a per-machine query PER machine
# — recent downtime, open maintenance tasks, and pending agent actions — and the
# /machine-health/{id} cockpit (build_machine_detail) adds per-machine production
# and quality lookups. All of these tables grow continuously, yet a plain
# ForeignKey column is NOT indexed (SQLAlchemy indexes only the PK), so each
# per-machine filter was a full-table scan that got slower every week — worst for a
# HEALTHY machine with no recent rows, where the `machine_id == X` scan reaches the
# end of the table finding nothing. Index the machine_id / related_machine_id
# columns these read-models filter on, the same idempotent _ensure_index hardening
# already applied to iot_telemetry.machine_id and inventory_transactions.item_id.
# The created_at indexes above still serve the windowed variants; these serve the
# per-machine scoping on top of the window.
_ensure_index("downtime_logs", "machine_id")             # twin recent-downtime + cockpit trend/timeline (ai/twin)
_ensure_index("production_records", "machine_id")        # cockpit throughput window + daily-cap check (ai/twin, factory_simulator)
_ensure_index("quality_inspections", "machine_id")       # cockpit quality window + /analytics/quality per-machine GROUP BY
_ensure_index("maintenance_tasks", "machine_id")         # twin open-task count + cockpit timeline (ai/twin)
_ensure_index("agent_actions", "related_machine_id")     # twin pending-action count + cockpit timeline/open-actions (ai/twin)
# GMATS transactional-listing line lookups: /gmats/proformas and /gmats/min batch
# every page's lines in ONE query (proforma_id / min_id IN (...)) instead of a
# per-row N+1. These FK columns carry no index (SQLAlchemy indexes only the PK), so
# the join was a full-table scan of the line tables that grew with every document.
_ensure_index("gmats_proforma_lines", "proforma_id")     # batched line fetch for /gmats/proformas listing
_ensure_index("gmats_min_lines", "min_id")               # batched line fetch for /gmats/min listing
tenancy.ensure_tenant_columns(engine)  # ADR-0002: tenant_code on core tables
# Audit trail + enterprise-inventory: add tenant_code NULLABLE, NO blind backfill.
# Legacy rows stay NULL (hidden) until an approved, source-based backfill assigns
# them — never silently handed to DEFAULT. (backfill_enterprise_tenants.py)
tenancy.ensure_tenant_columns(engine, tenancy.FAIL_SAFE_TENANT_TABLES, backfill=False)
tenancy.install_scoping()              # ADR-0002: auto-enforce tenant scoping

# Optional error monitoring — active only when SENTRY_DSN is set in the env.
_SENTRY_DSN = os.environ.get("SENTRY_DSN")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_SENTRY_DSN, traces_sample_rate=0.1, environment=os.environ.get("ENV", "production"))
        log.info("[sentry] error monitoring enabled")
    except Exception as e:
        log.info(f"[sentry] init skipped: {e}")

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

# The deployment-health read-model (ADR-0007) — GET /system-health, the
# operator's diagnostic view: database round-trip and pool, MQTT ingest
# liveness, live WebSocket clients, growth of the append-only tables, build sha
# and whether Sentry is configured.
#
# Deliberately separate from /health. /health is the Railway probe and the
# uptime monitor's contract: public, and its STATUS CODE carries the answer
# (503 when the database is unreachable). /system-health is authenticated,
# always 200, and its BODY carries the detail. Merging them would mean either
# leaking internals publicly or breaking the probe's contract.
app.include_router(monitoring.router)

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
app.include_router(bom_routes.router)
app.include_router(core_routes.router)

# Register the OEM portal (ADR-0017) — the machine manufacturer's view of its
# installed fleet across customer factories. Every route authenticates as an OEM
# principal, which binds a SENTINEL factory tenant, so these handlers cannot
# reach a customer's operational data even if one of them forgets to filter.
app.include_router(oem_routes.router)

# Register the factory's own view of its connected equipment (ADR-0017) — which
# machines came from an OEM, and exactly what that OEM can see about them. A
# consent control nobody can read is not consent.
app.include_router(connected_equipment_routes.router)

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
        drift_utilization,
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
                        # NULL-guarded drift (see factory_simulator.drift_utilization):
                        # a Running machine with a NULL utilization used to raise
                        # `None + int` here, rolling back this whole tenant's tick.
                        m.utilization = drift_utilization(m.utilization, random.randint(-5, 5))
                    db.commit()
                except Exception as tick_err:
                    db.rollback()
                    log.info(f"[SIM TICK ERROR] {sim_tenant}: {tick_err}")
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
                        log.info(f"[SIM ESCALATE ERROR] {tc}: {esc_err}")
                    finally:
                        reset_current_tenant(token)
            db.close()
        except Exception as e:
            log.info(f"[SIM TICK ERROR] {e}")
        await asyncio.sleep(45)


@app.on_event("startup")
async def startup_event():
    # Re-apply: uvicorn installs its own plain-text handlers after import, which
    # would otherwise emit a second, unstructured copy of every access line.
    logging_config.configure_logging()
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
                log.info(f"[RESEED] flag '{reseed_flag}' already consumed — skipping "
                      "(set a new value to reseed again, and remove the variable when done)")
            else:
                try:
                    from reset_factory import rebuild_factory
                    rebuild_factory(db)
                    db.add(models.EventLog(tenant_code="DEFAULT", event_type="FactoryReseeded",
                                           event_version=1,
                                           payload=_json.dumps({"flag": reseed_flag})))
                    db.commit()
                    log.info(f"[RESEED] DEFAULT rebuilt to the SMT->IC factory "
                          f"(flag '{reseed_flag}' consumed; future boots skip it)")
                except Exception as e:
                    db.rollback()
                    log.info(f"[RESEED] factory reset failed: {e}")
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
            log.info("[SEED] GMATS client login (gmats / gmats@2026)")
        # Seed a GMATS Admin from env (password never hardcoded — set GMATS_ADMIN_PASSWORD in Railway).
        gmats_admin_user = os.environ.get("GMATS_ADMIN_USERNAME", "gmats_admin")
        gmats_admin_pw = os.environ.get("GMATS_ADMIN_PASSWORD")
        if gmats_admin_pw and not db.query(models.User).filter(models.User.username == gmats_admin_user).first():
            db.add(models.User(username=gmats_admin_user, password=hash_password(gmats_admin_pw), role="Admin", tenant_code="GMATS"))
            db.commit()
            log.info(f"[SEED] GMATS Admin '{gmats_admin_user}' created from GMATS_ADMIN_PASSWORD env")
        # Reconcile client logins to their correct tenant. Users created before the
        # tenant_code column existed were backfilled to DEFAULT by the migration.
        for uname, tcode in tenancy.CLIENT_TENANTS.items():
            u = db.query(models.User).filter(models.User.username == uname).first()
            if u and (u.tenant_code or "DEFAULT") != tcode:
                u.tenant_code = tcode
                db.commit()
                log.info(f"[MIGRATE] {uname} tenant_code -> {tcode}")
        db.close()
    except Exception as e:
        log.info(f"[GMATS SEED ERROR] {e}")


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

# Rate limiting sits INSIDE CORS for the same reason the plan gate does: a
# 429 that a cross-origin browser cannot read is indistinguishable from the
# API being down, and the frontend would show "network unreachable" instead
# of "you are being throttled".
app.add_middleware(http_security.RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://[a-z0-9-]+-ashwinvars-projects\.vercel\.app|https://([a-z0-9-]+\.)?marx8\.com|http://localhost:3000|http://127\.0\.0\.1:3000",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added LAST so it runs FIRST (Starlette runs the last-added middleware
# outermost). Security headers must wrap everything — including CORS
# preflights, 404s, and the plan gate's and throttle's own rejections — so
# there is no response path that escapes without them.
app.add_middleware(http_security.SecurityHeadersMiddleware)

# Added after the header middleware, so it runs just inside it: every log line
# emitted while handling a request — including from the plan gate and the
# throttle — carries the same request id, and the id is echoed to the caller so
# a support report can be grepped straight to the failing request.
app.add_middleware(logging_config.RequestContextMiddleware)


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
    # WS auth headers), BEFORE accepting. This used to accept every connection
    # and merely bind the decoded tenant, so a missing, forged or expired token
    # still got an open socket, and a DELETED user's token still bound to their
    # factory and streamed its telemetry (ADR-0016).
    db = SessionLocal()
    try:
        tenant = ws_auth.resolve(db, websocket.query_params.get("token"))
    except ws_auth.WsDenied as denied:
        # close() without accept() is the ASGI refusal: the handshake never
        # completes, so there is no socket to leak or to account for.
        await websocket.close(code=denied.code, reason=denied.reason)
        log.info("WebSocket refused (%s): %s", denied.code, denied.reason)
        return
    finally:
        db.close()

    if not await manager.connect(websocket, tenant):
        await websocket.close(code=ws_auth.REFUSED, reason="No workspace")
        return
    try:
        await websocket.send_json({"event": "connected", "message": "AMP live WebSocket connected"})
        while True:
            # The server has no inbound protocol: the feed is one-way. Race the
            # heartbeat against a client frame so an unexpected frame is
            # REFUSED rather than merely ignored -- "the server never reads" is
            # fail-closed by accident, and an accident is not a contract.
            receive = asyncio.ensure_future(websocket.receive())
            done, _ = await asyncio.wait({receive}, timeout=30)
            if receive in done:
                message = receive.result()
                if message.get("type") == "websocket.disconnect":
                    break
                await websocket.close(code=ws_auth.NO_CLIENT_FRAMES,
                                      reason="This feed accepts no client messages")
                break
            receive.cancel()
            try:
                await websocket.send_json({"event": "heartbeat", "message": "alive"})
            except Exception:
                break
    except WebSocketDisconnect:
        log.info("WebSocket client disconnected")
    except ConnectionResetError:
        log.info("WebSocket forcibly closed by client")
    except Exception as e:
        log.info("WebSocket error: %s", repr(e))
    finally:
        manager.disconnect(websocket)
