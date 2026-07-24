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


def _send(status, downtime="30 min"):
    # on_message prints progress lines containing '→'; capture them so a
    # cp1252 (Windows) console can't raise UnicodeEncodeError mid-handler.
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _Msg(
            {"machine": "PRESS-01", "status": status, "utilization": 50, "downtime": downtime}))


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


if __name__ == "__main__":
    test_breakdown_logs_once_per_transition_not_per_message()
    print("MQTT SERVICE OK: downtime logged once per breakdown event")
