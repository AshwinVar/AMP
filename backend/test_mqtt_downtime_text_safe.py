"""A null / numeric / blank `downtime` from a gateway must still record the stop.

THE BUG THIS PINS
-----------------
`mqtt_service.on_message` read the gateway's `downtime` field RAW --
`payload.get("downtime", "0 min")` -- and wrote it straight onto two String
columns: `Machine.downtime` (`Column(String, default="0 min")`) and, on the
transition INTO Breakdown, `DowntimeLog.duration` (`Column(String,
nullable=False)`) plus the `DowntimeStarted` event's `duration`.

The HTTP path validates that field through `DowntimeCreate.duration` (a required
`str`). The MQTT path did not, and only defaulted a MISSING key. So a gateway
that sends `"downtime": null` -- the real wire form of an estimate it hasn't
computed, since `JSON.stringify` emits `null` for `undefined`/`NaN` -- put a
`None` onto a NOT NULL column. The `DowntimeLog` INSERT then fails its NOT NULL
constraint, so the breakdown's downtime log is SILENTLY DROPPED (verified below:
zero logs written), `Machine.downtime` is stored as NULL, and the
`DowntimeStarted` event fires carrying `duration=None`. Every downtime-derived
figure -- downtime minutes, the stoppage count, MTBF/MTTR, the predictive risk
score, the Pareto, the cost-of-losses -- then under-counts the one breakdown
that message was reporting, and nothing errors to say so (it is logged at INFO).
A numeric `downtime` is the same class of fault from the other side: an int
against a VARCHAR column is a type error on PostgreSQL.

`_downtime_text` coerces the field to the column's own default, exactly as the
HTTP schema does: null / blank / non-finite -> "0 min", a finite number -> its
plain string (read back as that many minutes), other text trimmed. The stoppage
is recorded, the event carries a usable duration, and a real "2 hrs 15 min" is
stored and parsed unchanged.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_downtime_text_safe.py
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
from duration import parse_duration_to_minutes
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
    # A production-bearing packet: total > 0 and good + rejected == total, so a
    # ProductionRecord is written alongside the status update. That record is the
    # collateral the rolled-back packet used to lose, so we assert it survives.
    payload = {"machine": "PRESS-01", "status": "Running", "utilization": 50,
               "downtime": "30 min", "total_count": 100, "good_count": 90,
               "rejected_count": 10}
    payload.update(fields)
    buf = io.StringIO()
    with redirect_stdout(buf):
        # json.dumps(float("inf")) -> "Infinity", which json.loads decodes back to
        # inf on the wire -- exactly how a non-finite reading reaches on_message.
        mqtt_service.on_message(None, None, _Msg(json.dumps(payload).encode()))


def _machine(Session):
    db = Session()
    try:
        return db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    finally:
        db.close()


def _counts(Session):
    db = Session()
    try:
        return (db.query(models.DowntimeLog).count(),
                db.query(models.ProductionRecord).count())
    finally:
        db.close()


def _latest_duration(Session):
    db = Session()
    try:
        row = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).first()
        return None if row is None else row.duration
    finally:
        db.close()


def _breakdown(seen, **fields):
    """Drive the machine Running -> Breakdown so a DowntimeLog is written, and
    return the number of DowntimeStarted events this transition produced."""
    before = len(seen)
    _send(status="Running", utilization=60)
    _send(status="Breakdown", utilization=0, **fields)
    return len(seen) - before


def main():
    print("=" * 74)
    print("1. A NULL `downtime` ON A BREAKDOWN STILL RECORDS THE STOPPAGE")
    print("=" * 74)
    Session = _setup()
    seen = []

    def handler(event, db=None):
        seen.append(event)
    event_bus.subscribe(DowntimeStarted, handler)

    # JSON null is the real wire form of a gateway that computed no estimate
    # (JSON.stringify emits null for undefined/NaN). Before the fix the breakdown
    # DowntimeLog's NOT NULL rejected it and the log was silently dropped (0 rows).
    # Measure the single Breakdown packet in isolation (deltas around it), so the
    # counts are THIS packet's own, not a sibling's.
    _send(status="Running", utilization=60)            # establish Running first
    logs0, records0 = _counts(Session)
    before = len(seen)
    _send(status="Breakdown", utilization=0, downtime=None)
    events = len(seen) - before
    logs, records = _counts(Session)
    m = _machine(Session)

    check("the machine reached Breakdown", m is not None and m.status == "Breakdown",
          str(getattr(m, "status", None)))
    check("the breakdown DowntimeLog was WRITTEN, not dropped by NOT NULL",
          logs - logs0 == 1, f"{logs - logs0} new logs")
    check("...with a non-null duration healed to the column default \"0 min\"",
          _latest_duration(Session) == "0 min", repr(_latest_duration(Session)))
    check("...and the machine's own downtime column is text, not NULL",
          m is not None and m.downtime == "0 min", repr(getattr(m, "downtime", None)))
    check("the production record in the SAME packet is untouched", records - records0 == 1,
          f"{records - records0} new production records")
    check("exactly one DowntimeStarted event was published", events == 1, f"{events} events")
    if seen:
        check("...carrying the safe \"0 min\" duration, not None",
              getattr(seen[-1], "duration", "x") == "0 min",
              repr(getattr(seen[-1], "duration", None)))

    print()
    print("=" * 74)
    print("2. A NUMERIC `downtime` IS STORED AS TEXT AND READS BACK AS MINUTES")
    print("=" * 74)
    # An int against a VARCHAR column aborts the packet on PostgreSQL. Coerced to
    # its plain string, it both stores and parses: the bare-number convention in
    # parse_duration_to_minutes reads "45" as 45 minutes.
    events = _breakdown(seen, downtime=45)
    check("a numeric downtime did not drop the breakdown", events == 1, f"{events} events")
    check("...stored as the string \"45\"", _latest_duration(Session) == "45",
          repr(_latest_duration(Session)))
    check("...which parse_duration_to_minutes reads as 45 minutes",
          parse_duration_to_minutes(_latest_duration(Session)) == 45,
          str(parse_duration_to_minutes(_latest_duration(Session))))

    # A float with a fractional part rounds to whole minutes, same as free text.
    _breakdown(seen, downtime=12.0)
    check("a float downtime 12.0 stores and reads as 12 minutes",
          parse_duration_to_minutes(_latest_duration(Session)) == 12,
          repr(_latest_duration(Session)))

    print()
    print("=" * 74)
    print("3. A REAL HOUR-FORMAT STRING IS STORED AND PARSED UNCHANGED")
    print("=" * 74)
    # The fix must not disturb a genuine duration: "2 hrs 15 min" is 135 minutes
    # (the shared parser's hour contract), NOT 2.
    _breakdown(seen, downtime="2 hrs 15 min")
    check("the string survives verbatim", _latest_duration(Session) == "2 hrs 15 min",
          repr(_latest_duration(Session)))
    check("...and parses to 135 minutes (hours honoured), not 2",
          parse_duration_to_minutes(_latest_duration(Session)) == 135,
          str(parse_duration_to_minutes(_latest_duration(Session))))

    print()
    print("=" * 74)
    print("4. BLANK AND NON-FINITE `downtime` FOLD TO THE DEFAULT")
    print("=" * 74)
    _breakdown(seen, downtime="   ")
    check("a whitespace-only downtime folds to \"0 min\"",
          _latest_duration(Session) == "0 min", repr(_latest_duration(Session)))

    # float("inf") reaches on_message as a real float (json decodes "Infinity");
    # it is not a duration, so it folds to the default rather than aborting.
    events = _breakdown(seen, downtime=float("inf"))
    check("an infinite downtime did not drop the breakdown", events == 1, f"{events} events")
    check("...and folded to \"0 min\"", _latest_duration(Session) == "0 min",
          repr(_latest_duration(Session)))

    # A missing key keeps the historical "0 min" default (unchanged behaviour).
    _send(status="Running", utilization=60)
    buf = io.StringIO()
    with redirect_stdout(buf):
        payload = {"machine": "PRESS-01", "status": "Breakdown", "utilization": 0}
        mqtt_service.on_message(None, None, _Msg(json.dumps(payload).encode()))
    check("a missing downtime key still defaults to \"0 min\"",
          _latest_duration(Session) == "0 min", repr(_latest_duration(Session)))

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


def test_mqtt_downtime_text_safe():
    """The pytest entry point.

    CI runs this file as `python test_mqtt_downtime_text_safe.py`; the coverage
    job runs pytest, which collects module-level ``test_*`` functions and nothing
    else. Without this the checks would be counted as untested and drag the
    coverage floor down (see the sibling MQTT suites).
    """
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
