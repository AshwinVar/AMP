"""Is the plant reaching AMP at all — and when it is not, WHICH half is broken?

THE THREE FAULTS THAT LOOK IDENTICAL. On a board that only draws numbers, these
are the same picture — a machine with no bars today:

    1. the press is idle            nothing is wrong
    2. the press is running, but its contact is unplugged from the gateway
    3. the Raspberry Pi in the panel has no power, or is off the WiFi

The customer this was written for has fourteen presses and one unattended Pi in
a panel. Nobody watches the thing that watches the machines, so (2) and (3) are
discovered when the day's figures come up short — which is the day after.

WHAT AMP CAN HONESTLY TELL APART, AND WHAT IT CANNOT. The gateway writes
`GatewayCredential.last_seen_at` on every authorised packet, and the edge
publishes every PUBLISH_WINDOW (300 s) whether or not anything was made — its
`latest` survives the window reset, so a quiet press still reports. So silence
from the gateway is real silence, not an idle plant, and three missed windows is
a dead node.

    gateway fresh + machine fresh     REPORTING       green
    gateway fresh + machine silent    MACHINE SILENT  amber   its link, or it is off
    gateway silent                    GATEWAY DOWN    red     every machine under it
    nothing ever received             NEVER REPORTED  grey    not commissioned

THE RULE THAT MATTERS: when the gateway is down, its machines are NOT reported
as silent. AMP cannot see them at all, so claiming "IMM-07 has stopped" would be
asserting something it has no evidence for; the honest statement is that AMP
lost the gateway and knows nothing about what is behind it.

POWER OR NETWORK? NOT LIVE — a Pi with no power and a Pi off the WiFi both
produce exactly the same evidence at this end: nothing arrives. AMP says so
rather than guessing. The distinction IS knowable afterwards, and the edge
already records it: `buffer.alive_at` is heartbeated to disk every drain loop,
so a gateway that kept running through a network outage returns with its queue
intact and no gap marker, while one that lost power returns with
`note_restart()` having armed a gap for the window it could not see. That gap
becomes a "Data gap at <machine>" notification (mqtt_service._record_data_gap).
`resolved_cause` below reads it back, so once the Pi returns the screen can say
which it was instead of leaving a shrug on the page.

A pure read-model over machines, gateway_credentials, machine_telemetry_spans,
production_records and notifications. It adds no storage and writes nothing.

`GatewayCredential` sits OUTSIDE the ADR-0002 scoping hook (ADR-0041: the
gateway id is resolved before the tenant is known), so every read of it here
filters `tenant_code` BY HAND — test_unscoped_model_reads.py enforces that.
"""
from datetime import datetime, timedelta

from sqlalchemy import func

import models

#: A gateway that has missed three publish windows (300 s each) is not a quiet
#: plant, it is a dead node. factory_ops_routes imports this so the banner and
#: the notification cannot drift apart.
GATEWAY_SILENCE = timedelta(minutes=15)
#: The same arithmetic for one machine's own stream, for the same reason.
MACHINE_SILENCE = timedelta(minutes=15)

REPORTING = "REPORTING"
MACHINE_SILENT = "MACHINE SILENT"
GATEWAY_DOWN = "GATEWAY UNREACHABLE"
NEVER = "NEVER REPORTED"

#: The light each state burns. Named here rather than in the browser so the
#: Machines table and the Plant Board cannot disagree about what amber means.
LIGHT = {REPORTING: "green", MACHINE_SILENT: "amber", GATEWAY_DOWN: "red", NEVER: "grey"}


def _minutes(since, now):
    return max(0, int((now - since).total_seconds() // 60)) if since else None


def _gateways(db, tenant, now):
    """Every active gateway for this tenant, with how long it has been quiet.

    A NULL `last_seen_at` has never reported at all: that is a provisioning
    state, not a silence, and calling it "down" would send somebody to a panel
    to look at a Pi that was never installed.
    """
    rows = (db.query(models.GatewayCredential)
              .filter(models.GatewayCredential.tenant_code == tenant,
                      models.GatewayCredential.is_active.is_(True))
              .order_by(models.GatewayCredential.gateway_id.asc()).all())
    out = []
    for gw in rows:
        quiet = _minutes(gw.last_seen_at, now)
        state = (NEVER if gw.last_seen_at is None
                 else GATEWAY_DOWN if (now - gw.last_seen_at) > GATEWAY_SILENCE
                 else REPORTING)
        out.append({
            "gateway_id": gw.gateway_id,
            "label": gw.label or gw.gateway_id,
            "site": gw.site or "",
            "state": state,
            "light": LIGHT[state],
            "last_seen": gw.last_seen_at.isoformat() if gw.last_seen_at else None,
            "quiet_minutes": quiet,
            "reason": _gateway_reason(state, gw.label or gw.gateway_id, quiet),
            "fix": ("Check the Pi has power, that it is on the WiFi, and that the "
                    "amp-edge service is running." if state == GATEWAY_DOWN else ""),
        })
    return out


def _gateway_reason(state, name, quiet):
    if state == NEVER:
        return f"{name} has never reported. It is registered but not yet commissioned."
    if state == GATEWAY_DOWN:
        # Deliberately all three, in one sentence, with no ranking: AMP has the
        # same evidence for each (none), and naming one would be a guess that
        # sends somebody to the wrong place.
        return (f"{name} last reached AMP {quiet} minutes ago. It has no power, it is "
                f"off the network, or the amp-edge service has stopped — from here "
                f"those look the same, and AMP will say which once it returns.")
    return f"{name} is reporting."


#: Where an answer to "when did AMP last hear from this machine" came from.
#: Reported because the three paths look nothing alike to the person chasing a
#: silent machine: a gateway writes production records, an IoT device writes
#: telemetry rows, and "0 signals" on a machine that is plainly reporting is the
#: confusion this exists to end.
IOT, SPAN, PRODUCTION = "iot telemetry", "telemetry span", "production record"


def last_heard(db, tenant, since, machine_ids=None):
    """{machine_id: (when, source)} — the latest ANY path heard from a machine.

    THREE SOURCES BECAUSE THERE ARE THREE PATHS, and a read-model that knows
    only one declares the others dead:

      * `iot_telemetry`   the IoT/command path, one row per signal
      * telemetry spans   every status-bearing MQTT message (ADR-0021)
      * production records the MQTT gateway's actual output

    A machine fed by a Raspberry Pi gateway writes the last two and NEVER the
    first, so a freshness check over `iot_telemetry` alone reports a plant that
    is reporting perfectly as dark. That was live.

    `since` bounds the scan. `machine_ids`, when given, bounds it further — the
    callers that only need to tell "silent" from "never seen" probe just the
    machines that came back empty, rather than scanning an ever-growing history
    for every machine on every poll.
    """
    heard = {}

    def offer(mid, when, source):
        if mid is None or when is None:
            return
        if mid not in heard or when > heard[mid][0]:
            heard[mid] = (when, source)

    for model, column, source in (
        (models.IoTTelemetry, models.IoTTelemetry.created_at, IOT),
        (models.MachineTelemetrySpan, models.MachineTelemetrySpan.span_end, SPAN),
        (models.ProductionRecord, models.ProductionRecord.created_at, PRODUCTION),
    ):
        q = (db.query(model.machine_id, func.max(column))
               .filter(model.tenant_code == tenant, model.machine_id.isnot(None)))
        if since is not None:
            q = q.filter(column >= since)
        if machine_ids is not None:
            if not machine_ids:
                continue
            q = q.filter(model.machine_id.in_(list(machine_ids)))
        for mid, when in q.group_by(model.machine_id).all():
            offer(mid, when, source)
    return heard


def _last_heard(db, tenant, since):
    """Just the timestamps, for callers that do not care which path answered."""
    return {mid: when for mid, (when, _src) in last_heard(db, tenant, since).items()}


def resolved_cause(db, tenant, machine_name):
    """What the LAST outage turned out to be, once the gateway came back.

    The edge heartbeats `alive_at` to disk while it runs, so a gateway that was
    powered through a network outage returns with nothing missing, and one that
    lost power returns having armed a gap for the window it could not see. The
    gap is raised as an unread "Data gap at <machine>" notification, so its
    presence is the evidence — and its absence, after a silence, means the
    counts survived.

    None when there is nothing to say.
    """
    hit = (db.query(models.Notification)
             .filter(models.Notification.tenant_code == tenant,
                     models.Notification.title == f"Data gap at {machine_name}",
                     models.Notification.status != "Read")
             .first())
    if hit is None:
        return None
    return ("The gateway restarted and the counts for that window were never taken — "
            "so it lost power or the service stopped, rather than only losing the network.")


def build_gateway_health(db, tenant, now=None):
    """Per machine: can AMP see it, and if not, which half is broken."""
    now = now or datetime.utcnow()
    gateways = _gateways(db, tenant, now)
    machines = (db.query(models.Machine)
                  .filter(models.Machine.tenant_code == tenant)
                  .order_by(models.Machine.name.asc()).all())

    # A gateway publishes per SITE (prefix/{tenant}/{site}/machines), so a
    # machine belongs to the gateway at its site. With exactly one gateway on
    # the tenant that mapping is unambiguous whatever the sites say, which is
    # the common case and the one this customer is in.
    by_site = {g["site"]: g for g in gateways}
    only = gateways[0] if len(gateways) == 1 else None

    heard = _last_heard(db, tenant, now - timedelta(days=30))
    rows = []
    for m in machines:
        gw = only or by_site.get(m.site or "")
        seen = heard.get(m.id)
        quiet = _minutes(seen, now)
        if gw is not None and gw["state"] == GATEWAY_DOWN:
            # Its own stream is UNKNOWN, not silent: nothing about this machine
            # can reach AMP while the gateway is unreachable.
            state = GATEWAY_DOWN
            reason = gw["reason"]
            fix = gw["fix"]
        elif gw is not None and gw["state"] == NEVER:
            # Registered but never commissioned. Nothing is BROKEN, so this is
            # grey and not red: sending somebody to a panel to look at a Pi that
            # was never installed is how an alert stops being believed.
            state = NEVER
            reason = gw["reason"]
            fix = "Commission the gateway: run `validate`, then `check-amp`, then `run`."
        elif seen is None:
            state = NEVER
            reason = (f"AMP has never received anything for {m.name}. It is on the "
                      f"machine list but nothing is wired to it yet.")
            fix = "Map it in the gateway's config, or check it is named the same on both sides."
        elif (now - seen) > MACHINE_SILENCE:
            state = MACHINE_SILENT
            reason = (f"The gateway is reporting, but nothing has arrived for {m.name} "
                      f"for {quiet} minutes — so the gateway is fine and this machine's "
                      f"own link is not.")
            fix = "Check the contact wiring at the machine, and that the press is switched on."
        else:
            state = REPORTING
            reason = f"{m.name} reported {quiet} minutes ago."
            fix = ""
        rows.append({
            "machine_id": m.id, "machine": m.name, "status": m.status,
            "state": state, "light": LIGHT[state],
            "last_heard": seen.isoformat() if seen else None,
            "quiet_minutes": quiet,
            "gateway": gw["label"] if gw else None,
            "reason": reason, "fix": fix,
            # Attached whatever the current state is. The question "what was
            # that outage?" is asked AFTER the gateway returns -- gating it on
            # the machine still being down means the answer only exists while
            # nobody can see the screen.
            "resolved_cause": resolved_cause(db, tenant, m.name),
        })

    worst = _worst(rows)
    return {
        "as_of": now.isoformat(),
        "gateways": gateways,
        "machines": rows,
        "reporting": sum(1 for r in rows if r["state"] == REPORTING),
        "total": len(rows),
        "worst": worst,
        "light": LIGHT[worst] if rows else "grey",
        # One sentence for a banner. Silence about a dead gateway is the thing
        # this module exists to stop, so the headline names it first.
        "headline": _headline(gateways, rows),
    }


def _worst(rows):
    """The one light the plant burns. Precedence, worst first, so the banner and
    the badge can never disagree: a dead gateway outranks a silent machine, and
    a machine nobody has wired yet is not a fault at all."""
    if any(r["state"] == GATEWAY_DOWN for r in rows):
        return GATEWAY_DOWN
    if any(r["state"] == MACHINE_SILENT for r in rows):
        return MACHINE_SILENT
    if rows and all(r["state"] == NEVER for r in rows):
        return NEVER
    return REPORTING


def _headline(gateways, rows):
    """One sentence for the banner, derived from the SAME rows `_worst` reads.

    Written this way after the first version computed its own verdict from the
    gateways and contradicted the badge -- the screen said every machine was
    unreachable while the banner said all of them were reporting.
    """
    worst = _worst(rows)
    if worst == GATEWAY_DOWN:
        down = [g for g in gateways if g["state"] == GATEWAY_DOWN]
        return down[0]["reason"] if down else "A gateway is unreachable."
    if worst == MACHINE_SILENT:
        silent = [r for r in rows if r["state"] == MACHINE_SILENT]
        names = ", ".join(r["machine"] for r in silent[:3])
        more = f" and {len(silent) - 3} more" if len(silent) > 3 else ""
        return (f"The gateway is reporting, but nothing has arrived for {names}{more}. "
                f"Check the wiring at those machines.")
    if worst == NEVER:
        waiting = [g for g in gateways if g["state"] == NEVER]
        if waiting:
            return (f"{waiting[0]['label']} is registered but has never reported — "
                    f"nothing is wired to it yet.")
        return "Nothing has ever reported for this workspace."
    if not gateways:
        return "No gateway is registered for this workspace."
    pending = [r for r in rows if r["state"] == NEVER]
    reporting = len(rows) - len(pending)
    tail = (f"; {len(pending)} not wired up yet" if pending else "")
    # A plant that is green NOW but was out earlier should still say what that
    # outage was -- that is the whole point of keeping the cause.
    cause = next((r["resolved_cause"] for r in rows if r["resolved_cause"]), None)
    head = f"All {reporting} machines are reporting{tail}."
    return f"{head} {cause}" if cause else head
