"""
Run once against Railway DB to set machines to realistic active states and seed downtime logs.
Usage: DATABASE_URL=<public_url> python reset_machines.py
"""
from database import SessionLocal
import models

STATUSES = [
    {"status": "Running",   "utilization": 87, "downtime": "0 min"},
    {"status": "Running",   "utilization": 73, "downtime": "0 min"},
    {"status": "Running",   "utilization": 91, "downtime": "0 min"},
    {"status": "Breakdown", "utilization": 0,  "downtime": "2 hrs 15 min"},
    {"status": "Running",   "utilization": 68, "downtime": "0 min"},
]

DOWNTIME_SEED = [
    {"machine": "Packaging-01",     "reason": "Mechanical Failure", "duration": "2 hrs 15 min", "notes": "Drive belt snapped. Replacement ordered."},
    {"machine": "CNC-01",           "reason": "Tooling Change",     "duration": "45 min",        "notes": "Scheduled insert change between jobs."},
    {"machine": "CNC-02",           "reason": "Setup / Changeover", "duration": "30 min",        "notes": "Job changeover from SHAFT-001 to BEAR-003."},
    {"machine": "Laser-Cutter-01",  "reason": "Power Fluctuation",  "duration": "15 min",        "notes": "UPS tripped. Power restored, recalibrated."},
    {"machine": "CNC-01",           "reason": "Quality Hold",       "duration": "1 hr 10 min",   "notes": "Batch QI-7003 failed dimensional check. Rework in progress."},
    {"machine": "Assembly-Robot-01","reason": "Sensor Fault",       "duration": "50 min",        "notes": "End-effector proximity sensor error. Reset and tested OK."},
]

db = SessionLocal()

# Fix machine statuses
machines = db.query(models.Machine).order_by(models.Machine.id).all()
for i, machine in enumerate(machines):
    s = STATUSES[i % len(STATUSES)]
    machine.status = s["status"]
    machine.utilization = s["utilization"]
    machine.downtime = s["downtime"]
    print(f"  {machine.name} → {machine.status} ({machine.utilization}%)")
db.commit()

# Seed downtime logs if missing
if db.query(models.DowntimeLog).count() == 0:
    machine_map = {m.name: m.id for m in machines}
    for entry in DOWNTIME_SEED:
        mid = machine_map.get(entry["machine"])
        if not mid:
            continue
        db.add(models.DowntimeLog(
            machine_id=mid,
            reason=entry["reason"],
            duration=entry["duration"],
            notes=entry["notes"],
        ))
    db.commit()
    print("Downtime logs seeded.")
else:
    print("Downtime logs already exist, skipping.")

db.close()
print("Done.")
