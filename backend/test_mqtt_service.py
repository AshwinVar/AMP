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


def _send_payload(payload):
    base = {"machine": "PRESS-01", "status": "Running", "utilization": 50}
    base.update(payload)
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _Msg(base))


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


def test_valid_production_counts_are_recorded():
    # Positive path — a physically-valid reading (good + rejected == total, all
    # non-negative) IS recorded, so the guard below can't be a blanket reject.
    Session = _setup()
    _send_payload({"total_count": 100, "good_count": 95, "rejected_count": 5,
                   "planned_minutes": 480, "runtime_minutes": 420,
                   "ideal_cycle_time_seconds": 60})

    db = Session()
    records = db.query(models.ProductionRecord).all()
    db.close()
    assert len(records) == 1, len(records)
    r = records[0]
    assert (r.total_count, r.good_count, r.rejected_count) == (100, 95, 5)
    assert (r.planned_minutes, r.runtime_minutes, r.ideal_cycle_time_seconds) == (480, 420, 60)
    print("PASS MQTT records a valid production reading")


def test_negative_counts_skip_production_but_keep_status():
    # A glitching gateway can publish NEGATIVE counts that STILL satisfy
    # good + rejected == total (-5 + 15 == 10), so the equality check alone lets
    # them through — and a negative good_count drags pooled OEE below zero
    # (quality = good/total is not floored). The record must be skipped, while
    # the rest of the message (status) still applies.
    Session = _setup()
    _send_payload({"status": "Running", "total_count": 10,
                   "good_count": -5, "rejected_count": 15})

    db = Session()
    records = db.query(models.ProductionRecord).all()
    machine = db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    db.close()
    assert len(records) == 0, len(records)          # negative row NOT written
    assert machine.status == "Running"              # status still applied
    print("PASS MQTT skips a negative-count production row and still applies status")


def test_non_numeric_count_skips_production_not_the_whole_message():
    # A non-numeric count ("--") used to raise int() mid-handler, aborting the
    # whole message. Now it's treated as "no usable value": production is skipped
    # but the machine's status/utilization update still lands.
    Session = _setup()
    _send_payload({"status": "Running", "utilization": 77,
                   "total_count": "--", "good_count": 5, "rejected_count": 0})

    db = Session()
    records = db.query(models.ProductionRecord).all()
    machine = db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    db.close()
    assert len(records) == 0, len(records)
    assert machine.status == "Running"
    assert machine.utilization == 77, machine.utilization
    print("PASS MQTT survives a non-numeric count and still applies status/utilization")


def test_infinite_count_skips_production_and_still_logs_the_breakdown():
    # JSON permits Infinity, and json.loads decodes it to float('inf'), so an edge
    # gateway with a disconnected analog input can publish {"total_count": Infinity}.
    # int(float('inf')) raises OverflowError, which the OLD _non_negative_int did NOT
    # catch (only TypeError/ValueError) — so the exception escaped mid-handler and
    # aborted the WHOLE message: the Idle->Breakdown transition's DowntimeLog and the
    # status write were lost along with the (rightly-skipped) production record.
    #
    # Build the encoded payload directly so the wire bytes carry the literal JSON
    # token `Infinity`, which json.loads (as on_message calls it) decodes to
    # float('inf') — the exact value a real gateway would put on the topic.
    Session = _setup()
    _send("Running")     # created Idle -> Running (baseline transition)

    class _RawMsg:
        topic = "flowmes/machines"
        payload = (
            b'{"machine": "PRESS-01", "status": "Breakdown", "downtime": "30 min", '
            b'"total_count": Infinity, "good_count": 5, "rejected_count": 5}'
        )

    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _RawMsg())

    db = Session()
    records = db.query(models.ProductionRecord).all()
    logs = db.query(models.DowntimeLog).all()
    machine = db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    db.close()

    assert len(records) == 0, len(records)           # infinite count -> no phantom record
    assert machine.status == "Breakdown", machine.status  # status still applied
    # The transition INTO Breakdown must still have written its downtime row — the
    # infinite production count must not swallow the rest of the message.
    assert len(logs) == 1, len(logs)
    assert logs[0].reason == "Breakdown"
    print("PASS MQTT survives an infinite count, skips only production, still logs the breakdown")


def test_non_negative_int_maps_infinity_and_nan_to_none():
    # Unit-level pin on the guard itself: every non-usable numeric maps to None (so
    # production_valid is False and the record is skipped), never an exception.
    assert mqtt_service._non_negative_int(float("inf")) is None
    assert mqtt_service._non_negative_int(float("-inf")) is None
    assert mqtt_service._non_negative_int(float("nan")) is None
    assert mqtt_service._non_negative_int("--") is None
    assert mqtt_service._non_negative_int(None) is None
    assert mqtt_service._non_negative_int(-4) is None        # negative -> not usable
    # ...and a valid non-negative reading still passes through unchanged.
    assert mqtt_service._non_negative_int(0) == 0
    assert mqtt_service._non_negative_int(95) == 95
    assert mqtt_service._non_negative_int(3.9) == 3          # float truncates, as before
    print("PASS _non_negative_int maps inf/-inf/NaN/garbage to None, keeps valid counts")


if __name__ == "__main__":
    test_breakdown_logs_once_per_transition_not_per_message()
    test_lowercase_breakdown_is_canonicalised_and_logs_downtime()
    test_unrecognised_status_leaves_machine_untouched()
    test_out_of_range_utilization_is_clamped()
    test_valid_production_counts_are_recorded()
    test_negative_counts_skip_production_but_keep_status()
    test_non_numeric_count_skips_production_not_the_whole_message()
    test_infinite_count_skips_production_and_still_logs_the_breakdown()
    test_non_negative_int_maps_infinity_and_nan_to_none()
    print("MQTT SERVICE OK: downtime logged once per breakdown event")
