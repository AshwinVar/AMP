"""VDA 5050 master-control adapter: real AMRs drive the same fleet rows.

WHAT THIS IS. The first real-vehicle integration for the fleet pack. VDA 5050
is the open AGV/AMR interoperability standard (VDA + VDMA; spoken by MiR,
OTTO, idealworks, NVIDIA's Isaac Mission Dispatch stack, and most serious
vendors): vehicles and a "master control" exchange JSON over MQTT on topics

    {interface}/{majorVersion}/{manufacturer}/{serialNumber}/{channel}

where the vehicle publishes `state` (~1 Hz: position, battery, driving,
errors) and `connection` (retained, with a last-will), and master control
publishes `order` (a released graph of nodes and edges to traverse) and
`instantActions` (e.g. cancelOrder). AMP is the master control.

THE CONTRACT WAS ALREADY WRITTEN. fleet_engine's docstring promises: a real
integration writes x, y, battery_pct, status onto FleetVehicle rows and flips
TransportTask through Pending -> Assigned -> InTransit -> Delivered, and the
dashboard cannot tell the difference. That is exactly and only what this
module does. Dispatch (which vehicle gets which task) stays in fleet_engine —
shared with the simulator — so policy is decided once; this module turns an
assignment into an order on the wire, and the vehicle's own reports into row
updates.

WHO MOVES WHOM. A FleetVehicle row with adapter="vda5050" is NEVER moved by
fleet_engine.advance() (it skips non-"sim" rows): its position, battery and
status come only from its state messages. Conversely this module never
invents motion — between two reports the row simply holds the last report,
which is the truth.

FAIL-CLOSED REGISTRATION, THE mqtt_service LESSON. A VDA 5050 topic carries
no tenant. The vehicle's identity ("{manufacturer}/{serial}") is therefore
registered FIRST (POST /fleet/vehicles with adapter="vda5050"), unique
installation-wide like GatewayCredential.gateway_id, and the row is what
binds a message to a tenant. An unregistered identity is dropped with a log
line — never auto-created, because creating it would mean guessing an owner,
the one unrecoverable mistake in a multi-tenant ingest path.

ORDERS ARE THREE NODES. start (where the vehicle is) -> pickup (the task's
origin station) -> dropoff (destination), even sequenceIds with the two edges
between them, everything released, no VDA actions. Progress is read from the
vehicle's own lastNodeSequenceId: reaching the pickup node flips the task
InTransit; finishing the order (dropoff reached, not driving) delivers it.
Actions are deliberately not used: the action vocabulary is vendor-specific,
while node traversal is the part of the spec every implementation — including
NVIDIA's simulated robots — agrees on.

EVENT-DRIVEN, NO BACKGROUND LOOP. The lazy tick runs on dashboard reads; a
real fleet must not need a dashboard open. So the vehicle's own heartbeat is
the scheduler: each state message runs dispatch when the reporting vehicle is
idle and work is pending, and publishes the order for whatever task the
vehicle holds that has none yet. A fleet of N vehicles at 1 Hz gives N
dispatch opportunities a second with zero timers, zero polling.

Runner: start_vda5050_service() (called from main.py at boot) starts a paho
thread iff VDA5050_BROKER is set, mirroring mqtt_service's hardened boot
(retry with backoff, named persistent session, optional TLS/credentials).
Every behaviour worth testing lives in the pure handlers above it, callable
with a Session, a dict and a fake `publish` — no broker in any test.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from itertools import count

import paho.mqtt.client as mqtt

import models
import tenancy
from database import SessionLocal

import logging_config

log = logging_config.get_logger(__name__)

# Topic scheme. The interface name and major version are deployment-wide
# (every vehicle on the broker speaks one), env-overridable because vendors
# ship different defaults ("uagv" is the spec's own example and NVIDIA's).
INTERFACE = (os.environ.get("VDA5050_INTERFACE") or "").strip() or "uagv"
MAJOR_VERSION = (os.environ.get("VDA5050_VERSION") or "").strip() or "v2"
PROTOCOL_VERSION = "2.0.0"      # the full version stamped into message headers
MAP_ID = "amp-floor"            # fleet_engine's 100x100 m grid, one map

DEFAULT_CLIENT_ID = "amp-vda5050"   # stable, for the same reason amp-ingest is

ADAPTER_SIM = "sim"
ADAPTER_VDA5050 = "vda5050"

_header_ids = count(1)


def identity(manufacturer: str, serial: str) -> str:
    return f"{manufacturer}/{serial}"


def topic_filters():
    """What master control listens to: every vehicle's state and connection."""
    base = f"{INTERFACE}/{MAJOR_VERSION}"
    return [f"{base}/+/+/state", f"{base}/+/+/connection"]


def parse_topic(topic: str):
    """(manufacturer, serial, channel) for a topic in our scheme, else None.

    Segment COUNT is checked exactly — a five-segment match on a wildcard
    subscription is the only shape that can arrive, but a parser that trusts
    its subscription is a parser that breaks when the subscription changes.
    """
    parts = (topic or "").split("/")
    if len(parts) != 5 or parts[0] != INTERFACE or parts[1] != MAJOR_VERSION:
        return None
    manufacturer, serial, channel = parts[2], parts[3], parts[4]
    if not manufacturer or not serial:
        return None
    return manufacturer, serial, channel


def vehicle_for(db, manufacturer: str, serial: str):
    """The registered row for this identity, or None. Deliberately NOT
    tenant-filtered: the identity arrives before AMP knows whose it is, and
    the row is what answers that (see the module docstring)."""
    return (db.query(models.FleetVehicle)
              .filter(models.FleetVehicle.vda_identity == identity(manufacturer, serial))
              .first())


def _header(manufacturer: str, serial: str, now=None):
    now = now or datetime.utcnow()
    return {
        "headerId": next(_header_ids),
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "version": PROTOCOL_VERSION,
        "manufacturer": manufacturer,
        "serialNumber": serial,
    }


def _vehicle_topic(vehicle, channel: str) -> str:
    return f"{INTERFACE}/{MAJOR_VERSION}/{vehicle.vda_identity}/{channel}"


# ── Outbound: orders and instant actions ─────────────────────────────────

# The three-node order's sequenceIds (nodes even, edges odd — the spec's
# numbering). Progress reads compare against these.
SEQ_START, SEQ_PICKUP, SEQ_DROPOFF = 0, 2, 4


def order_for(vehicle, task, stations, order_id: str, update_id: int, now=None):
    """The VDA 5050 order for one transport task. Pure: no I/O, no ids
    invented here (the caller owns order_id so re-publishes keep it)."""
    origin = stations[task.origin]
    dest = stations[task.destination]
    manufacturer, serial = vehicle.vda_identity.split("/", 1)

    def node(seq, node_id, x, y):
        return {"nodeId": node_id, "sequenceId": seq, "released": True,
                "nodePosition": {"x": float(x), "y": float(y), "mapId": MAP_ID},
                "actions": []}

    start_id = f"start-{task.id}"
    pickup_id = f"pickup-{task.id}"
    dropoff_id = f"dropoff-{task.id}"
    body = {
        "orderId": order_id,
        "orderUpdateId": update_id,
        "nodes": [
            node(SEQ_START, start_id, vehicle.x or 0.0, vehicle.y or 0.0),
            node(SEQ_PICKUP, pickup_id, origin["x"], origin["y"]),
            node(SEQ_DROPOFF, dropoff_id, dest["x"], dest["y"]),
        ],
        "edges": [
            {"edgeId": f"edge-{task.id}-1", "sequenceId": 1, "released": True,
             "startNodeId": start_id, "endNodeId": pickup_id, "actions": []},
            {"edgeId": f"edge-{task.id}-2", "sequenceId": 3, "released": True,
             "startNodeId": pickup_id, "endNodeId": dropoff_id, "actions": []},
        ],
    }
    body.update(_header(manufacturer, serial, now))
    return body


def cancel_order_action(vehicle, now=None):
    """The instantAction that tells a vehicle to drop its current order.
    cancelOrder is one of the few actions the spec itself defines, so every
    compliant vehicle supports it — the reason the advice verbs map to it."""
    manufacturer, serial = vehicle.vda_identity.split("/", 1)
    body = {
        "actions": [{
            "actionType": "cancelOrder",
            "actionId": uuid.uuid4().hex,
            "blockingType": "HARD",
        }],
    }
    body.update(_header(manufacturer, serial, now))
    return body


def ensure_order(db, vehicle, publish, stations=None, now=None):
    """Publish the order for the vehicle's current task if none is on the
    wire yet. Returns the orderId published, or None.

    orderId is FRESH PER ASSIGNMENT (task id + a nonce): a vehicle rejects an
    orderId it has already seen with a non-increasing orderUpdateId, so a task
    that was returned to the queue and re-dispatched to the same vehicle must
    not reuse its old orderId at update 0.
    """
    import fleet_engine
    if not vehicle.current_task_id:
        return None
    task = (db.query(models.TransportTask)
              .filter(models.TransportTask.id == vehicle.current_task_id,
                      models.TransportTask.status.in_(("Assigned", "InTransit")))
              .first())
    if task is None or task.vda_order_id is not None:
        return None
    stations = stations or fleet_engine.stations_for(db)
    if task.origin not in stations or task.destination not in stations:
        return None     # unknown station: dispatch policy owns that story
    order_id = f"amp-task-{task.id}-{uuid.uuid4().hex[:8]}"
    task.vda_order_id = order_id
    task.vda_order_update_id = 0
    db.commit()
    publish(_vehicle_topic(vehicle, "order"),
            order_for(vehicle, task, stations, order_id, 0, now))
    log.info("VDA5050 order %s published: task #%d %s -> %s on %s",
             order_id, task.id, task.origin, task.destination, vehicle.name)
    return order_id


# ── Inbound: state and connection ────────────────────────────────────────

def _has_fatal_error(state: dict) -> bool:
    return any((e or {}).get("errorLevel", "").upper() == "FATAL"
               for e in state.get("errors") or [])


def _open_alert(db, vehicle, kind: str):
    return (db.query(models.FleetAlert)
              .filter(models.FleetAlert.vehicle_id == vehicle.id,
                      models.FleetAlert.kind == kind,
                      models.FleetAlert.status == "Open")
              .first())


def handle_state(db, vehicle, state: dict, publish, now=None):
    """One state report -> row truth. The whole inbound mapping, pure.

    Order of business: position and battery are facts and land first; then
    task progress (pickup / delivery) read from the order the vehicle says it
    is executing; then status; then the event-driven scheduling (dispatch if
    idle, publish an order if one is owed).
    """
    import fleet_engine
    now = now or datetime.utcnow()

    # 1. Position and battery — facts, whatever else is going on.
    pos = state.get("agvPosition") or {}
    if isinstance(pos.get("x"), (int, float)) and isinstance(pos.get("y"), (int, float)):
        moved = fleet_engine._dist(vehicle.x or 0, vehicle.y or 0,
                                   float(pos["x"]), float(pos["y"]))
        vehicle.x, vehicle.y = float(pos["x"]), float(pos["y"])
        vehicle.odometer_m = (vehicle.odometer_m or 0) + moved
    battery = (state.get("batteryState") or {}).get("batteryCharge")
    if isinstance(battery, (int, float)):
        vehicle.battery_pct = max(0.0, min(100.0, float(battery)))

    # 2. Task progress, read from the vehicle's own order echo.
    task = None
    if vehicle.current_task_id:
        task = (db.query(models.TransportTask)
                  .filter(models.TransportTask.id == vehicle.current_task_id,
                          models.TransportTask.status.in_(("Assigned", "InTransit")))
                  .first())
        if task is None:
            # Cancelled under the vehicle (the engine does the same dance).
            vehicle.current_task_id = None
    last_seq = state.get("lastNodeSequenceId")
    on_our_order = (task is not None and task.vda_order_id is not None
                    and state.get("orderId") == task.vda_order_id
                    and isinstance(last_seq, (int, float)))
    delivered = False
    if on_our_order:
        if task.status == "Assigned" and last_seq >= SEQ_PICKUP:
            task.status = "InTransit"
            task.picked_up_at = now
        if (task.status == "InTransit" and last_seq >= SEQ_DROPOFF
                and not state.get("driving", False)):
            task.status = "Delivered"
            task.completed_at = now
            task.vda_order_id = None
            task.vda_order_update_id = 0
            vehicle.deliveries_total = (vehicle.deliveries_total or 0) + 1
            vehicle.current_task_id = None
            task = None
            delivered = True

    # 3. Status. A FATAL error is a plea (once per episode); a vehicle that
    # is Stuck stays Stuck while its plea is open — the human owns the verb,
    # exactly like the simulator (the Waymo model) — and recovers to the
    # task-appropriate status only once the plea is resolved or cleared.
    if _has_fatal_error(state):
        if vehicle.status != "Stuck":
            vehicle.status = "Stuck"
        if _open_alert(db, vehicle, "Stuck") is None:
            first = next((e for e in state.get("errors") or []
                          if (e or {}).get("errorLevel", "").upper() == "FATAL"), {})
            what = first.get("errorType") or "fatal error"
            db.add(models.FleetAlert(
                vehicle_id=vehicle.id, task_id=task.id if task else None,
                kind="Stuck", severity="High",
                message=f"{vehicle.name} reported {what} at "
                        f"({vehicle.x or 0:.0f}, {vehicle.y or 0:.0f}); needs advice."))
    elif vehicle.status == "Stuck" and _open_alert(db, vehicle, "Stuck") is not None:
        pass    # error cleared but the plea is still open: the human decides
    else:
        if (state.get("batteryState") or {}).get("charging"):
            vehicle.status = "Charging"
        elif task is not None:
            vehicle.status = "Carrying" if task.picked_up_at else "ToPickup"
        else:
            vehicle.status = "Idle"

    db.commit()

    # 4. Event-driven scheduling: the heartbeat is the scheduler.
    if vehicle.status == "Idle" and not vehicle.current_task_id:
        pending = (db.query(models.TransportTask)
                     .filter(models.TransportTask.status == "Pending").count())
        if pending:
            fleet_engine.dispatch(db)
            db.refresh(vehicle)
    ensure_order(db, vehicle, publish, now=now)
    return delivered


def handle_connection(db, vehicle, payload: dict, now=None):
    """ONLINE/OFFLINE/CONNECTIONBROKEN -> the row's Offline flag. The task is
    deliberately KEPT on an offline vehicle: dropping it would silently
    re-dispatch cargo that may be sitting on the deck of a vehicle that lost
    wifi, and the assistance queue is where a human decides that."""
    now = now or datetime.utcnow()
    conn = (payload.get("connectionState") or "").upper()
    if conn in ("OFFLINE", "CONNECTIONBROKEN"):
        vehicle.status = "Offline"
    elif conn == "ONLINE" and vehicle.status == "Offline":
        task = None
        if vehicle.current_task_id:
            task = (db.query(models.TransportTask)
                      .filter(models.TransportTask.id == vehicle.current_task_id,
                              models.TransportTask.status.in_(("Assigned", "InTransit")))
                      .first())
        if task is not None:
            vehicle.status = "Carrying" if task.picked_up_at else "ToPickup"
        else:
            vehicle.current_task_id = None
            vehicle.status = "Idle"
    db.commit()


def on_advice(db, vehicle, advice: str, publish, now=None):
    """What the human's verb means ON THE WIRE for a real vehicle. Called by
    the advise route after fleet_engine.advise() has applied the row-side
    meaning (shared with the simulator):

      resume          re-publish the current order, orderUpdateId + 1 — the
                      spec's way of saying "carry on with this order"
      return_task     cancelOrder; the task is already back in the queue
      send_to_charge  cancelOrder; charging is the vehicle's own policy once
                      it has no order (AMP will not navigate it by force)
    """
    import fleet_engine
    if (vehicle.adapter or ADAPTER_SIM) != ADAPTER_VDA5050 or not vehicle.vda_identity:
        return
    now = now or datetime.utcnow()
    if advice == "resume":
        task = None
        if vehicle.current_task_id:
            task = (db.query(models.TransportTask)
                      .filter(models.TransportTask.id == vehicle.current_task_id,
                              models.TransportTask.status.in_(("Assigned", "InTransit")))
                      .first())
        if task is not None and task.vda_order_id is not None:
            task.vda_order_update_id = (task.vda_order_update_id or 0) + 1
            db.commit()
            stations = fleet_engine.stations_for(db)
            if task.origin in stations and task.destination in stations:
                publish(_vehicle_topic(vehicle, "order"),
                        order_for(vehicle, task, stations,
                                  task.vda_order_id, task.vda_order_update_id, now))
    else:   # return_task | send_to_charge — engine already released the task
        publish(_vehicle_topic(vehicle, "instantActions"),
                cancel_order_action(vehicle, now))


def on_task_cancelled(db, vehicle, publish, now=None):
    """The cancel route's wire-side effect: tell the vehicle to stand down."""
    if (vehicle.adapter or ADAPTER_SIM) != ADAPTER_VDA5050 or not vehicle.vda_identity:
        return
    publish(_vehicle_topic(vehicle, "instantActions"),
            cancel_order_action(vehicle, now))


# ── The runner (mirrors mqtt_service's hardened boot) ────────────────────

_client = None      # the live paho client, for publish_live()


def publish_live(topic: str, body: dict):
    """Publish through the service's client. A no-op with a log line when the
    service is not running — an advice verb must never 500 because the broker
    is down; the row-side meaning has already been applied."""
    if _client is None:
        log.info("VDA5050 publish skipped (no broker connected): %s", topic)
        return
    try:
        _client.publish(topic, json.dumps(body), qos=1)
    except Exception as e:      # noqa: BLE001 — never let the wire break a request
        log.info("VDA5050 publish failed on %s: %r", topic, e)


def vda5050_is_configured():
    return bool((os.environ.get("VDA5050_BROKER") or "").strip())


def on_message(client, userdata, msg):
    """Route one inbound message. Same discipline as mqtt_service.on_message:
    parse, resolve the owner fail-closed, bind the tenant, handle, and never
    let one bad message stop the listener."""
    db = SessionLocal()
    tenant_token = None
    try:
        parsed = parse_topic(msg.topic)
        if parsed is None:
            return
        manufacturer, serial, channel = parsed
        if channel not in ("state", "connection"):
            return
        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("VDA5050 message on %s is not JSON; dropped", msg.topic)
            return
        if not isinstance(payload, dict):
            return
        vehicle = vehicle_for(db, manufacturer, serial)
        if vehicle is None:
            log.warning("VDA5050 message from UNREGISTERED vehicle %s/%s dropped: "
                        "register it first (POST /fleet/vehicles, adapter=vda5050)",
                        manufacturer, serial)
            return
        tenant_token = tenancy.set_current_tenant(vehicle.tenant_code)
        if channel == "connection":
            handle_connection(db, vehicle, payload)
        else:
            handle_state(db, vehicle, payload, publish_live)
    except Exception as e:      # noqa: BLE001 — the listener must outlive any message
        db.rollback()
        log.info("VDA5050 handler error: %r", e)
    finally:
        if tenant_token is not None:
            tenancy.reset_current_tenant(tenant_token)
        db.close()


def on_connect(client, userdata, flags, rc):
    log.info("VDA5050 master control connected with code: %s", rc)
    if rc == 0:
        for f in topic_filters():
            client.subscribe(f, qos=1)
            log.info("VDA5050 subscribed to %s", f)


def _build_client():
    client_id = (os.environ.get("VDA5050_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID
    client = mqtt.Client(client_id=client_id, clean_session=False)
    client.on_connect = on_connect
    client.on_message = on_message
    username = (os.environ.get("VDA5050_USERNAME") or "").strip()
    if username:
        client.username_pw_set(username, os.environ.get("VDA5050_PASSWORD") or None)
    if (os.environ.get("VDA5050_TLS") or "").strip().lower() in ("1", "true", "yes", "on"):
        client.tls_set()
    return client


def start_vda5050_service(client_factory=None, sleep=time.sleep, run_inline=False):
    """Start the VDA 5050 master-control listener, iff VDA5050_BROKER is set.

    Deliberately a SEPARATE client and env namespace from mqtt_service: the
    telemetry broker and the vehicle broker are often the same box but are
    different trust domains with different credentials, and the hardened
    telemetry path must not grow a second responsibility. The boot contract
    (not-configured is not an error; a refused first connect retries with
    backoff) is mqtt_service's, kept identical on purpose.
    """
    global _client
    if not vda5050_is_configured():
        log.info("VDA5050 adapter not configured (VDA5050_BROKER unset); "
                 "no master control started. Simulated fleets are unaffected.")
        return None

    host = (os.environ.get("VDA5050_BROKER") or "").strip()
    try:
        port = int(os.environ.get("VDA5050_PORT") or 1883)
    except (TypeError, ValueError):
        port = 1883
    try:
        retries = max(1, int(os.environ.get("VDA5050_CONNECT_RETRIES") or 5))
    except (TypeError, ValueError):
        retries = 5
    try:
        backoff = float(os.environ.get("VDA5050_CONNECT_BACKOFF_SECONDS") or 2)
    except (TypeError, ValueError):
        backoff = 2.0

    def run():
        global _client
        client = (client_factory or _build_client)()
        for attempt in range(1, retries + 1):
            try:
                client.connect(host, port, 60)
                break
            except Exception as e:      # noqa: BLE001
                if attempt >= retries:
                    log.info("VDA5050 connect to %s:%s failed after %d attempt(s): %s. "
                             "No VDA 5050 master control for this process.",
                             host, port, attempt, repr(e))
                    return
                log.info("VDA5050 connect to %s:%s failed (attempt %d/%d): %s; retrying",
                         host, port, attempt, retries, repr(e))
                sleep(backoff * attempt)
        _client = client
        try:
            client.loop_forever()
        except Exception as e:      # noqa: BLE001
            log.info("VDA5050 master control stopped: %r", e)
        finally:
            _client = None

    if run_inline:
        run()
        return None

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    log.info("VDA5050 master control started (broker %s:%s)", host, port)
    return thread
