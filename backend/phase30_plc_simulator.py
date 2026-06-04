import json
import random
import time
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883
TOPIC = "flowmes/machines"

MACHINES = ["CNC-01", "CNC-02", "Laser-Cutter-01", "Packaging-01", "Assembly-Robot-01"]

def build_payload(machine: str):
    status = random.choices(["Running", "Idle", "Maintenance", "Breakdown"], weights=[70, 15, 10, 5])[0]
    utilization = random.randint(35, 95) if status == "Running" else random.randint(0, 55)
    downtime = "0 min" if status == "Running" else f"{random.randint(5, 45)} min"
    total = random.randint(200, 500)
    rejected = random.randint(0, 20)
    return {
        "machine": machine,
        "status": status,
        "utilization": utilization,
        "downtime": downtime,
        "planned_minutes": 480,
        "runtime_minutes": random.randint(250, 470),
        "ideal_cycle_time_seconds": 60,
        "total_count": total,
        "good_count": total - rejected,
        "rejected_count": rejected,
        "temperature": random.randint(28, 82),
        "vibration": random.randint(1, 12),
        "source": "phase30_plc_simulator",
    }

def main():
    client = mqtt.Client()
    client.connect(BROKER, PORT, 60)
    print("Phase 30 PLC simulator started")
    print(f"Publishing to {BROKER}:{PORT} topic={TOPIC}")

    while True:
        machine = random.choice(MACHINES)
        payload = build_payload(machine)
        client.publish(TOPIC, json.dumps(payload))
        print("PLC SIM →", payload)
        time.sleep(2)

if __name__ == "__main__":
    main()
