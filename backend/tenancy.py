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

# Core operational tables that gain a tenant_code column in this PR. The rest of
# the core tables follow in later PRs; platform and GMATS tables are already
# tenant-aware, and event_log is already tenant-stamped (ADR-0001).
CORE_TENANT_TABLES = [
    "machines",
    "work_orders",
    "inventory_items",
    "inventory_transactions",
]


def ensure_tenant_columns(engine, tables=CORE_TENANT_TABLES):
    """Idempotently add ``tenant_code`` (+ index) to each table and backfill
    existing rows to the default tenant. Never raises — a migration hiccup must
    not block application startup."""
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
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN tenant_code VARCHAR DEFAULT '{DEFAULT_TENANT}'"
                ))
                conn.execute(text(
                    f"UPDATE {table} SET tenant_code = '{DEFAULT_TENANT}' WHERE tenant_code IS NULL"
                ))
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_code ON {table} (tenant_code)"
                ))
            print(f"[MIGRATE] {table}.tenant_code added")
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
    models.Machine,
    models.WorkOrder,
    models.InventoryItem,
    models.InventoryTransaction,
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
