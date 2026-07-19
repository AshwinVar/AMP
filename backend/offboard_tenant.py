"""Tenant offboarding — the destructive tail of the tenant lifecycle.

Deleting a company from SaaS Admin removes its registry row, but until now the
tenant's operational data (machines, records, orders, users, licence) stayed
orphaned in every table forever. ``purge_tenant_data`` removes it completely.

Deliberately paranoid:
  * DEFAULT (the founder workspace) and blank codes can never be purged.
  * Every mapped model carrying a ``tenant_code`` column is swept — so new
    tenant-stamped tables are covered automatically, no list to forget.
  * EventLog is kept: it is the platform's immutable history (ADR-0001), and
    the offboarding itself should remain traceable after the data is gone.
  * Returns per-table delete counts for the audit log.
"""
import models
from tenancy import DEFAULT_TENANT

# Immutable history stays after offboarding: the event log records WHAT
# happened on the platform, including that this tenant existed and left.
_KEEP_HISTORY = {"EventLog", "AuditLog"}


def purge_tenant_data(db, tenant_code: str) -> dict:
    """Permanently delete every row stamped with ``tenant_code`` across all
    tenant-aware tables (except immutable history). Returns {table: count}.
    Raises ValueError for DEFAULT or blank codes — those are never purgeable."""
    code = (tenant_code or "").strip()
    if not code or code == DEFAULT_TENANT:
        raise ValueError("This tenant cannot be purged")

    targets = []
    for mapper in models.Base.registry.mappers:
        cls = mapper.class_
        if cls.__name__ in _KEEP_HISTORY:
            continue
        if getattr(cls, "tenant_code", None) is not None:
            targets.append(cls)

    # Foreign keys dictate deletion order (children before machines, etc.) and
    # the mapper registry is unordered — so sweep in passes. Each model's
    # delete runs in a savepoint: an FK violation rolls back just that model,
    # which is retried on the next pass once its children are gone. Repeats
    # until everything is deleted or a pass makes no progress.
    counts = {}
    remaining = list(targets)
    try:
        for _ in range(len(targets) + 1):
            if not remaining:
                break
            progressed = False
            still = []
            for cls in remaining:
                try:
                    with db.begin_nested():
                        n = (db.query(cls)
                               .filter(cls.tenant_code == code)
                               .delete(synchronize_session=False))
                    if n:
                        counts[cls.__tablename__] = counts.get(cls.__tablename__, 0) + n
                    progressed = True
                except Exception:
                    still.append(cls)
            remaining = still
            if not progressed:
                break
        if remaining:
            raise RuntimeError(
                "purge blocked by constraints on: "
                + ", ".join(c.__tablename__ for c in remaining))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts
