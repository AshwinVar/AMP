import asyncio
import json
import os
import threading

from dotenv import load_dotenv
import paho.mqtt.client as mqtt

load_dotenv()

from database import SessionLocal
import models
from machine_status import clamp_utilization, normalize_machine_status

try:
    from live_ws import broadcast_live_event
except Exception:
    async def broadcast_live_event(event):
        print("Live WebSocket broadcast skipped:", event)


MQTT_BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC = os.environ.get("MQTT_TOPIC", "flowmes/machines")


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


def get_or_create_machine(db, name: str):
    machine = db.query(models.Machine).filter(
        models.Machine.name == name
    ).first()

    if machine:
        return machine

    machine = models.Machine(
        name=name,
        status="Idle",
        utilization=0,
        downtime="0 min",
    )

    db.add(machine)
    db.commit()
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
            print("WebSocket broadcast error:", repr(ws_error))
    except Exception as ws_error:
        print("WebSocket broadcast error:", repr(ws_error))


def on_connect(client, userdata, flags, rc):
    print(f"FastAPI MQTT connected with code: {rc}")

    if rc == 0:
        client.subscribe(TOPIC)
        print(f"FastAPI MQTT subscribed to {TOPIC}")
    else:
        print("FastAPI MQTT connection failed")


def on_message(client, userdata, msg):
    db = SessionLocal()

    try:
        print("\nRAW MQTT MESSAGE RECEIVED")
        print("Topic:", msg.topic)

        raw_payload = msg.payload.decode()
        print("Payload:", raw_payload)

        payload = json.loads(raw_payload)

        machine_name = payload.get("machine")

        if not machine_name:
            print("MQTT payload skipped: missing machine name")
            return

        downtime_value = payload.get("downtime", "0 min")

        machine = get_or_create_machine(db, machine_name)

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

        db.commit()
        db.refresh(machine)

        print(
            f"DB UPDATED → {machine.name} | "
            f"{old_status} → {status} | "
            f"{old_utilization}% → {utilization}% | "
            f"Downtime: {downtime_value}"
        )

        if old_status != status:
            event = models.MachineEvent(
                machine_id=machine.id,
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

        print(
            f"FASTAPI MQTT → WS BROADCAST: "
            f"{machine.name} | {status} | {utilization}%"
        )

    except Exception as e:
        db.rollback()
        print("FastAPI MQTT service error:", repr(e))

    finally:
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
            print("FastAPI MQTT connection error:", repr(e))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    print("FastAPI embedded MQTT service started")