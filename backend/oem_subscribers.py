"""What the FACTORY is told when its supplier touches its machine (ADR-0017).

An OEM commissioning or servicing equipment on somebody's shop floor is a thing
that happened to THEIR asset. They should not have to hear about it from the
manufacturer, and they should not have to go looking. So each lifecycle event
raises a notification in the customer's own workspace.

DIRECTION MATTERS, AND ONLY ONE DIRECTION IS SAFE HERE.
The factory is told about its own machine — data it already owns (ADR-0002), so
no sharing grant is involved or needed. Nothing flows the other way: there is no
notification to the OEM, because an OEM-scoped notification store does not exist
and inventing one on the factory's `notifications` table would put manufacturer
rows inside a customer's tenant. That is left undone deliberately rather than
approximated.

WHAT IS IN THE MESSAGE.
The serial, the model, the site, and what happened. Nothing about production,
orders, or anything else the OEM might have seen — the notification describes
the OEM's action, not the factory's business, so it cannot become a back-channel
for data the sharing policy withholds.
"""
import models
from events import event_bus
from oem_events import MachineCommissioned, MachineInstalled, ServiceCompleted


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


def register(bus=event_bus) -> None:
    """Wire the OEM subscribers. Called once at startup, beside subscribers.register."""
    bus.subscribe(MachineInstalled, notify_factory_of_installation)
    bus.subscribe(MachineCommissioned, notify_factory_of_commissioning)
    bus.subscribe(ServiceCompleted, notify_factory_of_service)
