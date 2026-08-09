"""What a factory has agreed to share with an OEM, and the fleet it can see.

THE RULE THIS MODULE EXISTS TO ENFORCE (ADR-0017)
------------------------------------------------
An OEM relationship does NOT grant access to a factory. Selling somebody a
compressor tells you nothing about their order book, their recipes, their
customers or their costs, and AMP must not infer otherwise. So there is no
"the OEM has machines at FACTORY_A, therefore the OEM may query FACTORY_A".

Two independent things must BOTH be true before an OEM sees a field:

    1. the machine is one the OEM actually installed  (an installation row)
    2. that factory has granted that class of data    (a sharing policy)

Neither implies the other. A factory that grants SHARE_ALARMS has still not
granted operating hours; an OEM with fifty machines at a site that has granted
nothing sees fifty machines and no telemetry.

DEFAULT DENY, AND WHY IT IS THE ABSENCE OF A ROW
------------------------------------------------
No policy row means nothing is shared. Not "everything", not "the safe subset" —
nothing beyond what the OEM already knows from having sold the machine: its own
serial, its own model, and which customer site it was shipped to. Those facts
came from the OEM's own records, not from the factory's operations.

READ AT QUERY TIME, NEVER CACHED
--------------------------------
`grants_for` is called on every request that needs it. A cached projection of
shared data would survive the revocation of the policy that permitted it — the
factory would withdraw consent and the OEM would keep reading yesterday's copy.
Recomputing is the difference between "revoked" and "revoked from now on".
"""
import models
import oem_auth

# The grant vocabulary. A factory grants these; nothing else is shareable.
SHARE_MACHINE_HEALTH = "SHARE_MACHINE_HEALTH"
SHARE_OPERATING_HOURS = "SHARE_OPERATING_HOURS"
SHARE_SERVICE_STATUS = "SHARE_SERVICE_STATUS"
SHARE_ALARMS = "SHARE_ALARMS"
SHARE_TELEMETRY = "SHARE_TELEMETRY"
SHARE_MAINTENANCE_HISTORY = "SHARE_MAINTENANCE_HISTORY"
SHARE_DOWNTIME = "SHARE_DOWNTIME"

ALL_GRANTS = (
    SHARE_MACHINE_HEALTH, SHARE_OPERATING_HOURS, SHARE_SERVICE_STATUS,
    SHARE_ALARMS, SHARE_TELEMETRY, SHARE_MAINTENANCE_HISTORY, SHARE_DOWNTIME,
)

# Human-readable, for the factory's own "what am I sharing?" screen. A consent
# control nobody can read is not consent.
GRANT_LABELS = {
    SHARE_MACHINE_HEALTH: "Machine health score and connectivity state",
    SHARE_OPERATING_HOURS: "Operating and loaded hours",
    SHARE_SERVICE_STATUS: "Service due / overdue status",
    SHARE_ALARMS: "Equipment alarm codes raised by this machine",
    SHARE_TELEMETRY: "Live telemetry readings from this machine",
    SHARE_MAINTENANCE_HISTORY: "Maintenance work carried out on this machine",
    SHARE_DOWNTIME: "Downtime events recorded against this machine",
}

# What is visible with NO policy at all. Deliberately only facts the OEM already
# holds in its own records — never anything derived from factory operations.
ALWAYS_VISIBLE = ("serial_number", "model", "status", "customer", "site",
                  "warranty", "commissioned_at", "installed_at")


def parse_grants(raw):
    """CSV -> a set of KNOWN grants. Unknown tokens are dropped, not honoured:
    a typo, or a value written by an older/newer version, must never widen
    access. Fail closed on anything this version does not recognise."""
    if not raw:
        return set()
    return {g.strip() for g in str(raw).split(",")
            if g.strip() in ALL_GRANTS}


def grants_for(db, oem_code, tenant_code):
    """What THIS factory shares with THIS OEM, right now.

    Read by the (oem_code, tenant_code) key with both passed EXPLICITLY. It
    cannot ride the ADR-0002 hook: an OEM request binds a sentinel tenant that
    matches no factory, so the hook would hide the very grant that authorises
    the read and every factory would look as though it shared nothing.
    """
    if not oem_code or not tenant_code:
        return set()
    row = (db.query(models.OemDataSharingPolicy)
             .filter(models.OemDataSharingPolicy.oem_code == oem_code,
                     models.OemDataSharingPolicy.tenant_code == tenant_code)
             .first())
    return parse_grants(row.grants if row else None)


def installations_for(db, oem_code, tenant_code=None, serial=None,
                      installation_id=None):
    """The OEM's OWN installations. The only way into the fleet.

    Always filtered by `oem_code` explicitly. This query does not ride the
    ADR-0002 hook either — MachineInstallation is outside SCOPED_MODELS by
    design (the sentinel would hide the OEM's whole fleet), so the filter here
    IS the boundary. A missing filter is a cross-manufacturer leak, which is why
    oem_code is a required positional argument and never defaults.
    """
    # No early return for a falsy oem_code. `filter(oem_code == "")` and
    # `filter(oem_code == None)` already match nothing (the column is NOT NULL),
    # so a guard here mutation-tested as changing nothing — a comment wearing an
    # if-statement. The FILTER is the guard, which is why it is unconditional.
    q = (db.query(models.MachineInstallation)
           .filter(models.MachineInstallation.oem_code == oem_code))
    if tenant_code is not None:
        q = q.filter(models.MachineInstallation.factory_tenant_code == tenant_code)
    if serial is not None:
        q = q.filter(models.MachineInstallation.serial_number == serial)
    if installation_id is not None:
        q = q.filter(models.MachineInstallation.id == installation_id)
    return q.order_by(models.MachineInstallation.id.asc()).all()


def get_installation(db, oem_code, installation_id):
    """One installation, or None. `None` becomes a 404 at the route — never a
    403: a manufacturer probing ids must not learn that a row exists and belongs
    to a competitor."""
    rows = installations_for(db, oem_code, installation_id=installation_id)
    return rows[0] if rows else None


def visible_machine(db, installation, grants):
    """The factory Machine row behind an installation, or None.

    Requires SHARE_MACHINE_HEALTH: the machine row carries status and
    utilisation, which are operational facts about how the customer is running
    the plant, not facts about the equipment the OEM sold.

    Reads with the tenant bound to the FACTORY, briefly and explicitly. That is
    the sanctioned pattern in this codebase (saas_routes.get_tenant_activity)
    and it is deliberately the only place the OEM layer touches a factory table
    at all: one function, auditable, and gated on a grant that the factory
    controls.
    """
    import tenancy

    if SHARE_MACHINE_HEALTH not in grants:
        return None
    if not installation.machine_id or not installation.factory_tenant_code:
        return None
    token = tenancy.set_current_tenant(installation.factory_tenant_code)
    try:
        machine = (db.query(models.Machine)
                     .filter(models.Machine.id == installation.machine_id)
                     .first())
    finally:
        tenancy.reset_current_tenant(token)
    if machine is None:
        return None
    # BELT AND BRACES, and not redundant. The binding above came from the
    # installation row, so this function is only as trustworthy as whatever
    # wrote `machine_id` and `factory_tenant_code`. If a row ever paired one
    # factory's tenant with another factory's machine id — a bad commissioning
    # write, a botched migration, a hostile create — the query above would bind
    # the tenant that MAKES THAT ROW VISIBLE and hand it over.
    #
    # Re-checking the machine's OWN tenant closes that: the row must agree with
    # itself. The grant says the OEM may see the machine it sold; it never says
    # which machine that is.
    if (machine.tenant_code or "") != installation.factory_tenant_code:
        return None
    return machine


def fleet_row(db, installation, grants, model=None):
    """One machine as its MANUFACTURER may see it.

    Built field by field from the grants. The shape is constant — a caller
    always gets the same keys — but a field the factory has not shared is None
    and carries no value, so "not shared" and "no data" are distinguishable by
    the `shared` block rather than by a missing key. A UI that cannot tell those
    apart shows "0 hours" for a machine whose owner simply declined to say.
    """
    row = {
        # Facts from the OEM's OWN records. Visible with no policy at all.
        "installation_id": installation.id,
        "serial_number": installation.serial_number,
        "model_code": getattr(model, "model_code", None),
        "model_name": getattr(model, "name", None),
        "customer": installation.factory_tenant_code,
        "site": installation.site or "",
        "lifecycle_status": installation.status,
        "installed_at": _iso(installation.installed_at),
        "commissioned_at": _iso(installation.commissioned_at),
        "warranty_start": _iso(installation.warranty_start),
        "warranty_end": _iso(installation.warranty_end),
        # Everything below is the FACTORY's to grant.
        "operating_hours": None,
        "last_seen_at": None,
        "machine_status": None,
        "utilization": None,
        "shared": sorted(grants),
    }
    if SHARE_OPERATING_HOURS in grants:
        row["operating_hours"] = installation.operating_hours
    if SHARE_MACHINE_HEALTH in grants:
        row["last_seen_at"] = _iso(installation.last_seen_at)
        machine = visible_machine(db, installation, grants)
        if machine is not None:
            row["machine_status"] = machine.status
            row["utilization"] = machine.utilization
    return row


def _iso(value):
    return value.isoformat() if value else None
