"""MQTT ingest tests — one DowntimeLog per breakdown EVENT, not per message.

mqtt_service.on_message is the app-wired MQTT callback (main.py starts it at
boot). A PLC gateway publishes on an interval, so a machine that stays in
Breakdown emits many messages; the handler must write a downtime row only on the
transition INTO Breakdown. Gating on status alone wrote a new row every tick —
each re-asserting the full downtime string — inflating downtime counts/minutes,
MTBF/MTTR and the predictive risk score without bound.

Run:  python backend/test_mqtt_service.py
"""
import io
import json
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import mqtt_service
from database import Base


class _Msg:
    topic = "flowmes/machines"

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    mqtt_service.safe_broadcast = lambda event: None   # no websocket in a unit test
    return mqtt_service.SessionLocal


def _send(status, downtime="30 min", utilization=50):
    # on_message prints progress lines containing '→'; capture them so a
    # cp1252 (Windows) console can't raise UnicodeEncodeError mid-handler.
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _Msg(
            {"machine": "PRESS-01", "status": status,
             "utilization": utilization, "downtime": downtime}))


def _machine(Session):
    db = Session()
    try:
        return db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    finally:
        db.close()


def test_breakdown_logs_once_per_transition_not_per_message():
    Session = _setup()
    _send("Running")     # Idle (created) -> Running: no downtime
    _send("Breakdown")   # -> Breakdown: 1 downtime row
    _send("Breakdown")   # still Breakdown: NO new row (was the bug)
    _send("Breakdown")   # still Breakdown: NO new row
    _send("Running")     # recovered: no row
    _send("Breakdown")   # -> Breakdown again: 1 more row

    db = Session()
    logs = db.query(models.DowntimeLog).all()
    # Before the fix this was 4 (one per Breakdown message); now 2 — one per
    # transition INTO Breakdown.
    assert len(logs) == 2, len(logs)
    assert all(l.reason == "Breakdown" for l in logs)
    assert db.query(models.Machine).count() == 1     # created once, reused
    print("PASS MQTT writes one DowntimeLog per breakdown transition, not per message")


def test_lowercase_breakdown_is_canonicalised_and_logs_downtime():
    # A gateway that publishes "breakdown" (lowercase) must be treated exactly
    # like "Breakdown": the machine's status is stored canonically (so it still
    # appears in every status-based report) AND a DowntimeLog is written. Before
    # the fix the raw string was stored and the case-sensitive breakdown check
    # skipped the downtime row entirely.
    Session = _setup()
    _send("running")     # created Idle -> canonical "Running"
    _send("breakdown")   # lowercase -> canonical "Breakdown" + 1 downtime row

    machine = _machine(Session)
    assert machine.status == "Breakdown", machine.status

    db = Session()
    logs = db.query(models.DowntimeLog).all()
    db.close()
    assert len(logs) == 1, len(logs)
    assert logs[0].reason == "Breakdown"
    print("PASS MQTT canonicalises a lowercase status and still logs the downtime")


def test_unrecognised_status_leaves_machine_untouched():
    # An unknown status ("faulted") is NOT a machine state; writing it raw would
    # drop the machine from every status-based rollup. The handler must keep the
    # last known status instead — and emit no phantom transition event.
    Session = _setup()
    _send("Running")
    _send("faulted")     # unknown -> ignored, stays Running

    machine = _machine(Session)
    assert machine.status == "Running", machine.status

    db = Session()
    events = db.query(models.MachineEvent).all()
    db.close()
    # Only the Idle -> Running transition; "faulted" produced no event.
    assert len(events) == 1, [(e.old_status, e.new_status) for e in events]
    assert events[0].new_status == "Running"
    print("PASS MQTT ignores an unrecognised status and emits no phantom event")


def test_out_of_range_utilization_is_clamped():
    # A glitching sensor value is clamped into [0, 100] on ingest, never stored
    # raw (which would surface as a >100% / negative utilization downstream).
    Session = _setup()
    _send("Running", utilization=150)
    assert _machine(Session).utilization == 100, _machine(Session).utilization

    _send("Running", utilization=-20)
    assert _machine(Session).utilization == 0, _machine(Session).utilization

    # A non-numeric reading isn't a number: keep the previous value rather than
    # dropping the whole message on an int() ValueError.
    _send("Running", utilization=55)
    _send("Running", utilization="not-a-number")
    assert _machine(Session).utilization == 55, _machine(Session).utilization
    print("PASS MQTT clamps utilization into [0, 100] and keeps the last value on garbage")


if __name__ == "__main__":
    test_breakdown_logs_once_per_transition_not_per_message()
    test_lowercase_breakdown_is_canonicalised_and_logs_downtime()
    test_unrecognised_status_leaves_machine_untouched()
    test_out_of_range_utilization_is_clamped()
    print("MQTT SERVICE OK: downtime logged once per breakdown event")
