"""Tenant scoping for the core domain (ADR-0002).

Keeps each tenant's operational data isolated. Three pieces:

  * ``ensure_tenant_columns`` — idempotent startup migration that adds a
    ``tenant_code`` column (+ index) to core tables and backfills existing rows.
    Runs on every boot; safe on both SQLite (dev) and PostgreSQL (prod).
  * ``tenant_of`` — derive the caller's tenant from the JWT principal, never
    from client input.
  * ``TenantScopedRepository`` — filters every read and stamps every write with
    the caller's tenant, so a handler cannot accidentally read or write across
    tenants.

This PR ships the mechanism and the schema. Wiring endpoints through the
repository (read-enforcement) is rolled out table-by-table in follow-up PRs,
once ownership of existing rows is settled — see ADR-0002.
"""
import contextvars

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session, with_loader_criteria

import models
from auth import decode_token_optional

DEFAULT_TENANT = "DEFAULT"

# Client logins whose tenant isn't stored on the user row (legacy accounts created
# before the tenant_code column) — mapped by username to their tenant. Read at
# startup (reconcile) and at login (fallback when the user has no tenant_code).
CLIENT_TENANTS = {"gmats": "GMATS"}

# Core operational tables that gain a tenant_code column in this PR. The rest of
# the core tables follow in later PRs; platform and GMATS tables are already
# tenant-aware, and event_log is already tenant-stamped (ADR-0001).
CORE_TENANT_TABLES = [
    "machines", "work_orders", "inventory_items", "inventory_transactions",
    "downtime_logs", "shift_data", "production_records", "alerts",
    "machine_events", "production_plans", "escalations", "quality_inspections",
    "factory_layout_nodes", "customer_orders", "suppliers", "purchase_orders",
    "compliance_documents", "maintenance_tasks", "production_schedules",
    "iot_telemetry", "ai_recommendations", "cost_records",
    "operator_job_executions", "notifications", "report_requests",
    "industrial_devices", "industrial_signals", "plc_signal_mappings",
]

# Tables that gain tenant_code but must NOT be blind-backfilled to DEFAULT: the
# audit trail and the enterprise-inventory domain. Their legacy rows have no
# reliable in-row tenant, so a blanket UPDATE ... = 'DEFAULT' could hand another
# tenant's records (or system audit) to the founder workspace. They are added
# NULLABLE and left NULL — hidden from every tenant's reads — until an approved,
# source-based backfill (see backfill_enterprise_tenants.py) assigns them.
FAIL_SAFE_TENANT_TABLES = [
    "audit_logs", "remnants", "material_issue_slips", "goods_receipt_notes",
    "grn_items", "cycle_counts", "cycle_count_items",
]


def ensure_tenant_columns(engine, tables=CORE_TENANT_TABLES, backfill=True):
    """Idempotently add ``tenant_code`` (+ index) to each table. Never raises — a
    migration hiccup must not block application startup.

    ``backfill=True`` (core tables): existing rows are set to the DEFAULT tenant.
    ``backfill=False`` (audit + enterprise inventory): the column is added
    NULLABLE with no default and existing rows are LEFT NULL. A NULL tenant_code
    matches no tenant under the read filter, so an ambiguous legacy row is hidden
    rather than silently assigned to the wrong tenant — it waits for an approved,
    source-based backfill."""
    insp = inspect(engine)
    present = set(insp.get_table_names())
    for table in tables:
        if table not in present:
            continue  # a fresh DB gets the column from the model via create_all
        cols = [c["name"] for c in insp.get_columns(table)]
        if "tenant_code" in cols:
            continue
        try:
            with engine.begin() as conn:
                if backfill:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN tenant_code VARCHAR DEFAULT '{DEFAULT_TENANT}'"
                    ))
                    conn.execute(text(
                        f"UPDATE {table} SET tenant_code = '{DEFAULT_TENANT}' WHERE tenant_code IS NULL"
                    ))
                else:
                    # Nullable, no default, no backfill — legacy rows stay NULL
                    # (hidden) pending an approved, source-based backfill.
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN tenant_code VARCHAR"))
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_code ON {table} (tenant_code)"
                ))
            print(f"[MIGRATE] {table}.tenant_code added (backfill={backfill})")
        except Exception as e:
            print(f"[MIGRATE] {table}.tenant_code skipped: {e}")


def tenant_of(current_user, default=DEFAULT_TENANT):
    """The caller's tenant, taken from the JWT principal — never client input."""
    if not current_user:
        return default
    return current_user.get("tenant", default)


class TenantScopedRepository:
    """Data-access wrapper that isolates a single tenant's rows.

    Every read is filtered to ``tenant``; every object added is stamped with it.
    Handlers use this instead of raw ``db.query(Model)`` so scoping cannot be
    forgotten per endpoint.
    """

    def __init__(self, db, model, tenant):
        self.db = db
        self.model = model
        self.tenant = tenant

    def query(self):
        return self.db.query(self.model).filter(self.model.tenant_code == self.tenant)

    def all(self):
        return self.query().all()

    def get(self, obj_id):
        return self.query().filter(self.model.id == obj_id).first()

    def add(self, obj):
        obj.tenant_code = self.tenant
        self.db.add(obj)
        return obj


# ── Automatic request-scoped tenant enforcement (ADR-0002) ─────────────
# One chokepoint instead of editing ~80 query sites: a per-request tenant is set
# from the JWT (middleware in main.py); a do_orm_execute hook adds
# `tenant_code = :tenant` to every SELECT of a scoped model; a before_flush hook
# stamps new rows. When no tenant is set (background/system work, seeding), both
# are no-ops — so the simulation loop, MQTT ingestion and startup seeding are
# unaffected. Because read-by-id also goes through a SELECT, this transparently
# protects get/update/delete-by-id too (a foreign row simply isn't found).

_current_tenant = contextvars.ContextVar("amp_current_tenant", default=None)

# Models under automatic scoping. Each has a tenant_code column (see models.py).
SCOPED_MODELS = (
    models.Machine, models.WorkOrder, models.InventoryItem, models.InventoryTransaction,
    models.DowntimeLog, models.ShiftData, models.ProductionRecord, models.Alert,
    models.MachineEvent, models.ProductionPlan, models.Escalation, models.QualityInspection,
    models.FactoryLayoutNode, models.CustomerOrder, models.Supplier, models.PurchaseOrder,
    models.ComplianceDocument, models.MaintenanceTask, models.ProductionSchedule,
    models.IoTTelemetry, models.AIRecommendation, models.CostRecord,
    models.OperatorJobExecution, models.Notification, models.ReportRequest,
    models.IndustrialDevice, models.IndustrialSignal, models.PlcSignalMapping,
    # Fail-safe scoping (this PR): the audit trail + the enterprise-inventory
    # domain. These carry a NULLABLE tenant_code (FAIL_SAFE_TENANT_TABLES) —
    # request writes auto-stamp, legacy NULL rows stay hidden until backfilled.
    models.AuditLog, models.Remnant, models.MaterialIssueSlip,
    models.GoodsReceiptNote, models.GRNItem, models.CycleCount, models.CycleCountItem,
)


def set_current_tenant(tenant):
    """Bind the tenant for the current request/context; returns a reset token."""
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


def current_tenant():
    return _current_tenant.get()


def tenant_from_token(token):
    """Tenant claim from a JWT, or None if the token is missing/invalid."""
    payload = decode_token_optional(token)
    return payload.get("tenant") if payload else None


def effective_tenant(claim_tenant, header_tenant):
    """The tenant a request is scoped to. Only the founder workspace (a DEFAULT
    claim) may preview another tenant via the X-Tenant header — that's how the
    top-bar company switcher works. A client token always stays locked to its
    own tenant: the header is ignored for every non-DEFAULT claim."""
    if claim_tenant == DEFAULT_TENANT and header_tenant:
        return header_tenant
    return claim_tenant


def request_tenant(current_user):
    """Tenant scope for a request handler: the middleware-bound effective tenant
    (which honours the founder's X-Tenant company-switcher preview), falling
    back to the JWT claim. Token-issuing endpoints (login/refresh) must NOT use
    this — identity claims always come from the JWT itself."""
    return current_tenant() or (current_user or {}).get("tenant", DEFAULT_TENANT)


def tenant_unit_value(db, tenant):
    """The tenant's configured £ margin per good unit (TenantConfig.unit_value_gbp),
    or None if unset. The single per-tenant £ rate — shared by the recovery
    read-model and the management summary's estimated_loss_value so every money
    figure uses the same number (never a made-up default when unset)."""
    c = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == tenant).first()
    return c.unit_value_gbp if c else None


_scoping_installed = False


def install_scoping():
    """Register the read-filter and write-stamp on the ORM Session (once)."""
    global _scoping_installed
    if _scoping_installed:
        return
    _scoping_installed = True

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_filter(state):
        if not state.is_select:
            return
        tenant = current_tenant()
        if tenant is None:
            return
        for model in SCOPED_MODELS:
            state.statement = state.statement.options(
                with_loader_criteria(model, model.tenant_code == tenant, include_aliases=True)
            )

    @event.listens_for(Session, "before_flush")
    def _stamp_tenant_on_new(session, flush_context, instances):
        tenant = current_tenant()
        if tenant is None:
            return
        for obj in session.new:
            if isinstance(obj, SCOPED_MODELS) and getattr(obj, "tenant_code", None) is None:
                obj.tenant_code = tenant


class TenantScopeMiddleware:
    """Pure-ASGI middleware that binds the request's tenant (from the JWT) so the
    ORM auto-scopes core-table queries. Pure ASGI — not BaseHTTPMiddleware — so it
    shares the endpoint's task (contextvars propagate) and never buffers the
    request body (POST bodies stream normally, no deadlock)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = None
        header_tenant = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                parts = value.decode("latin-1").split(" ", 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1].strip()
            elif key == b"x-tenant":
                header_tenant = value.decode("latin-1").strip() or None
        reset = set_current_tenant(effective_tenant(tenant_from_token(token), header_tenant))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(reset)
