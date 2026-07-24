"""Industrial-IoT route registration test (ADR-0009).

IoT telemetry + industrial devices / signals / PLC mappings live in
industrial_iot_routes.register(app), peeled out of main.py. Guards registration
+ sole ownership by the module.

Run:  python backend/test_industrial_iot_routes.py     (exit 0 = pass)
"""
import main

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import industrial_iot_routes
import machine_status
import models
import schemas
from database import Base

EXPECTED = {
    "/iot/telemetry",
    "/industrial/devices",
    "/industrial/devices/{device_id}",
    "/industrial/signals",
    "/industrial/mappings",
}


def test_industrial_iot_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"industrial-iot paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"industrial_iot_routes"}}
    assert not wrong, f"industrial-iot paths not owned solely by industrial_iot_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} industrial-iot paths owned by industrial_iot_routes")


# --- Behavioural tests: the ingest handler must not corrupt machine state from
# raw edge telemetry (unclamped utilization / arbitrary status string). ---

def _db_with_machine(status="Running", util=80):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="M1", status=status, utilization=util))
    db.commit()
    return db


def _machine(db):
    return db.query(models.Machine).filter(models.Machine.id == 1).first()


def _telemetry(**kw):
    kw.setdefault("signal_value", "")
    kw.setdefault("numeric_value", 0)
    return schemas.IoTTelemetryCreate(machine_id=1, **kw)


def _post(db, **kw):
    return industrial_iot_routes.create_iot_telemetry(_telemetry(**kw), db=db, current_user={})


def test_iot_utilization_is_clamped_to_percentage_range():
    db = _db_with_machine()
    _post(db, signal_name="utilization", numeric_value=150)
    assert _machine(db).utilization == 100          # not the raw 150
    _post(db, signal_name="Load", numeric_value=-20)
    assert _machine(db).utilization == 0            # not the raw -20
    _post(db, signal_name="efficiency", numeric_value=67)
    assert _machine(db).utilization == 67           # in-range value applied as-is
    print("PASS IoT utilization is clamped into [0, 100], never shown as >100% or negative")


def test_iot_unknown_status_does_not_corrupt_machine_state():
    db = _db_with_machine(status="Running")
    # a recognised status (any case) is normalised and applied, with an event
    _post(db, signal_name="status", signal_value="breakdown")
    assert _machine(db).status == "Breakdown"
    # an UNRECOGNISED status is ignored, not written — otherwise the machine would
    # silently vanish from every status-based report
    _post(db, signal_name="status", signal_value="Frobnicate")
    assert _machine(db).status == "Breakdown"       # unchanged, not "Frobnicate"
    # every MachineEvent that WAS written carries a canonical status
    events = db.query(models.MachineEvent).all()
    assert events and all(e.new_status in machine_status.VALID_MACHINE_STATUSES for e in events)
    # and the telemetry row itself is still recorded regardless
    assert db.query(models.IoTTelemetry).count() == 2
    print("PASS IoT status ingest normalises known statuses and ignores unknown (no corruption)")


if __name__ == "__main__":
    test_industrial_iot_paths_owned_by_module()
    test_iot_utilization_is_clamped_to_percentage_range()
    test_iot_unknown_status_does_not_corrupt_machine_state()
    print("ALL INDUSTRIAL-IOT ROUTE TESTS PASSED")
