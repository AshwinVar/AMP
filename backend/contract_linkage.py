"""Coverage of a service contract ends where its installation's link changes (ADR-0021, C8).

WHY
---
When a factory accepts a service contract, AMP snapshots which shop-floor
machine each covered installation is linked to (service_contract_machines.
machine_id_at_acceptance). The statement reads THAT machine's spans and
downtime reasons. If the installation is later unlinked, relinked to another
machine, released, or its factory offboarded, that machine's data stops being
evidence about the covered equipment.

The first design checked linkage at compute time, which turned a whole
unaccepted past period into "no data" when a machine was released a month
later (critic finding C8). Instead the change is DATED when AMP observes it:

  * `coverage_ended_at` = now (whole seconds, UTC) and `coverage_end_reason` on
    every OPEN coverage row of the installation, whose contract is still
    binding and has not ended;
  * two `contract_coverage_ended` audit rows (the factory's tenant and the
    manufacturer's sentinel) and one CoverageEnded event per contract,
  * all IN THE SAME FLUSH as the change, so a failed commit leaves none of it.

Covered time from the stamp on is UNMEASURED, cause `installation_unlinked`.
A stamp is never moved. A physical move AMP was not told about cannot be seen;
that limitation is documented.

HOW
---
`install()` registers ONE before_flush listener on the Session class (called at
boot beside tenancy.install_scoping, and by offboarding, which also runs as a
CLI). It sees instance writes. A bulk UPDATE never reaches before_flush, so a
bulk writer that can change the link must call `end_coverage` itself;
test_contract_linkage.py fails CI for one that does not.
"""
from datetime import datetime

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

import canonical

AUDIT_ACTION = "contract_coverage_ended"
MACHINE_UNLINKED = "machine_unlinked"
MACHINE_RELINKED = "machine_relinked"
INSTALLATION_RELEASED = "installation_released"
FACTORY_CHANGED = "factory_changed"

_LINK_ATTRIBUTES = ("machine_id", "factory_tenant_code")
_installed = [False]


def utcnow():
    """The listener's clock: naive UTC, whole seconds (tests pin it)."""
    return canonical.utc_seconds(datetime.utcnow())


def change_reason(old_machine, new_machine, old_factory, new_factory):
    """Why coverage ends for this link change, or None when the link is unchanged."""
    if old_factory != new_factory:
        return INSTALLATION_RELEASED if new_factory is None else FACTORY_CHANGED
    if old_machine != new_machine:
        return MACHINE_UNLINKED if new_machine is None else MACHINE_RELINKED
    return None


def end_coverage(session, installation_id, reason, now=None):
    """Stamp every open coverage row of one installation. Returns the rows stamped.

    Writes into `session` (the caller's transaction or flush) and never commits.
    Rows whose contract is not binding, or has already ended, are left open:
    no period after the stamp exists for them to change."""
    import contract_periods
    import models
    import oem_auth
    import oem_events
    import platform_routes
    from events import event_bus

    now = canonical.utc_seconds(now or utcnow())
    SCM, TV, SC = (models.ServiceContractMachine, models.ServiceContractTermVersion,
                   models.ServiceContract)
    with session.no_autoflush:
        rows = (session.query(SCM, SC)
                  .join(TV, TV.id == SCM.term_version_id)
                  .join(SC, SC.id == TV.contract_id)
                  .filter(SCM.installation_id == installation_id,
                          SCM.coverage_ended_at.is_(None),
                          SC.status.in_(("accepted", "terminated")))
                  .order_by(SCM.id.asc()).all())
        stamped, contracts = [], {}
        for coverage, contract in rows:
            if now >= contract_periods.contract_effective_end(contract):
                continue
            coverage.coverage_ended_at = now
            coverage.coverage_end_reason = reason
            stamped.append(coverage)
            contracts.setdefault(contract.id, (contract, coverage))
        for contract, coverage in contracts.values():
            details = (f"ref={contract.contract_ref} installation={installation_id} "
                       f"serial={coverage.serial_number} "
                       f"coverage_ended_at={canonical.ts(now)} reason={reason}")
            for tenant in (contract.factory_tenant_code,
                           oem_auth.sentinel_tenant(contract.oem_code)):
                platform_routes.add_audit(session, "system", AUDIT_ACTION, "service_contract",
                                          contract.id, details, tenant_code=tenant)
            oem_events.publish(event_bus, session, oem_events.CoverageEnded(
                tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
                contract_id=contract.id, contract_ref=contract.contract_ref,
                installation_id=installation_id, serial_number=coverage.serial_number,
                coverage_ended_at=canonical.ts(now), reason=reason))
    return stamped


def _link_changes(session):
    """(installation_id, reason) for each dirty installation whose link changed,
    compared with what the DATABASE still holds (not the attribute history,
    which is empty for an expired attribute and would miss or invent a change)."""
    import models

    MI = models.MachineInstallation
    out = []
    for obj in list(session.dirty):
        if not isinstance(obj, MI) or obj.id is None:
            continue
        state = sa_inspect(obj)
        if not any(state.attrs[a].history.has_changes() for a in _LINK_ATTRIBUTES):
            continue
        with session.no_autoflush:
            stored = (session.query(MI.machine_id, MI.factory_tenant_code)
                        .filter(MI.id == obj.id).first())
        if stored is None:
            continue
        reason = change_reason(stored.machine_id, obj.machine_id,
                               stored.factory_tenant_code, obj.factory_tenant_code)
        if reason is not None:
            out.append((obj.id, reason))
    return out


def _before_flush(session, flush_context, instances):
    changes = _link_changes(session)
    if not changes:
        return
    now = utcnow()
    for installation_id, reason in changes:
        end_coverage(session, installation_id, reason, now)


def install():
    """Register the listener once per process. Idempotent."""
    if _installed[0]:
        return
    _installed[0] = True
    event.listen(Session, "before_flush", _before_flush)
