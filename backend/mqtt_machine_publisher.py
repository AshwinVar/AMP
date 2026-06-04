import json
import random
import time
import paho.mqtt.client as mqtt

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
TOPIC = "flowmes/machines"

machines = [
    "CNC-01",
    "CNC-02",
    "Packaging-01",
    "Laser-Cutter-01"
]

statuses = [
    "Running",
    "Running",
    "Running",
    "Idle",
    "Maintenance",
    "Breakdown"
]


client = mqtt.Client()
client.connect(MQTT_BROKER, MQTT_PORT, 60)

print("MQTT machine publisher started...")


while True:
    for machine in machines:
        status = random.choice(statuses)

        total_count = random.randint(200, 500)
        rejected_count = random.randint(0, 25)
        good_count = total_count - rejected_count

        payload = {
            "machine": machine,
            "status": status,
            "utilization": random.randint(35, 95),
            "downtime": f"{random.randint(5, 40)} min" if status == "Breakdown" else "0 min",
            "planned_minutes": 480,
            "runtime_minutes": random.randint(300, 470),
            "ideal_cycle_time_seconds": 60,
            "total_count": total_count,
            "good_count": good_count,
            "rejected_count": rejected_count
        }

        client.publish(
            TOPIC,
            json.dumps(payload)
        )

        print("Published:", payload)

    time.sleep(10)