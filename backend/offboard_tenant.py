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
from datetime import datetime

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
        # BEFORE the sweep, not after. machine_installations.machine_id is a
        # foreign key onto machines.id, so an installation still pointing at a
        # machine row BLOCKS that row's delete — measured on PostgreSQL as
        # "purge blocked by constraints on: machines", i.e. offboarding a tenant
        # failed outright. SQLite does not enforce foreign keys by default and
        # showed nothing.
        counts.update(_unlink_oem_installations(db, code))
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


def _unlink_oem_installations(db, code: str) -> dict:
    """A departing factory releases its OEM equipment; it does not destroy it.

    An OEM's record of a machine it BUILT belongs to the OEM, not to the customer
    that happened to be running it. The sweep above hard-deletes every row whose
    class has a ``tenant_code`` attribute, so had MachineInstallation used that
    name, offboarding one customer would have erased another company's fleet
    history — a cross-owner deletion, from a routine admin action.

    ADR-0017 names the column ``factory_tenant_code`` precisely so the sweep
    cannot see it, which leaves this function owing the correct behaviour:
    unlink the installation from the factory (and from the factory Machine row
    that is about to be deleted), and record that it is no longer installed.
    The serial number, model, warranty and service history are untouched.
    """
    rows = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.factory_tenant_code == code)
              .all())
    for row in rows:
        row.factory_tenant_code = None
        row.machine_id = None          # the machines row is being deleted above
        row.site = ""
        row.status = "Decommissioned"
        row.decommissioned_at = datetime.utcnow()
    return {"machine_installations_unlinked": len(rows)} if rows else {}
