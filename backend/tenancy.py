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
from sqlalchemy import inspect, text

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
