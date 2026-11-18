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


# --- numeric_value must fit the INTEGER column that stores it, or be refused at
# ingest (a clean 422) rather than 500 the write mid-commit on PostgreSQL. ---

from pydantic import ValidationError

# The 32-bit signed INTEGER range, derived from the column type (Column(Integer) ->
# int4 on PostgreSQL), NOT read back from the code under test.
INT32_MAX = 2_147_483_647          # 2**31 - 1
INT32_MIN = -2_147_483_648         # -(2**31)


def _creates(numeric_value):
    """Build both ingest schemas with the same numeric_value; each must agree."""
    return (
        lambda: schemas.IoTTelemetryCreate(
            machine_id=1, signal_name="pressure", signal_value="x",
            numeric_value=numeric_value),
        lambda: schemas.IndustrialSignalCreate(
            device_id=1, signal_name="pressure", signal_value="x",
            numeric_value=numeric_value),
    )


def test_numeric_value_outside_int32_is_rejected_not_500():
    # One past the max, a value well past it, and one past the min: each would
    # commit on SQLite but raise DataError on Postgres, so each must be refused at
    # the schema (ValidationError -> 422) before the INSERT is ever attempted.
    for bad in (INT32_MAX + 1, 3_000_000_000, INT32_MIN - 1, -5_000_000_000):
        for build in _creates(bad):
            raised = False
            try:
                build()
            except ValidationError:
                raised = True
            assert raised, f"numeric_value {bad} should be refused, not accepted for a 32-bit INTEGER write"
    # A JSON "1e999" decodes to float('inf'); int coercion already refuses it, so
    # the whole out-of-range class (finite AND infinite) is covered.
    for build in _creates(float("inf")):
        raised = False
        try:
            build()
        except ValidationError:
            raised = True
        assert raised, "an infinite numeric_value must be refused, not stored"
    print("PASS an out-of-range numeric_value is refused at ingest (422), never a 500 mid-write")


def test_numeric_value_at_and_within_int32_bounds_is_accepted():
    # The exact column boundaries are valid readings and must pass untouched.
    for ok in (INT32_MAX, INT32_MIN, 0, 67):
        for build in _creates(ok):
            assert build().numeric_value == ok, f"{ok} fits the INTEGER column and must be accepted as-is"
    print(f"PASS the INTEGER-column boundary values ({INT32_MIN}..{INT32_MAX}) are accepted unchanged")


def test_a_legitimate_negative_reading_still_passes():
    # numeric_value is a raw sensor reading, not a counted quantity: a sub-zero
    # temperature (-40) or a negative differential pressure is real data. The bound
    # is symmetric precisely so this is not refused as a [0, MAX_QTY] quantity would be.
    for build in _creates(-40):
        assert build().numeric_value == -40, "a legitimate negative reading must not be rejected"
    print("PASS a legitimate negative reading (-40) is preserved, not refused")


if __name__ == "__main__":
    test_industrial_iot_paths_owned_by_module()
    test_iot_utilization_is_clamped_to_percentage_range()
    test_iot_unknown_status_does_not_corrupt_machine_state()
    test_numeric_value_outside_int32_is_rejected_not_500()
    test_numeric_value_at_and_within_int32_bounds_is_accepted()
    test_a_legitimate_negative_reading_still_passes()
    print("ALL INDUSTRIAL-IOT ROUTE TESTS PASSED")
