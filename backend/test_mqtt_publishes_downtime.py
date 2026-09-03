"""A machine that reports its OWN breakdown reaches the event stream (ADR-0001/0003).

THE GAP THIS CLOSES
-------------------
`mqtt_service` wrote a `DowntimeLog` on the transition into Breakdown and
published NOTHING — the whole file contained zero references to `event_bus`. The
HTTP path (`POST /downtime-logs`, machines_routes.py:165) has always published
`DowntimeStarted`.

So the platform reacted to downtime a HUMAN typed in and was blind to downtime a
MACHINE reported. The Escalation agent watches `DowntimeStarted` for repeated
stoppages; on any factory whose machines report over MQTT — which is the whole
point of the product — it never fired. Nothing failed, no error appeared: the
agent simply never had anything to watch.

WHY THE PUBLISH IS GUARDED, WHEN THE HTTP PATH'S IS NOT
-------------------------------------------------------
`event_bus.publish` dispatches handlers synchronously inside the caller's
transaction and does not catch their exceptions. On the HTTP path that is
correct: the request fails, nothing commits, the human retries.

MQTT has no human and no retry. A subscriber raising there would roll the
committed stoppage away and the telemetry would be gone for good — inverting the
invariant test_mqtt_resilience.py exists to protect ("a broken reaction costs the
reaction, never the row"). So the publish is wrapped: the event is still written
to `event_log` and dispatched before the commit, but a failing subscriber is
logged and the ingest proceeds.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_publishes_downtime.py
"""
import io
import json
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
from database import Base
from events import event_bus, DowntimeStarted

_TENANT = "FACTORY_A"
_TOPIC = f"flowmes/{_TENANT}/-/machines"

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class _Msg:
    def __init__(self, payload):
        self.topic = _TOPIC
        self.payload = payload


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    db = mqtt_service.SessionLocal()
    db.add(models.TenantConfig(tenant_code=_TENANT))
    db.commit()
    db.close()
    mqtt_service.safe_broadcast = lambda e: None
    return mqtt_service.SessionLocal


def _send(**fields):
    payload = {"machine": "PRESS-01", "status": "Running", "utilization": 50,
               "downtime": "30 min"}
    payload.update(fields)
    buf = io.StringIO()
    with redirect_stdout(buf):
        mqtt_service.on_message(None, None, _Msg(json.dumps(payload).encode()))


def _subscribe(seen):
    """Record every DowntimeStarted the bus dispatches."""
    def handler(event, db=None):
        seen.append(event)
    event_bus.subscribe(DowntimeStarted, handler)
    return handler


def main():
    print("=" * 74)
    print("1. A MACHINE-REPORTED BREAKDOWN PUBLISHES DowntimeStarted")
    print("=" * 74)
    Session = _setup()
    seen = []
    _subscribe(seen)

    _send(status="Running")                       # establish Idle -> Running
    check("CONTROL: a running packet publishes nothing", len(seen) == 0, str(len(seen)))

    _send(status="Breakdown", utilization=0, downtime="12 min")
    check("the transition INTO Breakdown publishes exactly one event",
          len(seen) == 1, f"{len(seen)} events")

    if seen:
        e = seen[0]
        check("...carrying the MACHINE'S tenant, not a default",
              getattr(e, "tenant_code", None) == _TENANT, str(getattr(e, "tenant_code", None)))
        check("...and the machine id", getattr(e, "machine_id", None) is not None,
              str(getattr(e, "machine_id", None)))
        check("...and a reason", bool(getattr(e, "reason", "")), repr(getattr(e, "reason", None)))
        check("...typed as DowntimeStarted",
              getattr(e, "event_type", None) == "DowntimeStarted",
              str(getattr(e, "event_type", None)))

    print()
    print("=" * 74)
    print("2. IT FOLLOWS THE DowntimeLog RULE — one per breakdown, not per packet")
    print("=" * 74)
    # A PLC gateway republishes on an interval. The DowntimeLog is transition-
    # gated; the event must be gated identically or the Escalation agent sees a
    # fresh stoppage every few seconds and escalates a machine that broke once.
    _send(status="Breakdown", utilization=0)
    _send(status="Breakdown", utilization=0)
    check("staying in Breakdown publishes NO further events",
          len(seen) == 1, f"{len(seen)} events after 3 breakdown packets")

    db = Session()
    logs = db.query(models.DowntimeLog).count()
    db.close()
    check("...matching the DowntimeLog count exactly", logs == len(seen),
          f"{logs} logs vs {len(seen)} events")

    _send(status="Running", utilization=60)
    _send(status="Breakdown", utilization=0)
    check("a GENUINE second breakdown publishes again", len(seen) == 2, str(len(seen)))

    print()
    print("=" * 74)
    print("3. A FAILING SUBSCRIBER MUST NOT COST THE TELEMETRY")
    print("=" * 74)
    # The invariant the whole ingest path is built on. On the HTTP path an
    # exploding subscriber correctly fails the request; here it must not.
    Session = _setup()
    boom = []

    def explode(event, db=None):
        boom.append(event)
        raise RuntimeError("subscriber exploded")

    event_bus.subscribe(DowntimeStarted, explode)

    raised = None
    try:
        _send(status="Running")
        _send(status="Breakdown", utilization=0, downtime="9 min")
    except BaseException as e:                      # noqa: BLE001 - reporting it
        raised = repr(e)

    check("the exploding subscriber ran", len(boom) >= 1, str(len(boom)))
    check("ingest did not raise", raised is None, str(raised))

    db = Session()
    dt = db.query(models.DowntimeLog).count()
    m = db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    db.close()
    check("THE DOWNTIME LOG SURVIVED the failing subscriber", dt == 1, f"{dt} logs")
    check("...and the machine still reached Breakdown",
          m is not None and m.status == "Breakdown",
          str(getattr(m, "status", None)))

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


if __name__ == "__main__":
    raise SystemExit(main())
