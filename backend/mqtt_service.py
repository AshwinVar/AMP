import asyncio
import json
import os
import threading
from datetime import datetime

from dotenv import load_dotenv
import paho.mqtt.client as mqtt

load_dotenv()

from sqlalchemy.exc import IntegrityError

from database import SessionLocal
import models
import mqtt_identity
import oem_telemetry
import tenancy
from machine_status import clamp_utilization, normalize_machine_status

import logging_config

log = logging_config.get_logger(__name__)

try:
    from live_ws import broadcast_live_event
except Exception:
    async def broadcast_live_event(event):
        log.info("Live WebSocket broadcast skipped: %s", event)


MQTT_BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# The prefix, not the whole topic: the tenant and site are segments of it now
# (see mqtt_identity). MQTT_TOPIC is still read so an existing deployment that
# set it to a custom single-tenant topic keeps that topic working via the legacy
# path below rather than silently going deaf after this upgrade.
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "flowmes")
LEGACY_TENANT = os.environ.get("MQTT_LEGACY_TENANT", "").strip()
LEGACY_SITE = os.environ.get("MQTT_LEGACY_SITE", "").strip()


def _non_negative_int(value):
    """Parse an inbound production count/minute into a non-negative int, or None
    if it isn't a usable number. A guard on INGEST, mirroring the HTTP path
    (machines_routes.create_production_record rejects negative minutes/counts,
    #266) and the utilization clamp above: an edge gateway can publish a
    non-numeric ("--") or NEGATIVE value, and a negative count is especially
    corrupting because it can still satisfy good+rejected==total (e.g.
    -5 + 15 == 10) yet write a negative good_count that drags pooled OEE below
    zero (pooled_oee's quality = good/total is not floored at 0). None means
    "no usable value" so the caller skips the production record rather than
    recording a physically-impossible one or throwing mid-handler.

    OverflowError is caught alongside TypeError/ValueError: JSON permits
    Infinity/-Infinity (Python's json.loads decodes them by default, so
    on_message reads a raw float('inf') straight off the payload), and a
    disconnected analog input on an edge gateway commonly reads infinity. A
    non-numeric ("--") and a NaN both raise ValueError, but int(float('inf'))
    raises OverflowError — NOT caught by (TypeError, ValueError) — so an infinite
    count escaped this guard and threw mid-handler, aborting the WHOLE message
    (its status / breakdown-downtime write and live broadcast with it) instead of
    skipping just the production record. This is the same non-finite reading
    clamp_utilization already rejects via isfinite; here int() raises directly, so
    catching OverflowError is the precise guard."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def tenant_is_provisioned(db, tenant: str) -> bool:
    """Has this tenant actually been onboarded?

    Fail-closed needs something to close against, and the honest answer to "is
    FACTORY_D a real customer?" is "does anything in this deployment already
    belong to FACTORY_D". Three signals, any one of which is enough:

      * a TenantConfig row  — created by platform onboarding
      * a User row          — every real customer has at least one login
      * a Machine row       — the tenant was seeded or imported

    Without this, a typo'd or hostile topic segment would auto-create machines
    under a tenant nobody owns. Those rows are invisible to every real tenant
    (the ADR-0002 scoping hook filters them out), so the failure is silent: the
    customer reports missing telemetry and nothing in the product explains where
    it went. Rejecting at the door puts the reason in the log instead.

    The consequence is deliberate and is the correct order of operations: a new
    customer must be provisioned BEFORE their gateway is pointed at the broker.
    """
    if db.query(models.TenantConfig).filter(
            models.TenantConfig.tenant_code == tenant).first():
        return True
    if db.query(models.User).filter(models.User.tenant_code == tenant).first():
        return True
    return bool(db.query(models.Machine).filter(
        models.Machine.tenant_code == tenant).first())


def get_or_create_machine(db, route, name: str):
    """Resolve a machine WITHIN a tenant and site, never by name alone.

    The filter carries all three terms because the name alone is not unique and
    never was: with FACTORY_A/CNC-01 and FACTORY_B/CNC-01 both present, a
    name-only `.first()` returns whichever row the database happens to order
    first — a coin flip that silently wrote one customer's telemetry onto
    another customer's machine. Measured before this change: FACTORY_B's
    breakdown packet flipped FACTORY_A's CNC-01 to Breakdown.
    """
    def find():
        return db.query(models.Machine).filter(
            models.Machine.tenant_code == route.tenant,
            models.Machine.site == route.site,
            models.Machine.name == name,
        ).first()

    machine = find()
    if machine:
        return machine

    machine = models.Machine(
        tenant_code=route.tenant,
        site=route.site,
        name=name,
        status="Idle",
        utilization=0,
        downtime="0 min",
    )

    db.add(machine)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race for the SAME identity, which is now possible precisely
        # BECAUSE this change added the uniqueness constraint. AMP can run more
        # than one instance (Railway scales the web service), each with its own
        # MQTT listener, so two processes can both miss on the SELECT above and
        # both try to insert the first packet for a newly-seen machine.
        #
        # The loser must not drop the message: a dropped first packet is a
        # permanent hole in that machine's history, and it would happen exactly
        # once per new machine — during commissioning, when someone is watching
        # the screen to check the integration works.
        #
        # Re-selecting rather than retrying the insert is the correct recovery:
        # the winner has committed the row we wanted, so there is nothing left
        # to create. If it is somehow still missing the exception propagates,
        # because inventing a machine after a constraint violation would be
        # guessing about identity, which is the whole thing this module refuses
        # to do.
        db.rollback()
        machine = find()
        if machine is None:
            raise
        log.info("machine %s/%s/%s was created concurrently; using the "
                 "committed row", route.tenant, route.site, name)
        return machine

    db.refresh(machine)

    return machine


def safe_broadcast(event: dict):
    try:
        asyncio.run(broadcast_live_event(event))
    except RuntimeError:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(broadcast_live_event(event))
            loop.close()
        except Exception as ws_error:
            log.info("WebSocket broadcast error: %s", repr(ws_error))
    except Exception as ws_error:
        log.info("WebSocket broadcast error: %s", repr(ws_error))


def on_connect(client, userdata, flags, rc):
    log.info(f"FastAPI MQTT connected with code: {rc}")

    if rc == 0:
        for topic_filter in mqtt_identity.topic_filters(TOPIC_PREFIX, LEGACY_TENANT):
            client.subscribe(topic_filter)
            log.info("FastAPI MQTT subscribed to %s", topic_filter)
    else:
        log.info("FastAPI MQTT connection failed")


def _record_installation_report(db, tenant, machine, payload):
    """Tell the OEM's installation record that its machine has reported.

    WHY THIS EXISTS. `MachineInstallation.last_seen_at` and `operating_hours`
    are read by the OEM fleet row (gated behind the factory's grants), by the
    fleet's connected/offline/unknown split, by the service clock that decides a
    machine is overdue, and by the `has_reported` commissioning check. Until
    this function they were written by NOTHING in the product — declared in
    ADR-0017, read in four places, and never set outside a performance harness.
    An OEM would have watched "no data" forever, no machine would ever have come
    due for service, and no machine could ever have been commissioned.

    WHAT IT DOES NOT DO. It does not invent a reading. `last_seen_at` is a fact
    the message itself establishes — this machine spoke, just now. Operating
    hours are taken only when the model's own telemetry profile names a source
    for them (ADR-0017: the vocabulary is the OEM's, never AMP's), so a model
    that does not report hours keeps NULL, which the portal renders as "no
    data" rather than as a zero.

    SCOPE. The installation must belong to the tenant the topic resolved to AND
    be linked to this exact machine. A machine reporting in one workspace can
    therefore never advance another workspace's service clock, and an unlinked
    installation is untouched — which is exactly why linking is a deliberate
    factory action (ADR-0019).
    """
    inst = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.machine_id == machine.id,
                      models.MachineInstallation.factory_tenant_code == tenant)
              .first())
    if inst is None:
        return

    inst.last_seen_at = datetime.utcnow()

    model = (db.query(models.MachineModel)
               .filter(models.MachineModel.id == inst.model_id).first())
    try:
        profile = oem_telemetry.parse(getattr(model, "telemetry_profile", None))
    except oem_telemetry.ProfileError as e:
        # A profile that cannot be trusted is not a reason to drop a reading
        # that needs no profile. The timestamp still stands; the hours do not.
        log.warning("telemetry profile unusable for %s: %s", inst.serial_number, e)
        return

    readings = payload.get("readings")
    if not isinstance(readings, dict) or not profile:
        return

    named, unknown, out_of_range = oem_telemetry.interpret(profile, readings)
    if unknown:
        # A gateway sending a tag nobody configured is a commissioning defect,
        # and silence hides it (oem_telemetry's own rule).
        log.warning("%s reported unconfigured tags: %s", inst.serial_number, unknown)
    if out_of_range:
        log.warning("%s reported out-of-range: %s", inst.serial_number, out_of_range)

    hours = named.get("operating_hours")
    if hours and isinstance(hours.get("value"), (int, float)):
        # Monotonic. An hour meter counts up; a lower figure is a replaced
        # controller or a mis-mapped tag, and accepting it would silently reset
        # the service clock and cancel a due service.
        if inst.operating_hours is None or hours["value"] >= inst.operating_hours:
            inst.operating_hours = float(hours["value"])
        else:
            log.warning("%s reported operating_hours going BACKWARDS (%s -> %s); "
                        "keeping the higher figure", inst.serial_number,
                        inst.operating_hours, hours["value"])


def on_message(client, userdata, msg):
    db = SessionLocal()
    tenant_token = None

    try:
        log.info("\nRAW MQTT MESSAGE RECEIVED")
        log.info("Topic: %s", msg.topic)

        raw_payload = msg.payload.decode()
        log.info("Payload: %s", raw_payload)

        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            log.warning("MQTT payload skipped: not a JSON object")
            return

        # ---- ROUTE BEFORE ANYTHING ELSE -----------------------------------
        # Nothing below may run until the message has exactly one owner. Every
        # failure here is a drop with a logged reason, never a default tenant:
        # "DEFAULT" was what the old code fell back to via the column default,
        # and it is precisely the wrong answer — it silently pooled three
        # customers' production into a tenant none of them can see.
        try:
            route = mqtt_identity.parse_topic(
                msg.topic, TOPIC_PREFIX, LEGACY_TENANT, LEGACY_SITE)
            mqtt_identity.check_payload_agrees(route, payload)
            machine_name = mqtt_identity.machine_identifier(payload)
        except mqtt_identity.RouteError as route_error:
            log.warning("MQTT message REJECTED (unroutable): %s", route_error)
            return

        if not tenant_is_provisioned(db, route.tenant):
            log.warning(
                "MQTT message REJECTED: tenant %r is not provisioned in this "
                "deployment (topic=%s)", route.tenant, msg.topic)
            return

        # Bind the thread to the resolved tenant for the rest of the handler so
        # the ADR-0002 scoping hook filters any read taken below, not just the
        # ones written with an explicit predicate. Defence in depth: the
        # explicit filters are the primary control.
        tenant_token = tenancy.set_current_tenant(route.tenant)

        downtime_value = payload.get("downtime", "0 min")

        machine = get_or_create_machine(db, route, machine_name)

        old_status = machine.status
        old_utilization = machine.utilization

        # Canonicalise the inbound reading before it touches the machine — the same
        # guard the IoT/industrial ingest paths already use (machine_status). An
        # MQTT/PLC gateway can publish a non-canonical status ("running",
        # "breakdown") or a glitching utilization; writing those straight onto the
        # machine dropped it from every status-based report and — because the
        # breakdown check below is case-sensitive — silently skipped its
        # DowntimeLog. An unrecognised status / non-numeric utilization leaves the
        # previous value untouched rather than corrupting it.
        new_status = normalize_machine_status(payload.get("status", "Idle"))
        status = new_status if new_status is not None else old_status

        clamped_utilization = clamp_utilization(payload.get("utilization", 0))
        utilization = clamped_utilization if clamped_utilization is not None else old_utilization

        machine.status = status
        machine.utilization = utilization
        machine.downtime = downtime_value

        _record_installation_report(db, route.tenant, machine, payload)

        db.commit()
        db.refresh(machine)

        log.info(
            f"DB UPDATED → {machine.name} | "
            f"{old_status} → {status} | "
            f"{old_utilization}% → {utilization}% | "
            f"Downtime: {downtime_value}"
        )

        if old_status != status:
            event = models.MachineEvent(
                machine_id=machine.id,
                tenant_code=machine.tenant_code,
                machine_name=machine.name,
                old_status=old_status,
                new_status=status,
                utilization=utilization,
                source="mqtt",
            )

            db.add(event)
            db.commit()

        # Parse the production numerics defensively — a non-numeric value would
        # raise mid-handler (dropping the whole message) and a NEGATIVE value is
        # physically impossible; either one skips the record rather than
        # corrupting the OEE window (see _non_negative_int / HTTP-ingest parity).
        total_count = _non_negative_int(payload.get("total_count", 0))
        good_count = _non_negative_int(payload.get("good_count", 0))
        rejected_count = _non_negative_int(payload.get("rejected_count", 0))
        planned_minutes = _non_negative_int(payload.get("planned_minutes", 480))
        runtime_minutes = _non_negative_int(payload.get("runtime_minutes", 0))
        ideal_cycle_time_seconds = _non_negative_int(
            payload.get("ideal_cycle_time_seconds", 60)
        )

        production_valid = None not in (
            total_count, good_count, rejected_count,
            planned_minutes, runtime_minutes, ideal_cycle_time_seconds,
        )

        if (production_valid and total_count > 0
                and good_count + rejected_count == total_count):
            production = models.ProductionRecord(
                machine_id=machine.id,
                tenant_code=machine.tenant_code,
                planned_minutes=planned_minutes,
                runtime_minutes=runtime_minutes,
                ideal_cycle_time_seconds=ideal_cycle_time_seconds,
                total_count=total_count,
                good_count=good_count,
                rejected_count=rejected_count,
            )

            db.add(production)

        # One DowntimeLog per breakdown EVENT — only on the transition INTO
        # Breakdown, not on every message while the machine stays down. A PLC
        # gateway publishes on an interval, so gating on status alone wrote a new
        # row (each re-asserting the full downtime string) on every tick, inflating
        # event counts, downtime minutes, MTBF/MTTR and the risk score without bound.
        if old_status != status and status == "Breakdown":
            downtime = models.DowntimeLog(
                machine_id=machine.id,
                tenant_code=machine.tenant_code,
                reason="Breakdown",
                duration=downtime_value,
                notes="MQTT auto-generated downtime event",
            )

            db.add(downtime)

        db.commit()

        live_event = {
            "event": "machine_update",
            "tenant_code": machine.tenant_code,
            "machine": {
                "id": machine.id,
                # Name alone does not identify a machine — three customers own a
                # CNC-01. A consumer that filters or renders on name needs the
                # site to tell two of one customer's plants apart, and the
                # tenant_code above to tell customers apart.
                "site": machine.site,
                "name": machine.name,
                "status": machine.status,
                "utilization": machine.utilization,
                "downtime": machine.downtime,
            },
            "production": {
                # Coalesce to 0 for the live view — a skipped/garbage reading
                # recorded nothing, so report nothing rather than a raw None.
                "total_count": total_count or 0,
                "good_count": good_count or 0,
                "rejected_count": rejected_count or 0,
            },
            "timeline": {
                "old_status": old_status,
                "new_status": status,
            },
            "source": "mqtt",
        }

        safe_broadcast(live_event)

        log.info(
            f"FASTAPI MQTT → WS BROADCAST: "
            f"{machine.name} | {status} | {utilization}%"
        )

    except Exception as e:
        db.rollback()
        log.info("FastAPI MQTT service error: %s", repr(e))

    finally:
        if tenant_token is not None:
            tenancy.reset_current_tenant(tenant_token)
        db.close()


def start_mqtt_service():
    def run():
        client = mqtt.Client()
        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            log.info("FastAPI MQTT connection error: %s", repr(e))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    log.info("FastAPI embedded MQTT service started")