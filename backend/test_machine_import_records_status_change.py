"""A machine CSV import that changes a machine's status records the change.

THE DEFECT
----------
Every path that changes a machine's status writes a MachineEvent (old status,
new status, source): PATCH /machines/{id}/status ("manual"), MQTT ingest
("mqtt"), the industrial gateway and the simulator ("simulator"). That event
stream is the machine's status history. The machine timeline and state summary
read it, the rule-based risk scorer reads it, and the AMP-native failure-risk
model learns from its transitions (amp_ai/failure_risk/db_history.py).

POST /machines/import-csv updated an existing machine's `status` directly and
wrote nothing. A roster import that marked a machine Breakdown changed what every
status tile shows, while the history said the machine never left Running, so
the next event's `old_status` contradicted the one before it.

THE RULE
--------
An import row that CHANGES an existing machine's status writes a MachineEvent
with source "import", inside the row's savepoint (a row that fails leaves no
event). A row that leaves the status as it was writes none, and neither does
creating a machine (POST /machines writes none either: a machine's first status
is not a change).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_import_records_status_change.py
"""
import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import machines_routes
import models
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class _Upload:
    """Stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text: str):
        self._data = text.encode("utf-8")

    async def read(self):
        return self._data


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(name="CNC-1", status="Running", utilization=70, line="SMT"))
    db.add(models.Machine(name="PRESS-2", status="Idle", utilization=10, line="SMT"))
    db.commit()
    return db


def _import(db, text):
    return asyncio.run(machines_routes.import_machines_csv(file=_Upload(text), db=db, current_user={}))


def _events(db):
    return [(e.machine_name, e.old_status, e.new_status, e.source)
            for e in db.query(models.MachineEvent).order_by(models.MachineEvent.id)]


def main():
    print("=" * 74)
    print("1. AN IMPORT THAT CHANGES A STATUS RECORDS IT")
    print("=" * 74)
    db = _session()
    r = _import(db, "name,status,utilization\n"
                    "CNC-1,Breakdown,0\n"      # Running -> Breakdown: a change
                    "PRESS-2,Idle,15\n"        # Idle -> Idle: not a change
                    "LATHE-9,Running,50\n")    # a new machine: no prior status
    check("the import itself succeeded", (r["updated"], r["created"], r["errors"]) == (2, 1, []), str(r))
    events = _events(db)
    check("CNC-1's change is in its history, marked as an import",
          ("CNC-1", "Running", "Breakdown", "import") in events, str(events))
    check("the machine's own row agrees with the history",
          db.query(models.Machine).filter(models.Machine.name == "CNC-1").one().status == "Breakdown")
    check("an unchanged status writes no event", not [e for e in events if e[0] == "PRESS-2"], str(events))
    check("creating a machine writes no event (it did not change)",
          not [e for e in events if e[0] == "LATHE-9"], str(events))
    check("exactly one event in all", len(events) == 1, str(events))
    db.close()

    print()
    print("=" * 74)
    print("2. THE HISTORY STAYS A CHAIN: EACH EVENT STARTS WHERE THE LAST ENDED")
    print("=" * 74)
    db = _session()
    _import(db, "name,status\nCNC-1,Breakdown\n")
    machine = db.query(models.Machine).filter(models.Machine.name == "CNC-1").one()
    machines_routes.update_machine_status(machine.id, "Running", db=db, current_user={"role": "Admin"})
    events = _events(db)
    check("the manual change after the import starts from Breakdown, not Running",
          events == [("CNC-1", "Running", "Breakdown", "import"), ("CNC-1", "Breakdown", "Running", "manual")],
          str(events))
    db.close()

    print()
    print("=" * 74)
    print("3. THE CHANGE AND ITS RECORD ARE ONE: A ROW THAT FAILS LEAVES NEITHER")
    print("=" * 74)
    db = _session()
    real = models.MachineEvent

    class _Refuses:
        def __init__(self, **kw):
            raise RuntimeError("the history could not be written")

    models.MachineEvent = _Refuses
    try:
        r = _import(db, "name,status\nCNC-1,Breakdown\n")
    finally:
        models.MachineEvent = real
    db.expire_all()
    check("the row is reported as an error", len(r["errors"]) == 1 and r["updated"] == 0, str(r))
    check("...and its status change was rolled back with it",
          db.query(models.Machine).filter(models.Machine.name == "CNC-1").one().status == "Running")
    db.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_machine_import_records_status_change():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
