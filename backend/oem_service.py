"""Commissioning, warranty, and service intelligence (ADR-0017).

SERVICE INTELLIGENCE, NOT PREDICTIVE MAINTENANCE
------------------------------------------------
Everything here is an arithmetic rule over facts the machine reported. It is
called Service Intelligence and never "predictive maintenance", because no model
was trained and nothing is predicted. Saying otherwise would be the same
fabrication this platform removed from OEE (ADR-0014): a confident number nobody
can justify is worse than no number.

Every recommendation therefore carries what produced it:

    reason      the rule, in words
    evidence    the numbers it used
    confidence  ONLY where it means something (see below)
    machine     the serial, not a display name
    action      what to actually do

CONFIDENCE IS OMITTED, NOT INVENTED
-----------------------------------
"Service due in 84 hours" from an hours counter is arithmetic — it has no
meaningful confidence, and attaching "0.9" to it would be decoration. A
projection from an hours-per-day TREND does have one, because it depends on how
much history the trend was drawn from. So `confidence` is present only on
projections, and its value is derived from the sample, never chosen.

THE LIFECYCLE
-------------
    Manufactured -> Sold -> Assigned -> Installed -> Commissioning
                 -> Active -> Service -> Decommissioned

Transitions are explicit. An installation cannot jump from Manufactured to
Active: commissioning is where somebody confirms the machine is actually
connected and reporting, and skipping it is how a fleet fills with machines
nobody has ever verified.
"""
from datetime import date, datetime, timedelta

# The state machine. A transition not listed here does not exist.
LIFECYCLE = {
    "Manufactured": ("Sold", "Decommissioned"),
    "Sold": ("Assigned", "Decommissioned"),
    "Assigned": ("Installed", "Decommissioned"),
    "Installed": ("Commissioning", "Decommissioned"),
    "Commissioning": ("Active", "Installed", "Decommissioned"),
    "Active": ("Service", "Decommissioned"),
    "Service": ("Active", "Decommissioned"),
    "Decommissioned": (),
}

# What must be true before an installation may go Active. Each is a FACT about
# the machine, not a checkbox somebody ticks.
COMMISSIONING_CHECKS = (
    ("assigned_to_customer", "The installation names a customer and site"),
    ("linked_to_machine", "The serial is linked to a machine in that factory"),
    ("telemetry_profile", "The model defines what this machine reports"),
    ("has_reported", "The machine has reported at least once"),
)


class LifecycleError(ValueError):
    """A transition the state machine does not allow."""


def can_transition(current, target):
    return target in LIFECYCLE.get(current or "", ())


def transition(installation, target, when=None):
    """Move an installation along the lifecycle, or raise.

    Raises rather than silently ignoring: a caller that believes it commissioned
    a machine, and did not, will report a fleet that does not exist.
    """
    current = installation.status or "Manufactured"
    if current == target:
        return installation
    if not can_transition(current, target):
        raise LifecycleError(
            f"cannot go from {current} to {target}; "
            f"allowed: {', '.join(LIFECYCLE.get(current, ())) or 'nothing'}")
    now = when or datetime.utcnow()
    installation.status = target
    if target == "Installed":
        installation.installed_at = installation.installed_at or now
    elif target == "Active" and current == "Commissioning":
        installation.commissioned_at = installation.commissioned_at or now
    elif target == "Decommissioned":
        installation.decommissioned_at = now
    return installation


def commissioning_report(installation, model, profile):
    """Is this machine ready to go Active, and if not, exactly what is missing.

    Returns {"ready": bool, "checks": [{key, description, passed, detail}]}.
    A failed check names the missing FACT, so an engineer on site can fix it
    rather than guessing which of four things went wrong.
    """
    checks = []

    assigned = bool(installation.factory_tenant_code)
    checks.append(("assigned_to_customer", assigned,
                   installation.factory_tenant_code or "no customer assigned"))

    linked = bool(installation.machine_id)
    checks.append(("linked_to_machine", linked,
                   f"machine_id={installation.machine_id}" if linked
                   else "not linked to a machine in the factory"))

    has_profile = bool(profile)
    checks.append(("telemetry_profile", has_profile,
                   f"{len(profile)} signals" if has_profile
                   else f"model {getattr(model, 'model_code', '?')} defines no "
                        f"telemetry profile"))

    reported = installation.last_seen_at is not None
    checks.append(("has_reported", reported,
                   installation.last_seen_at.isoformat() if reported
                   else "never reported"))

    described = dict(COMMISSIONING_CHECKS)
    out = [{"key": k, "description": described[k], "passed": p, "detail": d}
           for k, p, d in checks]
    return {"ready": all(c["passed"] for c in out), "checks": out}


def warranty_state(installation, today=None):
    """Where this machine stands on warranty. Never guesses a period.

    An installation with no warranty_end is "unknown", NOT "expired" and not
    "active": the OEM has not recorded one, and inventing either answer decides a
    commercial question on the customer's behalf.
    """
    today = today or datetime.utcnow().date()
    start, end = installation.warranty_start, installation.warranty_end
    if end is None:
        return {"state": "unknown", "days_remaining": None,
                "reason": "no warranty end date recorded for this installation"}
    if start and today < start:
        return {"state": "not_started", "days_remaining": (end - today).days,
                "reason": f"warranty begins {start.isoformat()}"}
    days = (end - today).days
    if days < 0:
        return {"state": "expired", "days_remaining": days,
                "reason": f"expired {abs(days)} days ago ({end.isoformat()})"}
    return {"state": "active", "days_remaining": days,
            "reason": f"expires {end.isoformat()}"}


def service_state(installation, model, today=None):
    """Service position from the hours counter. Arithmetic, and says so.

    A model with no `service_interval_hours` is "not configured" — the honest
    answer. Assuming a default interval would schedule somebody's van against a
    number this platform invented.
    """
    interval = getattr(model, "service_interval_hours", None)
    hours = installation.operating_hours
    if not interval:
        return {"state": "not_configured", "hours_remaining": None,
                "reason": f"model {getattr(model, 'model_code', '?')} defines no "
                          f"service interval"}
    if hours is None:
        return {"state": "unknown", "hours_remaining": None,
                "reason": "this machine has never reported operating hours"}

    # Hours since the LAST RECORDED SERVICE, not `hours % interval`. The modulo
    # form assumes every service happened exactly on schedule, which makes
    # `overdue` unreachable: a machine at 2,100 h against a 2,000 h interval that
    # was NEVER serviced would report 1,900 h remaining instead of 100 h overdue.
    last = getattr(installation, "last_service_hours", None)
    since = hours - (last or 0.0)
    if since < 0:
        # The counter reads below the last recorded service: a replaced
        # controller, or a counter reset. Say so rather than reporting a
        # negative interval as if it were fresh.
        return {"state": "unknown", "hours_remaining": None,
                "interval_hours": interval,
                "reason": f"the hours counter ({round(hours, 1)} h) reads below "
                          f"the last recorded service ({round(last, 1)} h) — the "
                          f"counter was probably reset or the controller replaced"}
    remaining = interval - since
    if remaining <= 0:
        state = "overdue"
    elif remaining <= interval * 0.05:
        state = "due"
    elif remaining <= interval * 0.15:
        state = "due_soon"
    else:
        state = "ok"
    serviced = ("never serviced" if last is None
                else f"last serviced at {round(last, 1)} h")
    return {"state": state, "hours_remaining": round(remaining, 1),
            "interval_hours": interval, "hours_since_service": round(since, 1),
            "reason": f"{round(hours, 1)} h run, {serviced}, {interval} h "
                      f"interval, {round(remaining, 1)} h to the next service"}


def recommendations(installation, model, today=None, history=None):
    """Explainable service recommendations for one machine.

    `history` is an optional list of (date, operating_hours) samples. With fewer
    than three, no projection is offered — a trend drawn from two points is a
    line through noise, and dressing it up with a confidence figure would be the
    dishonesty this module exists to avoid.
    """
    today = today or datetime.utcnow().date()
    out = []
    serial = installation.serial_number

    svc = service_state(installation, model, today)
    if svc["state"] in ("due", "overdue"):
        out.append({
            "kind": "service_due",
            "severity": "high" if svc["state"] == "overdue" else "medium",
            "machine": serial,
            "reason": ("This machine has passed its service interval."
                       if svc["state"] == "overdue"
                       else "This machine is within 5% of its service interval."),
            "evidence": svc["reason"],
            "action": "Schedule a service visit",
            "confidence": None,          # arithmetic, not a prediction
            "at": datetime.utcnow().isoformat(),
        })

    war = warranty_state(installation, today)
    if war["state"] == "active" and (war["days_remaining"] or 0) <= 60:
        out.append({
            "kind": "warranty_expiring",
            "severity": "low",
            "machine": serial,
            "reason": "Warranty ends within 60 days.",
            "evidence": war["reason"],
            "action": "Contact the customer about renewal or a service contract",
            "confidence": None,
            "at": datetime.utcnow().isoformat(),
        })
    elif war["state"] == "unknown":
        out.append({
            "kind": "warranty_unknown",
            "severity": "low",
            "machine": serial,
            "reason": "No warranty period is recorded for this installation.",
            "evidence": war["reason"],
            "action": "Record the warranty dates so cover can be reported",
            "confidence": None,
            "at": datetime.utcnow().isoformat(),
        })

    # Connectivity: an installation that stops reporting is either broken or
    # disconnected, and the OEM cannot tell which from here — so the wording
    # says exactly that.
    if installation.last_seen_at is not None:
        silent_days = (datetime.utcnow() - installation.last_seen_at).days
        if silent_days >= 2:
            out.append({
                "kind": "not_reporting",
                "severity": "medium",
                "machine": serial,
                "reason": "This machine has not reported recently. It is either "
                          "switched off, disconnected, or faulty — this cannot "
                          "be told apart from here.",
                "evidence": f"last reported {silent_days} days ago "
                            f"({installation.last_seen_at.isoformat()})",
                "action": "Check connectivity with the customer before travelling",
                "confidence": None,
                "at": datetime.utcnow().isoformat(),
            })

    proj = project_service_date(installation, model, history, today)
    if proj:
        out.append(proj)
    return out


def project_service_date(installation, model, history, today=None):
    """When the hours counter will reach the next service, IF usage continues.

    Requires at least three samples spanning at least a day. Below that there is
    no trend to draw, and this returns None rather than a number with a made-up
    error bar.

    `confidence` here is derived from the sample — how many points, over how long
    — and is the only place in this module that carries one.
    """
    today = today or datetime.utcnow().date()
    interval = getattr(model, "service_interval_hours", None)
    if not interval or not history or len(history) < 3:
        return None
    samples = sorted((d, h) for d, h in history if d and h is not None)
    if len(samples) < 3:
        return None
    (first_day, first_hours), (last_day, last_hours) = samples[0], samples[-1]
    span_days = (last_day - first_day).days
    if span_days < 1:
        return None
    gained = last_hours - first_hours
    if gained <= 0:
        return None
    per_day = gained / span_days
    svc = service_state(installation, model, today)
    remaining = svc.get("hours_remaining")
    if remaining is None:
        return None
    days_out = remaining / per_day
    if days_out > 180:
        return None                       # too far out to be worth saying

    # Confidence from the SAMPLE: more points over more days is firmer. Capped
    # at 0.85 — this is a straight line through an hours counter, not a model.
    confidence = min(0.85, 0.35 + 0.05 * len(samples) + 0.01 * span_days)
    return {
        "kind": "service_projection",
        "severity": "low",
        "machine": installation.serial_number,
        "reason": "Operating-hour trend suggests the service threshold will be "
                  "reached soon. This is a straight-line projection from the "
                  "hours counter, not a predictive model.",
        "evidence": f"{round(per_day, 1)} h/day over {span_days} days "
                    f"({len(samples)} samples); {round(remaining, 1)} h remaining",
        "action": f"Expect service in about {int(round(days_out))} days",
        "confidence": round(confidence, 2),
        "at": datetime.utcnow().isoformat(),
    }


def by_severity(recs):
    """A queue ordered most severe first, then by serial.

    Ordering, not policy — which is why it lives here and takes a plain list. The
    fleet-wide entry point is `oem_sharing.fleet_recommendations`, because
    assembling a fleet means deciding what each customer has agreed to show, and
    that decision belongs to exactly one module.

    THIS FUNCTION USED TO BE THAT ENTRY POINT, and it took no grants. Its
    docstring claimed the sharing policy "stays in one place (oem_sharing)"; in
    fact nothing applied it, and `/oem/service` disclosed every customer's hour
    meter regardless of consent. Sorting is all that was ever really here.
    """
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(recs, key=lambda r: (rank.get(r["severity"], 9), r["machine"]))
