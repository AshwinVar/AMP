import json
import paho.mqtt.client as mqtt
from sqlalchemy.orm import Session

from database import SessionLocal
import models

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
TOPIC = "flowmes/machines"


def get_or_create_machine(db: Session, name: str):
    machine = db.query(models.Machine).filter(
        models.Machine.name == name
    ).first()

    if machine:
        return machine

    machine = models.Machine(
        name=name,
        status="Idle",
        utilization=0,
        downtime="0 min"
    )

    db.add(machine)
    db.commit()
    db.refresh(machine)

    print(f"CREATED MACHINE → {machine.name}")

    return machine


def on_connect(client, userdata, flags, rc):
    print(f"MQTT CONNECTED | result code: {rc}")

    if rc == 0:
        client.subscribe(TOPIC)
        print(f"SUBSCRIBED → {TOPIC}")
    else:
        print("MQTT connection failed")


def on_message(client, userdata, msg):
    print("\nRAW MQTT MESSAGE RECEIVED")
    print("Topic:", msg.topic)
    print("Payload:", msg.payload.decode())

    db = SessionLocal()

    try:
        payload = json.loads(msg.payload.decode())

        machine_name = payload.get("machine")
        if not machine_name:
            print("SKIPPED: missing machine")
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
                notes="MQTT auto-generated downtime event"
            )
            db.add(downtime)

        db.commit()

        print(
            f"DB UPDATED → {machine.name} | "
            f"{old_status} → {status} | "
            f"{old_utilization}% → {utilization}% | "
            f"Downtime: {downtime_value}"
        )

        broadcast_live_event({
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
        "source": "mqtt"
})



    except Exception as e:
        db.rollback()
        print("MQTT DB ERROR:", repr(e))

    finally:
        db.close()


client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

print("CONNECTING TO MQTT BROKER...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

print("FLOWMES MQTT LISTENER RUNNING...")
client.loop_forever()