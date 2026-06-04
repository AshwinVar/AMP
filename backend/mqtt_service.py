import asyncio
import json
import threading

import paho.mqtt.client as mqtt

from database import SessionLocal
import models

try:
    from live_ws import broadcast_live_event
except Exception:
    async def broadcast_live_event(event):
        print("Live WebSocket broadcast skipped:", event)


MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
TOPIC = "flowmes/machines"


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

        status = payload.get("status", "Idle")
        utilization = int(payload.get("utilization", 0))
        downtime_value = payload.get("downtime", "0 min")

        machine = get_or_create_machine(db, machine_name)

        old_status = machine.status
        old_utilization = machine.utilization

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

        total_count = int(payload.get("total_count", 0))
        good_count = int(payload.get("good_count", 0))
        rejected_count = int(payload.get("rejected_count", 0))

        if total_count > 0 and good_count + rejected_count == total_count:
            production = models.ProductionRecord(
                machine_id=machine.id,
                planned_minutes=int(payload.get("planned_minutes", 480)),
                runtime_minutes=int(payload.get("runtime_minutes", 0)),
                ideal_cycle_time_seconds=int(
                    payload.get("ideal_cycle_time_seconds", 60)
                ),
                total_count=total_count,
                good_count=good_count,
                rejected_count=rejected_count,
            )

            db.add(production)

        if status == "Breakdown":
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
            "machine": {
                "id": machine.id,
                "name": machine.name,
                "status": machine.status,
                "utilization": machine.utilization,
                "downtime": machine.downtime,
            },
            "production": {
                "total_count": total_count,
                "good_count": good_count,
                "rejected_count": rejected_count,
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