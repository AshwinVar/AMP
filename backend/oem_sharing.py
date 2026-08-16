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


# ── Service intelligence, as the manufacturer may see it ─────────────
#
# `oem_service` is ARITHMETIC and knows nothing about consent — deliberately, and
# it stays that way. These functions are where its output meets the policy, so
# there is still exactly one module that decides what a factory has agreed to.
#
# WHY THIS EXISTS AT ALL. `/oem/fleet` withheld `operating_hours` correctly while
# `/oem/service` printed the same number in prose on the same screen, for a
# manufacturer whose customer had switched that grant off — and kept printing it
# with every grant withdrawn. Two deliberate decisions had been made about one
# column: the service routes argued the hour meter is the OEM's own record and
# needs no grant, `fleet_row` gated it. Only one could stand, and the factory's
# screen decides which: it shows a checkbox labelled "Operating and loaded hours"
# under the promise "Nothing is shared until you share it".
#
# WHAT EACH GRANT BUYS, and why the split is where it is:
#
#   SHARE_SERVICE_STATUS   the VERDICT — ok / due_soon / due / overdue, and the
#                          recommendation to act on it. Exactly its label,
#                          "Service due / overdue status".
#   SHARE_OPERATING_HOURS  the FIGURES — hours run, hours since service, hours
#                          remaining. `remaining` is gated even though the
#                          interval is the OEM's own, because interval minus
#                          remaining IS the hours since service.
#   SHARE_MACHINE_HEALTH   whether and when the machine last reported. Its label
#                          says "connectivity state" and `fleet_row` already
#                          treats `last_seen_at` as the gated datum.
#   (no grant)             the serial, the model, the service INTERVAL and the
#                          warranty dates. Those are the OEM's own records
#                          (ALWAYS_VISIBLE) and a customer's checkbox must never
#                          hide a manufacturer's own paperwork from it.
#
# ALLOWLIST, NOT REDACTION. Each function builds a NEW dict and copies fields in
# once a grant permits them, exactly as `fleet_row` does. Filtering the arithmetic
# module's output instead would fail OPEN: the day somebody adds a field to
# `service_state`, a filter does not know to remove it and a copier does not know
# to add it. The second mistake is a missing feature; the first is a disclosure.


def service_view(state, grants, model):
    """`oem_service.service_state()` as this manufacturer may see it.

    The interval is re-derived from the MODEL, never copied out of `state`.
    Copying it looked harmless — it is the OEM's own number either way — but
    `service_state` omits the key entirely on the branch where the machine has
    never reported hours, and includes it on every other. So the field's
    PRESENCE, rather than its value, carried one bit of the withheld meter:
    whether the customer's machine has ever reported an hours reading at all.
    `fleet_row` is byte-identical whether the meter holds 4600.0 or NULL, and
    this route has to be too. It is the allowlist discipline in the block above,
    applied to the one field that was quietly exempt from it.

    WHY `state: "unknown"` MAY STILL STAND, when the interval's presence may
    not. Both carry the same fact — that no usable hours reading exists — so the
    difference is not what they reveal but what they claim to be.
    `interval_hours` is documented as the manufacturer's own paperwork, a number
    that cannot legitimately move with the customer's data; a bit riding in it is
    a covert channel, and a reader has no reason to look for one. `state` IS the
    granted datum, and "unknown" is one of its honest values: the factory asked
    AMP to report service position, and "I cannot compute it" is the true answer.
    Replacing it with a confident "ok" to save the bit would be the fabrication
    ADR-0014 exists to forbid. Do not "fix" this by collapsing it.
    """
    interval = getattr(model, "service_interval_hours", None)
    if SHARE_SERVICE_STATUS not in grants:
        # NOT "unknown". `unknown` already means "this machine has never
        # reported hours", and a service desk that cannot tell that apart from
        # "the customer declined to say" will chase a gateway that is fine.
        return {"state": "not_shared", "hours_remaining": None,
                "interval_hours": interval, "hours_since_service": None,
                "reason": "this customer has not shared service status"}
    out = {"state": state.get("state"), "hours_remaining": None,
           "interval_hours": interval, "hours_since_service": None,
           "reason": state.get("reason")}
    if SHARE_OPERATING_HOURS in grants:
        out["hours_remaining"] = state.get("hours_remaining")
        out["hours_since_service"] = state.get("hours_since_service")
        return out
    # The verdict stands; the arithmetic behind it does not. The reason has to be
    # rewritten rather than filtered, because the figures are inside its prose —
    # which is where the whole leak lived.
    out["reason"] = _service_reason_without_hours(state.get("state"), interval)
    return out


def _service_reason_without_hours(state, interval):
    every = f"every {interval} h" if interval else "on its service interval"
    return {
        "overdue": f"past its service interval ({every}); the operating-hour "
                   f"figures are not shared by this customer",
        "due": f"at its service interval ({every}); the operating-hour figures "
               f"are not shared by this customer",
        "due_soon": f"approaching its service interval ({every}); the "
                    f"operating-hour figures are not shared by this customer",
        "ok": f"not due for service ({every}); the operating-hour figures are "
              f"not shared by this customer",
    }.get(state, "this customer has not shared the operating-hour figures")


def commissioning_view(report, grants):
    """`oem_service.commissioning_report()` as this manufacturer may see it.

    Only the `has_reported` check moves: whether a machine has ever reported is
    connectivity, and connectivity is SHARE_MACHINE_HEALTH's to give.

    It becomes `passed: None`, never `False`. Reporting an ungranted check as
    failed would tell a manufacturer its commissioning did not complete when it
    may well have — and this module's whole point is that "not shared" and "not
    true" are different answers. `ready` goes to None with it, because a
    confident `ready: true` would disclose the very boolean that was withheld.

    `machine_id` in the `linked_to_machine` detail stays. It is a column on the
    OEM's OWN installation row, it is an opaque integer, and no grant in
    GRANT_LABELS names it. Reading the machine BEHIND that id still needs
    SHARE_MACHINE_HEALTH (`visible_machine`), which is where the operational
    facts actually are.
    """
    if SHARE_MACHINE_HEALTH in grants:
        return report
    checks = []
    for c in report.get("checks", []):
        if c.get("key") == "has_reported":
            c = {**c, "passed": None,
                 "detail": "this customer has not shared connectivity state"}
        checks.append(c)
    return {"ready": None, "checks": checks}


# What each recommendation needs before a manufacturer may be shown it. A kind
# that is NOT listed needs no grant — the warranty items are the OEM's own
# records. Listing the requirement rather than hard-coding a chain of ifs is what
# lets `visible_recommendations` stay four lines and be obviously right.
RECOMMENDATION_GRANTS = {
    "service_due": SHARE_SERVICE_STATUS,
    "service_projection": SHARE_SERVICE_STATUS,
    "not_reporting": SHARE_MACHINE_HEALTH,
}


def visible_recommendations(recs, grants):
    """The recommendations this manufacturer may be shown, with their figures.

    A `service_due` item survives on SHARE_SERVICE_STATUS alone, because that is
    what the factory agreed to — but its `evidence` is the hour arithmetic, so
    without SHARE_OPERATING_HOURS the item keeps its verdict and loses its
    numbers. Dropping the item entirely would have been easier and would have
    withheld something the customer explicitly granted.
    """
    out = []
    for rec in recs:
        needed = RECOMMENDATION_GRANTS.get(rec.get("kind"))
        if needed and needed not in grants:
            continue
        if (rec.get("kind") in ("service_due", "service_projection")
                and SHARE_OPERATING_HOURS not in grants):
            rec = {**rec, "evidence": "the operating-hour figures are not "
                                      "shared by this customer"}
            # A projection IS the hours trend — "1.4 h/day over 30 days" is the
            # meter twice. Without the figures there is no honest way to state
            # it, so the action goes too rather than standing unexplained.
            if rec["kind"] == "service_projection":
                continue
        out.append(rec)
    return out


def fleet_recommendations(db, oem_code, installations, models_by_id, today=None):
    """Every recommendation across a fleet, as this manufacturer may see it.

    THE ONLY fleet-wide entry point, and it cannot be called without consulting
    consent — `db` and `oem_code` are required positionally for exactly that
    reason. The previous version of this function lived in `oem_service`, took no
    grants at all, and was what `/oem/service` called.

    Grants are read once per CUSTOMER: a thousand machines at four sites is four
    policy reads, as on the fleet screen.
    """
    import oem_service

    cache, out = {}, []
    for inst in installations:
        tenant = inst.factory_tenant_code
        if tenant not in cache:
            cache[tenant] = grants_for(db, oem_code, tenant)
        out.extend(visible_recommendations(
            oem_service.recommendations(inst, models_by_id.get(inst.model_id),
                                        today),
            cache[tenant]))
    return oem_service.by_severity(out)


def _iso(value):
    return value.isoformat() if value else None
