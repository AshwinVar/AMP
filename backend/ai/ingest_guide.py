"""How this workspace gets data into AMP, in the words a customer needs.

WHY THIS EXISTS. `mqtt_service` is a REAL ingest path: it writes machine status,
utilization, downtime, machine events and production records, and auto-creates
machines from `{prefix}/{tenant}/{site}/machines`. It works. And nothing in the
product ever told a customer the topic to publish to, or that the path existed
at all. A working ingest nobody can discover is, from the customer's side, the
same as no ingest — so an SME with a gateway had no way in, and the empty states
said "AMP has not received anything for this device" without ever saying where
anything should be sent.

WHAT THIS DELIBERATELY DOES NOT DO

  * It does not hand out credentials, and does not pretend they are
    self-service. There is ONE deployment-wide broker username and password
    (`mqtt_service._build_client`), not a per-tenant credential, so the honest
    thing to say is that the operator issues them — not to imply a
    self-serve flow that does not exist. No secret value is ever returned.
  * It does not claim AMP connects to anything. AMP opens NO connection to a
    PLC in any protocol (`industrial_adapters`): an edge agent on the customer's
    own network publishes to the topic below. The Connectivity screen already
    says this; saying it here too is cheaper than a customer discovering it
    after buying.
  * It invents no readiness. "Nothing has arrived yet" is reported as exactly
    that, never as a failure and never as success.

Composes existing configuration and the machines table; adds no storage.
"""
import models
import mqtt_service
from ai import evidence as ev

name = "ingest_guide"

M, U = ev.MEASURED, ev.UNKNOWN

# The paths a customer can actually use today, in the order they should try
# them. Each one is real: a route that exists and writes rows.
MANUAL_ROUTES = (
    ("Type it in", "Record production on the Machines screen, one row per machine per shift.",
     "/production-records"),
    ("Upload a CSV", "Import machines in bulk from a spreadsheet.", "/machines/import-csv"),
    ("Post it from your own system", "Any system that can make an HTTP request can write to AMP's API.",
     "/production-records"),
)


def _sites(db, tenant: str):
    """The sites this workspace's machines are filed under, newest naming first.

    `Machine.site` is part of the machine's identity (tenant, site, name) and is
    written today only by the MQTT path, from the topic. So an existing machine's
    site IS the topic segment a gateway should publish under — which is why this
    reads them rather than inventing a placeholder.
    """
    rows = (db.query(models.Machine.site)
            .filter(models.Machine.tenant_code == tenant)
            .distinct().all())
    return sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()})


def _duplicate_warning(configured, machines, siteless, sites):
    """What will actually happen if a gateway starts publishing, or None.

    The first version of this said "publish under the site these machines
    already use" — which is advice nobody can follow when NONE of them has a
    site, and that is the common case: `Machine.site` is written only by the
    MQTT path and is reachable from no form and no route, so every machine added
    by hand or by CSV has an empty one. Telling a customer to do an impossible
    thing is worse than telling them the awkward truth.
    """
    if not configured or not siteless:
        return None
    if sites:
        # Some machines DO carry a site, so there is a real answer.
        return (f"{siteless} of your {machines} machines have no site recorded. A machine is "
                f"identified by workspace, site and name, so a gateway publishing under a site will "
                f"REGISTER THOSE AGAIN rather than update them. Publish under "
                f"\"{sites[0]}\" — the site your other machines already use — so they match.")
    return (f"All {machines} of your machines have no site recorded, because a site is only set by "
            f"the gateway path and no screen can set one yet. A machine is identified by workspace, "
            f"site and name, so the first gateway message will REGISTER A SECOND SET rather than "
            f"update these. Ask your AMP operator to set the site before you start publishing.")


def build_ingest_guide(db, tenant: str) -> dict:
    """Where this workspace's data comes from, and where it could come from."""
    configured = mqtt_service.mqtt_is_configured()
    prefix = mqtt_service.TOPIC_PREFIX
    sites = _sites(db, tenant)
    # One example topic, using a real site when this workspace has one so the
    # string can be copied rather than adapted. `<site>` when it does not, which
    # is honest: AMP cannot guess what the customer calls their plant.
    example_site = sites[0] if sites else "<site>"
    topic = f"{prefix}/{tenant}/{example_site}/machines"

    machines = (db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant).count())
    # THE TRAP THIS SCREEN EXISTS TO WARN ABOUT. A machine's identity is
    # (tenant_code, site, name), and `site` is written today ONLY by the MQTT
    # path, from the topic. So machines added by hand or by CSV carry an EMPTY
    # site — and a gateway publishing under `plant-1` will not match them, it
    # will create a second set. Nobody would discover that until their machine
    # list doubled, which is exactly the kind of thing a "connect your data"
    # screen has to say before it happens rather than after.
    siteless = (db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant,
                        (models.Machine.site == "") | (models.Machine.site.is_(None))).count())
    # Has anything ever ARRIVED on the gateway path? A machine event stamped by
    # the MQTT writer is the only proof; a machine row alone is not, because the
    # manual form and the CSV import create those too.
    arrived = (db.query(models.MachineEvent)
               .filter(models.MachineEvent.tenant_code == tenant,
                       models.MachineEvent.source == "mqtt").count())

    facts = [
        ev.Fact(key="ingest.machines", label="Machines registered", value=machines,
                provenance=M, unit="machines", source="machines", window="now").to_dict("ingest.machines"),
        ev.Fact(key="ingest.siteless", label="Machines with no site recorded", value=siteless,
                provenance=M, unit="machines", source="machines", window="now",
                detail="a gateway publishing under a site name would register these again, "
                       "not update them").to_dict("ingest.siteless"),
    ]
    if configured:
        facts.append(ev.Fact(key="ingest.arrived", label="Readings received from a gateway",
                             value=arrived, provenance=M, unit="events", source="machine_events",
                             window="all time").to_dict("ingest.arrived"))
    else:
        facts.append(ev.Fact(key="ingest.arrived", label="Readings received from a gateway", value=None,
                             provenance=U, unit="events", source="machine_events", window="all time",
                             detail="no broker is configured on this deployment, so there is no "
                                    "gateway path to receive anything on").to_dict("ingest.arrived"))

    if not configured:
        state = ev.NOT_CONFIGURED
        headline = ("No message broker is configured on this deployment, so the gateway path is off. "
                    "Everything below still works: type production in, import a CSV, or post to the API.")
    elif arrived:
        state = ev.OK
        headline = (f"Your gateway is publishing to AMP — {arrived:,} reading"
                    f"{'' if arrived == 1 else 's'} received so far.")
    else:
        state = ev.NO_DATA
        headline = ("Nothing has arrived from a gateway yet. Publish to the topic below and the next "
                    "message will register its machine automatically.")

    return {
        "state": state,
        "headline": headline,
        "gateway": {
            "configured": configured,
            "topic": topic if configured else None,
            "topic_prefix": prefix if configured else None,
            "sites": sites,
            "readings_received": arrived if configured else None,
            # Said plainly, because it is the thing a buyer most often assumes
            # the other way round.
            "amp_connects_to_nothing": ("AMP opens no connection to your machines. An edge agent on "
                                        "your own network reads them and publishes to this topic."),
            "credentials": ("Broker credentials are issued by your AMP operator — they are not "
                            "self-service, and AMP never shows them on a screen."),
            # None, not a cheerful empty string: there is either a trap here or
            # there is not, and an empty string reads as "checked, nothing found"
            # on a screen that also renders when nothing was checked.
            "duplicate_warning": _duplicate_warning(configured, machines, siteless, sites),
        },
        "manual": [{"title": t, "detail": d, "route": r} for t, d, r in MANUAL_ROUTES],
        "machines": machines,
        "facts": facts,
        "note": ("AMP reports what it is given. Every path here writes the same rows, and the "
                 "Command Centre cannot tell which one they came from."),
    }
