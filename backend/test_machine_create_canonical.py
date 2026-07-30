"""POST /machines canonicalises status + clamps utilization at the create boundary.

Every status-based rollup (build_management_summary's breakdown_count,
build_smart_alerts, the Machine-Health twin's bands, the predictive risk score)
matches the EXACT canonical strings in machine_status.VALID_MACHINE_STATUSES, so
a raw "RUNNING" / "breakdown" / typo written straight onto machine.status
silently drops the machine from all of them; an out-of-range utilization (150,
-5) is a percentage the data can't support. The manual PATCH endpoint and every
ingest path (IoT / MQTT / industrial / CSV import) already guard both through the
shared machine_status helpers — this pins that the direct create path
(create_machine) does too, the one write path that previously wrote the client's
raw values verbatim.

Run:  python backend/test_machine_create_canonical.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import machines_routes
import schemas
from analytics_engine import build_management_summary, build_smart_alerts
from database import Base
from machine_status import VALID_MACHINE_STATUSES, normalize_machine_status

_ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _create(db, status, utilization, name="M1"):
    payload = schemas.MachineCreate(
        name=name, status=status, utilization=utilization, downtime="0 min")
    return machines_routes.create_machine(payload, db=db, current_user=_ADMIN)


def test_lowercase_status_stored_canonically():
    db = _sess()
    # A lowercase "breakdown" is a recognised status in the wrong case. It must be
    # stored as the canonical "Breakdown", not the raw "breakdown" that
    # `machine.status == "Breakdown"` would miss.
    m = _create(db, "breakdown", 50)
    assert m.status == "Breakdown", m.status
    assert normalize_machine_status(m.status) == m.status  # already canonical
    print("PASS create with 'breakdown' stores canonical 'Breakdown'")


def test_whitespace_and_mixed_case_canonicalise():
    db = _sess()
    m = _create(db, "  RuNnInG  ", 50)
    assert m.status == "Running", m.status
    print("PASS create with '  RuNnInG  ' stores 'Running'")


def test_unrecognised_status_is_rejected_and_writes_nothing():
    db = _sess()
    # "Broken" is not in the vocabulary. The old handler wrote it straight through,
    # dropping the machine from every status-based report. It must 400 instead and
    # persist no row.
    try:
        _create(db, "Broken", 50)
        assert False, "unrecognised status should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    db.rollback()
    assert db.query(models.Machine).count() == 0, "a rejected create must not persist a machine"
    print("PASS create with 'Broken' 400s and persists no machine")


def test_utilization_is_clamped_into_range():
    db = _sess()
    # Independently derived: clamp_utilization maps 150 -> 100 and -5 -> 0
    # (max(0, min(100, round(v)))), so neither an over-100 nor a negative
    # percentage is ever stored — the same clamp the CSV-import onboarding path
    # applies. A valid 73 is stored verbatim.
    assert _create(db, "Running", 150, name="A").utilization == 100
    assert _create(db, "Running", -5, name="B").utilization == 0
    assert _create(db, "Running", 73, name="C").utilization == 73
    print("PASS create clamps utilization: 150->100, -5->0, 73 unchanged")


def test_every_valid_status_round_trips():
    # Each canonical status is accepted verbatim and stored exactly, so the create
    # path can still set any real state the UI offers.
    for i, st in enumerate(VALID_MACHINE_STATUSES):
        db = _sess()
        m = _create(db, st, 40, name=f"M{i}")
        assert m.status == st, (st, m.status)
    print(f"PASS all {len(VALID_MACHINE_STATUSES)} canonical statuses round-trip on create")


def test_created_breakdown_machine_is_seen_by_status_rollups():
    # The end-to-end point of the fix: a created breakdown machine (via lowercase
    # input) must be seen by the status-based rollups. Independently derived: ONE
    # breakdown machine created -> breakdown_count == 1 and a Critical "Breakdown"
    # smart alert for it (both key off the canonical status == "Breakdown"). With
    # the raw "breakdown" the old code stored, both would have been 0 / absent.
    # Utilization 60 keeps the low-utilization alert (a different rule) from firing,
    # so the assertion isolates the status-based path.
    db = _sess()
    _create(db, "breakdown", 60, name="Press-9")
    machines = db.query(models.Machine).all()
    summary = build_management_summary(machines, [], [], [])
    assert summary["breakdown_count"] == 1, summary["breakdown_count"]
    alerts = build_smart_alerts(machines, [], [])
    down = [a for a in alerts
            if a["machine"] == "Press-9" and a["type"] == "Breakdown" and a["severity"] == "Critical"]
    assert down, "a created breakdown machine must raise a Critical Breakdown smart alert"
    print("PASS created breakdown machine is counted by breakdown_count + smart alerts")


if __name__ == "__main__":
    test_lowercase_status_stored_canonically()
    test_whitespace_and_mixed_case_canonicalise()
    test_unrecognised_status_is_rejected_and_writes_nothing()
    test_utilization_is_clamped_into_range()
    test_every_valid_status_round_trips()
    test_created_breakdown_machine_is_seen_by_status_rollups()
    print("MACHINE CREATE CANONICAL OK: create path matches the ingest/PATCH guard")
