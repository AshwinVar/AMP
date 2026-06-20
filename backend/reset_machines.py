"""
Run this once against Railway DB to set machines to realistic active states.
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

db = SessionLocal()
machines = db.query(models.Machine).order_by(models.Machine.id).all()
for i, machine in enumerate(machines):
    s = STATUSES[i % len(STATUSES)]
    machine.status = s["status"]
    machine.utilization = s["utilization"]
    machine.downtime = s["downtime"]
    print(f"  {machine.name} → {machine.status} ({machine.utilization}%)")
db.commit()
db.close()
print("Done.")
