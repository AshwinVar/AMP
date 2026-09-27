"""A record that waited in a queue is still a record of when it HAPPENED.

THE DEFECT THIS PINS, FOUND ON A REAL BROKER AND NOT BY READING
---------------------------------------------------------------
During the AMP-PILOT commissioning gate the subscriber was taken offline, three
records were published, and AMP was restored. All three arrived, none was
double-counted, and every `source_record_id` survived. Their times did not:

    PILOT-C  published 23:52:05  ->  created_at 23:54:32.086
    PILOT-D  published 23:52:09  ->  created_at 23:54:32.120
    PILOT-E  published 23:52:12  ->  created_at 23:54:32.149

Seven seconds of real production collapsed into sixty-three milliseconds at the
moment of recovery. `ProductionRecord` was constructed without `created_at`, so
SQLAlchemy stamped the WRITE time, and the payload's own `ts` -- which
edge/ampedge/payload.py has always sent, and which the HMAC covers -- was never
read.

WHY IT MATTERS MORE THAN IT LOOKS. Nothing is lost and nothing is duplicated, so
every count is right. Only the TIME is wrong, and time is the denominator of
every number AMP exists to produce: a three-hour outage puts three hours of
production into the minute AMP came back, inventing a spike that never happened
and leaving a hole where the work actually was. OEE, the shift scorecard and
every trend read the hole and the spike as real.

WHAT IS AND IS NOT TRUSTED. `ts` is inside the signed envelope, so for a
registered gateway it cannot be edited in flight without breaking the
signature. It is still bounded here: a clock that is wrong, or a publisher on a
workspace that has not yet registered a gateway, must not be able to write
production into last year or into next week. Out-of-range means fall back to
now, which is the old behaviour -- degraded, never wrong in a new way.

Run:  python backend/test_mqtt_production_time.py
"""
import io
import json
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
from database import Base

# CAPTURED AT IMPORT, RESTORED IN main()'s finally. pytest IMPORTS every
# test module during collection, before it runs anything, so a module that
# stubs a shared global and walks away has already broken every suite that
# runs afterwards -- whatever the alphabetical order suggests. Leaving
# safe_broadcast stubbed here took out test_live_broadcast_bridge and two
# cases in test_mqtt_resilience, which assert the broadcast is NOT a no-op.
_REAL_SAFE_BROADCAST = mqtt_service.safe_broadcast
_REAL_SESSION_LOCAL = mqtt_service.SessionLocal

_TENANT = "TIME_TEST"
_TOPIC = f"flowmes/{_TENANT}/-/machines"

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class _Msg:
    topic = _TOPIC

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    db = mqtt_service.SessionLocal()
    db.add(models.TenantConfig(tenant_code=_TENANT))
    db.commit()
    db.close()
    mqtt_service.safe_broadcast = lambda event: None
    return mqtt_service.SessionLocal


def _send(session, record_id, ts=None, total=10, good=10, rejected=0):
    body = {"machine": "PLC-01", "status": "Running", "utilization": 80,
            "downtime": "0 min", "total_count": total, "good_count": good,
            "rejected_count": rejected, "planned_minutes": 480,
            "runtime_minutes": 400, "ideal_cycle_time_seconds": 60,
            "record_id": record_id}
    if ts is not None:
        body["ts"] = ts
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _Msg(body))


def _record(session, record_id):
    db = session()
    try:
        return (db.query(models.ProductionRecord)
                  .filter(models.ProductionRecord.source_record_id == record_id)
                  .one_or_none())
    finally:
        db.close()


def main():
    failures.clear()
    try:
        # ── 1. a queued record keeps the time it happened ──────────────────────
        section("1. A RECORD DELIVERED LATE IS FILED WHEN IT HAPPENED")

        Session = _setup()
        three_hours_ago = time.time() - 3 * 3600
        _send(Session, "LATE-1", ts=three_hours_ago)
        row = _record(Session, "LATE-1")
        check("the late record was written at all", row is not None)
        if row is not None:
            drift = abs((row.created_at - datetime.utcfromtimestamp(three_hours_ago)).total_seconds())
            check("...stamped when it HAPPENED, not when it was replayed",
                  drift < 5,
                  f"created_at={row.created_at} is {drift:.0f}s from the reading time "
                  f"-- a three-hour outage would land in the recovery minute")

        # The spread between two real events must survive the replay. This is the part
        # the production evidence showed collapsing: 7 seconds became 63 milliseconds.
        _send(Session, "LATE-2", ts=three_hours_ago + 60)
        a, b = _record(Session, "LATE-1"), _record(Session, "LATE-2")
        if a is not None and b is not None:
            gap = abs((b.created_at - a.created_at).total_seconds())
            check("two readings a minute apart are still a minute apart after replay",
                  55 <= gap <= 65, f"gap={gap:.1f}s")


        # ── 2. absent or unusable ts falls back to now ─────────────────────────
        section("2. NO USABLE ts IS THE OLD BEHAVIOUR, NOT A NEW FAILURE")

        now = datetime.utcnow()
        _send(Session, "NOTS-1", ts=None)
        row = _record(Session, "NOTS-1")
        check("a payload with no ts is still written",
              row is not None and abs((row.created_at - now).total_seconds()) < 60,
              str(row.created_at if row else None))

        for label, bad in (("a string", "yesterday"), ("null", None), ("a list", [1]),
                           ("not a number", float("nan"))):
            rid = f"BAD-{label.replace(' ', '-')}"
            body_ts = bad if label != "null" else "null-marker"
            _send(Session, rid, ts=(bad if label != "null" else None))
            row = _record(Session, rid)
            check(f"ts that is {label} falls back to now rather than refusing the record",
                  row is not None and abs((row.created_at - datetime.utcnow()).total_seconds()) < 60,
                  str(row.created_at if row else None))


        # ── 3. the clock cannot be used to rewrite history ─────────────────────
        section("3. A BAD OR HOSTILE CLOCK CANNOT BACKDATE PRODUCTION")

        future = time.time() + 3 * 3600
        _send(Session, "FUTURE-1", ts=future)
        row = _record(Session, "FUTURE-1")
        check("a ts in the FUTURE is refused and falls back to now",
              row is not None and row.created_at <= datetime.utcnow() + timedelta(seconds=60),
              f"created_at={row.created_at if row else None} -- production must not appear "
              f"in a window that has not happened")

        ancient = time.time() - 400 * 24 * 3600
        _send(Session, "ANCIENT-1", ts=ancient)
        row = _record(Session, "ANCIENT-1")
        check("a ts a year old is refused and falls back to now",
              row is not None and (datetime.utcnow() - row.created_at).total_seconds() < 3600,
              f"created_at={row.created_at if row else None} -- an unbounded backdate "
              f"rewrites historical OEE")


        # ── 4. replay still does not double count ──────────────────────────────
        section("4. THE IDEMPOTENCY THE FIX MUST NOT BREAK")

        before = _record(Session, "LATE-1")
        _send(Session, "LATE-1", ts=three_hours_ago)
        _send(Session, "LATE-1", ts=time.time())      # re-sent later, same record
        after = _record(Session, "LATE-1")
        db = Session()
        count = (db.query(models.ProductionRecord)
                   .filter(models.ProductionRecord.source_record_id == "LATE-1").count())
        db.close()
        check("a re-sent record is still written exactly once", count == 1, f"count={count}")
        check("...and its original timestamp is not rewritten by the re-send",
              before is not None and after is not None and before.created_at == after.created_at,
              f"{before.created_at if before else None} -> {after.created_at if after else None}")



    finally:
        mqtt_service.safe_broadcast = _REAL_SAFE_BROADCAST
        mqtt_service.SessionLocal = _REAL_SESSION_LOCAL

    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_mqtt_production_time():
    """The pytest entry point, per CONVENTIONS: the coverage job runs pytest,
    which collects module-level ``test_*`` functions and nothing else."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
