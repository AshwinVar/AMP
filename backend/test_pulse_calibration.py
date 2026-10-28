"""Pulse calibration divides an over-counting node's counts back to one per cycle.

THE DEFECT THIS PINS, MEASURED ON A REAL NODE
---------------------------------------------
Shrinidhi IMM-02's shot-counter node counted 1:1 against the HMI at 16:00 on
2026-10-28, then drifted and held steady at ~3x for the rest of the day — a noisy
direct-wired GPIO input after the opto-isolator was removed. Every window landed
at ~4.7s/shot against a measured 14.1s cycle, so AMP's output, OEE and plant
board ran triple. The node is publish-only; AMP cannot re-wire it from the cloud.

THE FIX. A per-machine divisor, applied at MQTT ingestion before the counts are
validated, stored, keyed for idempotency or broadcast, collapses the known
multiple back to one count per cycle. It is the identity for every machine at the
default of one edge per cycle, so no other install is touched.

WHAT MUST HOLD
  * the divisor reduces the STORED count, not merely the display;
  * good+rejected==total survives the division (the ingest path drops a record
    whose parts disagree, so a careless split would silently lose data);
  * a non-calibrated machine is byte-for-byte unchanged;
  * calibration never RESCUES a malformed payload into a valid one;
  * the env override extends the table, and a malformed override cannot raise.

Run:  python backend/test_pulse_calibration.py
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
import pulse_calibration
from database import Base

_REAL_SAFE_BROADCAST = mqtt_service.safe_broadcast
_REAL_SESSION_LOCAL = mqtt_service.SessionLocal

_TENANT = "CAL_TEST"
_TOPIC = f"flowmes/{_TENANT}/-/machines"

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def section(title):
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74)


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


def _send(record_id, total=10, good=10, rejected=0):
    body = {"machine": "PLC-01", "status": "Running", "utilization": 80,
            "downtime": "0 min", "total_count": total, "good_count": good,
            "rejected_count": rejected, "planned_minutes": 480,
            "runtime_minutes": 400, "ideal_cycle_time_seconds": 60,
            "record_id": record_id}
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


def _machine(session):
    db = session()
    try:
        return db.query(models.Machine).filter(models.Machine.name == "PLC-01").one_or_none()
    finally:
        db.close()


def run_collapse_math():
    section("1. collapse(): the division preserves good+rejected==total")
    C = pulse_calibration.collapse
    check("divisor 1 is the identity (a non-calibrated machine is untouched)",
          C(64, 60, 4, 1) == (64, 60, 4))
    check("64 good shots at 3x become 21 (matches the one-off history correction)",
          C(64, 64, 0, 3) == (21, 21, 0))
    t, g, r = C(12, 9, 3, 3)
    check("a window with rejects keeps good+rejected==total after /3",
          g + r == t and (t, g, r) == (4, 3, 1), f"{(t, g, r)}")
    # Mutation guard: good must be DERIVED from the corrected total. Rounding good
    # on its own gives (2,2,1) here -- 2+1 != 2 -- and the record would be dropped.
    check("good is derived from the corrected total, not rounded independently",
          C(5, 3, 2, 2) == (2, 1, 1), f"{C(5, 3, 2, 2)}")
    check("two raw edges at 3x round to one real cycle, not zero", C(2, 2, 0, 3) == (1, 1, 0))
    t, g, r = C(1, 1, 0, 3)
    check("a single stray edge rounds to zero (dropped upstream, not stored)",
          (t, g, r) == (0, 0, 0), f"{(t, g, r)}")
    t, g, r = C(10, 0, 10, 3)
    check("an all-rejects window stays all-rejects and consistent",
          g + r == t and r <= t and g >= 0, f"{(t, g, r)}")


def run_lookup_and_env():
    section("2. pulses_per_cycle(): default 1, built-in entry, env override")
    other = SimpleNamespace(tenant_code="ACME", site="", name="M1")
    imm02 = SimpleNamespace(tenant_code="SHRINIDHI", site="", name="IMM-02")
    check("an unlisted machine gets 1 (no correction)",
          pulse_calibration.pulses_per_cycle(other) == 1)
    check("the built-in IMM-02 entry resolves to 3",
          pulse_calibration.pulses_per_cycle(imm02) == 3)

    os.environ["PULSE_CALIBRATION"] = "ACME::M1=4"
    try:
        check("an env override calibrates a machine not in the built-in table",
              pulse_calibration.pulses_per_cycle(other) == 4)
    finally:
        os.environ.pop("PULSE_CALIBRATION", None)
    check("clearing the env restores the default", pulse_calibration.pulses_per_cycle(other) == 1)

    os.environ["PULSE_CALIBRATION"] = "garbage,ACME:M1=2,ACME::M1=notanint,ACME::M1=0"
    try:
        check("a malformed override is skipped, not raised (built-in table stands)",
              pulse_calibration.pulses_per_cycle(imm02) == 3
              and pulse_calibration.pulses_per_cycle(other) == 1)
    finally:
        os.environ.pop("PULSE_CALIBRATION", None)


def run_ingest_applies_the_divisor():
    section("3. the STORED window is divided, end to end through on_message")
    Session = _setup()
    try:
        # Baseline: tenant not calibrated -> stored as sent (the control).
        _send("SEED", total=60, good=60, rejected=0)
        seed = _record(Session, "SEED")
        check("an uncalibrated machine stores the raw count", seed is not None and seed.total_count == 60,
              str(seed.total_count if seed else None))

        m = _machine(Session)
        os.environ["PULSE_CALIBRATION"] = f"{m.tenant_code}:{m.site}:{m.name}=3"
        try:
            _send("CAL", total=63, good=63, rejected=0)
            cal = _record(Session, "CAL")
            check("a calibrated window is stored at a third of the raw count",
                  cal is not None and cal.total_count == 21 and cal.good_count == 21
                  and cal.rejected_count == 0, str(cal.total_count if cal else None))

            # A payload whose parts already disagree must still be dropped, not
            # "repaired" into a valid record by the divisor.
            _send("BAD", total=9, good=9, rejected=3)
            check("calibration does not rescue a malformed payload (good+rej != total)",
                  _record(Session, "BAD") is None)
        finally:
            os.environ.pop("PULSE_CALIBRATION", None)
    finally:
        mqtt_service.safe_broadcast = _REAL_SAFE_BROADCAST
        mqtt_service.SessionLocal = _REAL_SESSION_LOCAL


def main():
    failures.clear()
    run_collapse_math()
    run_lookup_and_env()
    run_ingest_applies_the_divisor()
    print("\n" + "=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PULSE CALIBRATION CHECKS PASSED")
    return 0


def test_pulse_calibration():
    """pytest entry: the coverage job collects module-level test_* functions, so
    the suite exposes exactly one, which runs every section via main()."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
