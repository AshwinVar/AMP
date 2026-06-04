import random
import time
import requests

API_URL = "http://127.0.0.1:8000"

USERNAME = "admin_new"
PASSWORD = "admin123"


def login():
    res = requests.post(
        f"{API_URL}/login",
        json={
            "username": USERNAME,
            "password": PASSWORD
        }
    )

    res.raise_for_status()
    return res.json()["access_token"]


def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


def get_machines(token):
    res = requests.get(
        f"{API_URL}/machines",
        headers=auth_headers(token)
    )

    res.raise_for_status()
    return res.json()


def update_machine_status(token, machine_id, status):
    requests.patch(
        f"{API_URL}/machines/{machine_id}/status?status={status}",
        headers=auth_headers(token)
    )


def add_production_record(token, machine_id):
    planned = 480
    runtime = random.randint(300, 470)
    total = random.randint(250, 500)
    rejected = random.randint(0, 30)
    good = total - rejected

    payload = {
        "machine_id": machine_id,
        "planned_minutes": planned,
        "runtime_minutes": runtime,
        "ideal_cycle_time_seconds": 60,
        "total_count": total,
        "good_count": good,
        "rejected_count": rejected
    }

    requests.post(
        f"{API_URL}/production-records",
        json=payload,
        headers=auth_headers(token)
    )


def add_downtime(token, machine_id, status):
    if status == "Breakdown":
        payload = {
            "machine_id": machine_id,
            "reason": random.choice([
                "Breakdown",
                "Tool Change",
                "Quality Issue",
                "Material Shortage"
            ]),
            "duration": f"{random.randint(5, 45)} min",
            "notes": "Auto-generated live simulator event"
        }

        requests.post(
            f"{API_URL}/downtime-logs",
            json=payload,
            headers=auth_headers(token)
        )


def run():
    token = login()
    print("Live simulator connected.")

    while True:
        machines = get_machines(token)

        if not machines:
            print("No machines found. Add machines in dashboard first.")
            time.sleep(5)
            continue

        for machine in machines:
            status = random.choice([
                "Running",
                "Running",
                "Running",
                "Idle",
                "Maintenance",
                "Breakdown"
            ])

            update_machine_status(
                token,
                machine["id"],
                status
            )

            add_production_record(
                token,
                machine["id"]
            )

            add_downtime(
                token,
                machine["id"],
                status
            )

            print(
                f"Updated {machine['name']} → {status}"
            )

        time.sleep(10)


if __name__ == "__main__":
    run()