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

from sqlalchemy import select

import contract_linkage
import models
from tenancy import DEFAULT_TENANT

# Immutable history stays after offboarding: the event log records WHAT
# happened on the platform, including that this tenant existed and left.
_KEEP_HISTORY = {"EventLog", "AuditLog"}

# Tenant-less children of tenant-stamped tables, and what the purge does to each.
#
# The sweep below sees only models with a `tenant_code`. A model WITHOUT one that
# holds a foreign key into a tenant table is invisible to it — and on PostgreSQL
# its rows then BLOCK the delete of their parents. GmatsProformaLine and
# GmatsMINLine were exactly that: measured "purge blocked by constraints on:
# gmats_items, gmats_proformas, gmats_min", so any tenant that had ever raised a
# proforma or issued a MIN could not be offboarded at all.
#
# "Delete every child" is not the answer: MachineInstallation is such a child too,
# and it belongs to the OEM that built the machine (see _unlink_oem_installations).
# So each one's fate is DECLARED here, and test_offboarding enumerates the real
# model registry and fails for any tenant-less child that is not listed — the next
# one cannot be forgotten.
TENANTLESS_CHILDREN = {
    "GmatsProformaLine": "deleted with its parent proforma (_purge_gmats_lines)",
    "GmatsMINLine": "deleted with its parent MIN (_purge_gmats_lines)",
    "MachineInstallation": "unlinked, never deleted: it is the OEM's record "
                           "(_unlink_oem_installations)",
}


def purge_tenant_data(db, tenant_code: str) -> dict:
    """Permanently delete every row stamped with ``tenant_code`` across all
    tenant-aware tables (except immutable history). Returns {table: count}.
    Raises ValueError for DEFAULT or blank codes — those are never purgeable."""
    code = (tenant_code or "").strip()
    if not code or code == DEFAULT_TENANT:
        raise ValueError("This tenant cannot be purged")
    # Offboarding also runs as a CLI, without main's boot: the unlink below must
    # still end any service contract's coverage (ADR-0021).
    contract_linkage.install()

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
        # Contracts first: closing them reads the installations' coverage as it
        # stood, and the unlink below then ends coverage through the linkage
        # listener (ADR-0021).
        counts.update(_close_service_contracts(db, code))
        counts.update(_unlink_oem_installations(db, code))
        # Also BEFORE the sweep: the GMATS line tables have no tenant_code, so the
        # sweep cannot delete them, and they block their parents and items.
        counts.update(_purge_gmats_lines(db, code))
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


def _purge_gmats_lines(db, code: str) -> dict:
    """Delete the departing tenant's GMATS lines; detach anyone else's.

    A line belongs to its parent, so the tenant's lines are the lines of the
    tenant's proformas and MINs — found through the parent, the only thing that
    records ownership.

    A line can ALSO reference an item that belongs to a different tenant: the
    GMATS create path resolved items by unscoped id while locking the parent's
    tenant. Such a line is another company's record and is never deleted here.
    Its pointer at an item that is about to be destroyed is set to NULL instead
    (item_id is nullable), which is what lets the item be deleted, and the count
    is reported so the audit entry says it happened.
    """
    counts = {}
    tenant_proformas = select(models.GmatsProforma.id).where(models.GmatsProforma.tenant_code == code)
    tenant_mins = select(models.GmatsMIN.id).where(models.GmatsMIN.tenant_code == code)
    tenant_items = select(models.GmatsItem.id).where(models.GmatsItem.tenant_code == code)

    n = (db.query(models.GmatsProformaLine)
           .filter(models.GmatsProformaLine.proforma_id.in_(tenant_proformas))
           .delete(synchronize_session=False))
    if n:
        counts["gmats_proforma_lines"] = n
    n = (db.query(models.GmatsMINLine)
           .filter(models.GmatsMINLine.min_id.in_(tenant_mins))
           .delete(synchronize_session=False))
    if n:
        counts["gmats_min_lines"] = n

    detached = 0
    for line in (models.GmatsProformaLine, models.GmatsMINLine):
        detached += (db.query(line)
                       .filter(line.item_id.in_(tenant_items))
                       .update({line.item_id: None}, synchronize_session=False))
    if detached:
        counts["gmats_line_foreign_items_detached"] = detached
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


CLOSED_AT_OFFBOARDING = "contract_closed_at_offboarding"


def _close_service_contracts(db, code: str, now=None) -> dict:
    """A departing factory's service contracts close; what was AGREED survives.

    The sweep above cannot see the contract tables (they carry
    `factory_tenant_code`, never `tenant_code`, precisely so that a purge of one
    party cannot delete the other party's copy of a contract it signed). So this
    function owes the behaviour (ADR-0021):

      * a contract on offer (proposed) is withdrawn; an accepted one is
        terminated by the SYSTEM at the first period boundary at or after now
        (never mid-period, never past its end), reason "factory offboarded";
        pending amendments are withdrawn. A draft is the manufacturer's own
        unshared work and is left alone.
      * every statement KEEPS its revision, content hash and acceptances — the
        record of what both parties agreed — and LOSES its canonical content
        and attribution records (the factory's downtime), and every dispute
        (the parties' free text about it) is removed. The engine then refuses
        to recompute over the kept hash (ContentPurged).
      * one audit row per party, inside this transaction.

    Instance deletes and writes only, no bulk statements.
    """
    import canonical
    import contract_periods
    import contract_terms
    import oem_auth
    import platform_routes

    now = canonical.utc_seconds(now or datetime.utcnow())
    SC, TV = models.ServiceContract, models.ServiceContractTermVersion
    CS, CAR, CD = (models.ContractStatement, models.ContractAttributionRecord,
                   models.ContractDispute)
    counts = {"service_contracts_closed": 0, "contract_statement_contents_removed": 0,
              "contract_attribution_records_removed": 0, "contract_disputes_removed": 0}
    contracts = (db.query(SC)
                   .filter(SC.factory_tenant_code == code,
                           SC.status.in_(("proposed", "accepted", "terminated")))
                   .order_by(SC.id.asc()).all())
    for contract in contracts:
        before = contract.status
        statements = (db.query(CS).filter(CS.contract_id == contract.id)
                        .order_by(CS.period_start.asc()).all())
        if contract.status in ("proposed", "accepted"):
            for version in (db.query(TV).filter(TV.contract_id == contract.id,
                                                TV.status == "proposed").all()):
                version.status = "withdrawn"
            if contract.status == "proposed":
                contract.status = "withdrawn"
            else:
                contract.status = "terminated"
                contract.termination_effective_at = _offboarding_boundary(
                    db, contract, statements, now, contract_terms, contract_periods)
                contract.terminated_by_party = "SYSTEM"
                contract.terminated_by = "system:offboarding"
                contract.termination_reason = "factory offboarded"
            contract.updated_at = now
            counts["service_contracts_closed"] += 1
        for statement in statements:
            for record in db.query(CAR).filter(CAR.statement_id == statement.id).all():
                db.delete(record)
                counts["contract_attribution_records_removed"] += 1
            if statement.canonical_json is not None:
                statement.canonical_json = None
                statement.updated_at = now
                counts["contract_statement_contents_removed"] += 1
        for dispute in db.query(CD).filter(CD.contract_id == contract.id).all():
            db.delete(dispute)
            counts["contract_disputes_removed"] += 1
        details = (f"ref={contract.contract_ref} {before} -> {contract.status}: factory "
                   f"offboarded; statement content, records and disputes removed; "
                   f"hashes and acceptances kept")
        for tenant in (contract.factory_tenant_code,
                       oem_auth.sentinel_tenant(contract.oem_code)):
            platform_routes.log_audit(db, "system:offboarding", CLOSED_AT_OFFBOARDING,
                                      "service_contract", contract.id, details,
                                      tenant_code=tenant, commit=False)
    db.flush()
    return {k: v for k, v in counts.items() if v}


def _offboarding_boundary(db, contract, statements, now, contract_terms, contract_periods):
    """The first period boundary at or after now, capped at the contract's end.

    If the stored terms no longer parse (they always do for a contract accepted
    through the API), offboarding must still complete: the latest boundary AMP
    knows for certain — the end of the last closed statement period, or the
    contract start — is used instead."""
    v1 = (db.query(models.ServiceContractTermVersion)
            .filter(models.ServiceContractTermVersion.contract_id == contract.id,
                    models.ServiceContractTermVersion.version == 1).first())
    try:
        boundary = contract_periods.boundary_at_or_after(
            contract_terms.parse(v1.terms_json), contract.starts_at, now)
    except (AttributeError, ValueError, contract_terms.TermsError,
            contract_periods.PeriodError):
        boundary = max([contract.starts_at]
                       + [s.period_end for s in statements if s.period_end <= now])
    return min(boundary, contract.ends_at)

