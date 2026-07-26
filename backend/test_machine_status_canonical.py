"""Manual machine-status update canonicalises through the shared vocabulary.

PATCH /machines/{id}/status takes a raw ``status`` string. Every status-based
rollup (build_management_summary's breakdown_count, build_smart_alerts, the
Machine-Health twin's bands, the predictive risk score) matches the EXACT
canonical strings in machine_status.VALID_MACHINE_STATUSES, so writing a raw
"RUNNING" / "faulted" / typo straight onto machine.status silently drops the
machine from all of them. The ingest paths (IoT / MQTT / industrial) already
guard this via normalize_machine_status; this pins that the manual endpoint now
does too — a recognised-but-noncanonical value is stored canonically, and an
unrecognised one is a 400 (not a silently-corrupting write), leaving the machine
untouched.

Run:  python backend/test_machine_status_canonical.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import machines_routes
from database import Base
from machine_status import VALID_MACHINE_STATUSES, normalize_machine_status

_ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="M1", status="Idle", utilization=80))
    db.commit()
    return db


def _events(db):
    return db.query(models.MachineEvent).order_by(models.MachineEvent.id.asc()).all()


def test_recognised_but_noncanonical_is_stored_canonically():
    db = _sess()
    # A lowercase "breakdown" is a recognised status in the wrong case. It must be
    # stored as the canonical "Breakdown" so status-based rollups see it — NOT the
    # raw "breakdown", which `machine.status == "Breakdown"` would miss.
    m = machines_routes.update_machine_status(1, "breakdown", db=db, current_user=_ADMIN)
    assert m.status == "Breakdown", m.status
    assert normalize_machine_status(m.status) == m.status  # already canonical
    # The timeline records the canonical transition Idle -> Breakdown, once.
    evs = _events(db)
    assert len(evs) == 1, len(evs)
    assert (evs[0].old_status, evs[0].new_status) == ("Idle", "Breakdown"), (
        evs[0].old_status, evs[0].new_status)
    print("PASS 'breakdown' is stored canonically as 'Breakdown' (rollups see it)")


def test_whitespace_and_mixed_case_canonicalise():
    db = _sess()
    m = machines_routes.update_machine_status(1, "  RuNnInG  ", db=db, current_user=_ADMIN)
    assert m.status == "Running", m.status
    print("PASS '  RuNnInG  ' canonicalises to 'Running'")


def test_unrecognised_status_is_rejected_and_leaves_machine_untouched():
    db = _sess()
    # "Broken" is not in the vocabulary. The old handler wrote it straight through,
    # dropping the machine from every status-based report. It must 400 instead, and
    # must NOT mutate the machine or emit a spurious timeline event.
    try:
        machines_routes.update_machine_status(1, "Broken", db=db, current_user=_ADMIN)
        assert False, "unrecognised status should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    db.rollback()
    machine = db.query(models.Machine).filter(models.Machine.id == 1).first()
    assert machine.status == "Idle", machine.status  # untouched
    assert _events(db) == [], "a rejected status must not write a MachineEvent"
    print("PASS unrecognised 'Broken' 400s and leaves machine.status == 'Idle'")


def test_every_valid_status_round_trips():
    # Independently derived: each canonical status is accepted verbatim and stored
    # exactly, so the manual path can still set any real state the UI offers.
    for i, st in enumerate(VALID_MACHINE_STATUSES, start=1):
        db = _sess()
        m = machines_routes.update_machine_status(1, st, db=db, current_user=_ADMIN)
        assert m.status == st, (st, m.status)
    print(f"PASS all {len(VALID_MACHINE_STATUSES)} canonical statuses round-trip unchanged")


def test_no_op_same_status_writes_no_event():
    db = _sess()  # machine starts Idle
    m = machines_routes.update_machine_status(1, "Idle", db=db, current_user=_ADMIN)
    assert m.status == "Idle"
    assert _events(db) == [], "setting the same status must not emit a MachineEvent"
    print("PASS re-asserting the current status writes no MachineEvent")


if __name__ == "__main__":
    test_recognised_but_noncanonical_is_stored_canonically()
    test_whitespace_and_mixed_case_canonicalise()
    test_unrecognised_status_is_rejected_and_leaves_machine_untouched()
    test_every_valid_status_round_trips()
    test_no_op_same_status_writes_no_event()
    print("MACHINE STATUS CANONICAL OK: manual path matches the ingest guard")
