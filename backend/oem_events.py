"""OEM equipment-lifecycle events (ADR-0017 + ADR-0001).

WHY THESE EXIST AT ALL, AND WHY ONLY NOW
----------------------------------------
ADR-0017 specified five OEM events. Until this module they had no PRODUCER:
commissioning and service were read-only reports, so nothing ever happened that
an event could describe. Publishing them anyway would have been dead code
pretending to be an integration point. They arrive with the write endpoints that
cause them, and not before.

THE TENANCY RULE, WHICH IS THE WHOLE DESIGN
-------------------------------------------
events.EventBus appends every published event to `event_log`, stamped with

    tenant_code = getattr(event, "tenant_code", "DEFAULT")

That default is a trap for an OEM event. An installation that has not been
assigned to a customer belongs to nobody's factory, and stamping it "DEFAULT"
would file a manufacturer's private record in the FOUNDER's workspace, where a
completely unrelated party reads their event log.

So these events carry the FACTORY's tenant, and `publish` REFUSES to publish
without one. The manufacturer's own pre-sale history is not the factory event
log's business, and there is no tenant that can honestly own it.

Why the factory tenant and not the OEM's: the event records something that
happened on the CUSTOMER's shop floor to the CUSTOMER's machine. They own that
history — ADR-0002 — and it is theirs to read, export and retain. The OEM's code
travels on the event as a field, not as its owner.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class MachineInstalled:
    """An OEM recorded that a machine it made is now installed at a customer."""
    tenant_code: str                  # THE FACTORY. See the module docstring.
    oem_code: str
    installation_id: int
    serial_number: str
    site: str = ""
    model_code: Optional[str] = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "MachineInstalled"
    event_version: int = 1


@dataclass(frozen=True)
class MachineClaimed:
    """A factory accepted a machine into its workspace (ADR-0019).

    ONE new event, because one new fact occurs. Claim created / revoked /
    expired are NOT here: there is no factory yet, so the fact belongs to
    nobody's shop floor and EventBus would stamp it DEFAULT — the founder's
    workspace. Those are audited instead, which is where they belong.
    """
    tenant_code: str                  # THE ACCEPTING FACTORY.
    oem_code: str
    installation_id: int
    serial_number: str
    model_code: Optional[str] = None
    # What the factory chose AT THE MOMENT of acceptance, so the history records
    # what was actually agreed rather than whatever the policy says today.
    granted: str = ""
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "MachineClaimed"
    event_version: int = 1


@dataclass(frozen=True)
class MachineCommissioned:
    """Commissioning completed: the machine is in service at the customer."""
    tenant_code: str
    oem_code: str
    installation_id: int
    serial_number: str
    site: str = ""
    model_code: Optional[str] = None
    # Whether every commissioning check passed. Recorded because commissioning
    # is ADVICE, not a gate (oem_service.commissioning_report) — a machine can
    # legitimately go into service with a check outstanding, and the history
    # should say which happened rather than implying every one was clean.
    checks_passed: bool = True
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "MachineCommissioned"
    event_version: int = 1


@dataclass(frozen=True)
class ServiceCompleted:
    """A service was carried out and the hours clock reset against it."""
    tenant_code: str
    oem_code: str
    installation_id: int
    serial_number: str
    # The hours reading the service was recorded AT. This is the number that
    # makes `overdue` reachable at all (migration 0007) — without it, service
    # position falls back to assuming every service happened on schedule.
    service_hours: Optional[float] = None
    site: str = ""
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "ServiceCompleted"
    event_version: int = 1


def publish(bus, db, event):
    """Publish an OEM event, or decline and say why.

    Returns True when published. Declines — rather than raising — because the
    lifecycle write is the user's action and the event is a consequence of it:
    an unassigned machine being transitioned is a legitimate operation, and
    failing it because there is no factory to file the history under would be
    the tail wagging the dog. The caller's write still commits.
    """
    if not getattr(event, "tenant_code", None):
        return False
    bus.publish(event, db)
    return True
