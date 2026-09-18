"""What the FACTORY is told when its supplier touches its machine (ADR-0017).

An OEM commissioning or servicing equipment on somebody's shop floor is a thing
that happened to THEIR asset. They should not have to hear about it from the
manufacturer, and they should not have to go looking. So each lifecycle event
raises a notification in the customer's own workspace.

DIRECTION MATTERS, AND BOTH DIRECTIONS ARE NOW SAFE.
The factory is told about its own machine — data it already owns (ADR-0002), so
no sharing grant is involved or needed.

AN EARLIER VERSION OF THIS FILE SAID THE OTHER DIRECTION WAS IMPOSSIBLE: "an
OEM-scoped notification store does not exist and inventing one on the factory's
notifications table would put manufacturer rows inside a customer's tenant".
That was wrong, and ADR-0019 corrects it. `Notification` is in SCOPED_MODELS, so
a row stamped with the OEM SENTINEL `OEM:<code>` is filtered to that
manufacturer's sessions by the ordinary ADR-0002 hook and is invisible to every
factory and to the founder. The existing table, the existing mechanism, no new
concept — the sentinel made it work all along.

What still does not cross: a factory notification never names another customer,
and an OEM notification never carries anything the sharing policy withholds.

WHAT IS IN THE MESSAGE.
The serial, the model, the site, and what happened. Nothing about production,
orders, or anything else the OEM might have seen — the notification describes
the OEM's action, not the factory's business, so it cannot become a back-channel
for data the sharing policy withholds.
"""
import models
import oem_auth
from events import event_bus
from oem_events import (AmendmentProposed, ContractAccepted, ContractProposed,
                        CoverageEnded, DisputeRaised, DisputeResolved,
                        MachineClaimed, MachineCommissioned, MachineInstalled,
                        ServiceCompleted, StatementAgreed, StatementComputed)


def _notify(db, tenant_code, kind, severity, title, message):
    """One notification in the FACTORY's tenant.

    tenant_code is passed explicitly rather than left to the ORM's stamping
    hook: a subscriber can run outside the request that produced the event, so
    an ambient binding is exactly the wrong thing to rely on (the ingest defect
    ADR-0011 was written about).
    """
    db.add(models.Notification(
        tenant_code=tenant_code,
        notification_type=kind,
        severity=severity,
        title=title,
        message=message,
        status="Unread",
    ))


def notify_factory_of_installation(event: MachineInstalled, db) -> None:
    _notify(
        db, event.tenant_code, "equipment_installed", "Info",
        f"{event.serial_number} installed",
        f"{event.oem_code} recorded machine {event.serial_number}"
        + (f" ({event.model_code})" if event.model_code else "")
        + f" as installed at {event.site or 'your site'}.",
    )


def notify_factory_of_commissioning(event: MachineCommissioned, db) -> None:
    # An incomplete commissioning is raised as a WARNING, not hidden. The
    # checks are advice and a machine may go into service with one outstanding —
    # but the customer is the party who lives with the consequence, so they are
    # the party who gets told.
    incomplete = not event.checks_passed
    _notify(
        db, event.tenant_code, "equipment_commissioned",
        "Warning" if incomplete else "Info",
        f"{event.serial_number} commissioned"
        + (" (checks outstanding)" if incomplete else ""),
        f"{event.oem_code} commissioned machine {event.serial_number} at "
        f"{event.site or 'your site'}."
        + (" One or more commissioning checks did not pass — ask your supplier "
           "which, and whether it matters." if incomplete else ""),
    )


def notify_factory_of_service(event: ServiceCompleted, db) -> None:
    hours = (f" at {round(event.service_hours, 1)} operating hours"
             if event.service_hours is not None else "")
    _notify(
        db, event.tenant_code, "equipment_serviced", "Info",
        f"{event.serial_number} serviced",
        f"{event.oem_code} recorded a completed service on machine "
        f"{event.serial_number}{hours}.",
    )


def notify_both_parties_of_claim(event: MachineClaimed, db) -> None:
    """A claim is the one moment BOTH sides are waiting on, so both are told.

    The factory learns its equipment is registered; the manufacturer learns its
    machine reached the customer and can be commissioned. Two rows, one per
    party, in two different tenants — and neither can read the other's.
    """
    granted = [g for g in (event.granted or "").split(",") if g]
    _notify(
        db, event.tenant_code, "equipment_claimed", "Info",
        f"{event.serial_number} added to your connected equipment",
        f"You accepted machine {event.serial_number}"
        + (f" ({event.model_code})" if event.model_code else "")
        + f" from {event.oem_code}. "
        + (f"You are sharing {len(granted)} of 7 data categories with them; "
           "you can change that at any time under Connected Equipment."
           if granted else
           "You are sharing nothing with them; you can change that at any time "
           "under Connected Equipment."),
    )
    # The manufacturer's copy, stamped with ITS sentinel tenant so the ADR-0002
    # hook shows it to that OEM and to nobody else.
    _notify(
        db, oem_auth.sentinel_tenant(event.oem_code), "installation_accepted",
        "Info",
        f"{event.serial_number} accepted by {event.tenant_code}",
        f"Machine {event.serial_number} was accepted by {event.tenant_code}. "
        "Commissioning can begin.",
    )


# ── Service contracts (ADR-0021) ──────────────────────────────────────
#
# A contract has two parties and every step waits on one of them, so every
# notification goes to BOTH: once in the factory's tenant, once in the
# manufacturer's sentinel tenant, exactly as a claim does.
#
# The message names the contract, the period and what happened. It never
# carries a total, an availability, a credit or a reason: an OEM notification
# is readable whether or not the factory still shares downtime, so a figure in
# it would outlive a withdrawal of consent.


def _notify_both(db, event, kind, title, message):
    _notify(db, event.tenant_code, kind, "Info", title, message)
    _notify(db, oem_auth.sentinel_tenant(event.oem_code), kind, "Info", title,
            message)


def notify_contract_proposed(event: ContractProposed, db) -> None:
    _notify_both(
        db, event, "contract_proposed",
        f"Service contract {event.contract_ref} proposed",
        f"{event.oem_code} proposed service contract {event.contract_ref}"
        + (f" ({event.title})" if event.title else "")
        + f" to {event.tenant_code}. Nothing is measured until "
          f"{event.tenant_code} accepts the terms.")


def notify_contract_accepted(event: ContractAccepted, db) -> None:
    _notify_both(
        db, event, "contract_accepted",
        f"Service contract {event.contract_ref} accepted",
        f"{event.tenant_code} accepted service contract {event.contract_ref} "
        f"from {event.oem_code}, covering {event.installations} machine(s), and "
        "agreed to share downtime for the attribution statements. Sharing can "
        "be withdrawn under Connected Equipment.")


def notify_amendment_proposed(event: AmendmentProposed, db) -> None:
    _notify_both(
        db, event, "amendment_proposed",
        f"Amendment {event.version} to {event.contract_ref} proposed",
        f"{event.proposed_by_party} proposed version {event.version} of the "
        f"terms of {event.contract_ref}, effective {event.effective_from}. It "
        "applies only when the other party accepts the same terms.")


def notify_statement_computed(event: StatementComputed, db) -> None:
    _notify_both(
        db, event, "statement_computed",
        f"{event.contract_ref}: statement for {event.period_start[:10]} updated",
        f"The downtime attribution statement for {event.contract_ref}, period "
        f"starting {event.period_start}, is now at revision {event.revision}. "
        "Any earlier acceptance no longer counts.")


def notify_statement_agreed(event: StatementAgreed, db) -> None:
    _notify_both(
        db, event, "statement_agreed",
        f"{event.contract_ref}: statement for {event.period_start[:10]} agreed",
        f"Both parties accepted revision {event.revision} of the downtime "
        f"attribution statement for {event.contract_ref}, period starting "
        f"{event.period_start}. It is now final.")


def notify_dispute_raised(event: DisputeRaised, db) -> None:
    _notify_both(
        db, event, "dispute_raised",
        f"{event.contract_ref}: dispute #{event.dispute_id} raised",
        f"{event.raised_by_party} disputed part of a downtime attribution "
        f"statement under {event.contract_ref}. The statement cannot be agreed "
        "until the dispute is resolved or withdrawn.")


def notify_dispute_resolved(event: DisputeResolved, db) -> None:
    _notify_both(
        db, event, "dispute_resolved",
        f"{event.contract_ref}: dispute #{event.dispute_id} resolved",
        f"Both parties agreed how the disputed window under {event.contract_ref} "
        "is attributed. The statement has been recomputed and must be accepted "
        "again.")


def notify_coverage_ended(event: CoverageEnded, db) -> None:
    _notify_both(
        db, event, "coverage_ended",
        f"{event.contract_ref}: coverage of {event.serial_number} ended",
        f"Machine {event.serial_number} is no longer linked to the machine "
        f"{event.contract_ref} was accepted for (at {event.coverage_ended_at}). "
        "Covered time from then on is reported as No data.")


def register(bus=event_bus) -> None:
    """Wire the OEM subscribers. Called once at startup, beside subscribers.register."""
    bus.subscribe(MachineInstalled, notify_factory_of_installation)
    bus.subscribe(MachineCommissioned, notify_factory_of_commissioning)
    bus.subscribe(ServiceCompleted, notify_factory_of_service)
    bus.subscribe(MachineClaimed, notify_both_parties_of_claim)
    bus.subscribe(ContractProposed, notify_contract_proposed)
    bus.subscribe(ContractAccepted, notify_contract_accepted)
    bus.subscribe(AmendmentProposed, notify_amendment_proposed)
    bus.subscribe(StatementComputed, notify_statement_computed)
    bus.subscribe(StatementAgreed, notify_statement_agreed)
    bus.subscribe(DisputeRaised, notify_dispute_raised)
    bus.subscribe(DisputeResolved, notify_dispute_resolved)
    bus.subscribe(CoverageEnded, notify_coverage_ended)
