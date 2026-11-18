"""A counts-only MQTT report has no OEE time window, and must not invent one.

THE DEFECT THIS PINS
--------------------
on_message writes a ProductionRecord from any packet whose counts balance
(total > 0 and good + rejected == total). A gateway that reports the COUNTS but
not the time window — no planned_minutes, no runtime_minutes — is a real shape:
the AMP edge gateway always sends the window alongside counts, but the MQTT
contract is open and a counts-only third-party publisher is accepted (see
test_mqtt_resilience's "redelivered packet" pin, which sends exactly that).

Those absent fields used to default to a full 480-minute shift, 0 runtime and a
60-second ideal cycle. None of the three was measured, and all three leaked into
displayed numbers (ADR-0010):

  * availability read 0% (runtime 0 / planned 480) for a machine that was
    demonstrably producing;
  * the record added 480 to the pooled planned-minutes denominator and 0 to the
    runtime numerator, dragging the WHOLE plant's pooled availability down, and
    added 480 phantom minutes to ai.cost.downtime_minutes (planned - runtime);
  * pooled performance sums ideal_cycle x total but divides by runtime, so a 60s
    cycle with 0 runtime inflated it — ideal time with no minutes behind it.

THE RULE
--------
A report with no planned_minutes AND no runtime_minutes records the window as
0/0/0: the record contributes to pooled QUALITY alone. oee_contract then renders
availability and performance as "not measurable" (planned 0 -> None, runtime 0
-> None), and the record adds nothing to any denominator. A report that DOES
carry a window is unchanged.

Run:  python backend/test_mqtt_production_window.py     (exit 0 = pass)
"""
import io
import json
import sys
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
import oee_contract
from ai.cost import downtime_minutes
from database import Base

# Captured and restored in main()'s finally — pytest imports every test module
# during collection, so a module that walks away with a shared global stubbed
# breaks every suite that runs after it (see test_mqtt_production_time).
_REAL_SAFE_BROADCAST = mqtt_service.safe_broadcast
_REAL_SESSION_LOCAL = mqtt_service.SessionLocal

_TENANT = "WINDOW_TEST"
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


def _send(payload):
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, _Msg(payload))


def _record(session, machine):
    db = session()
    try:
        return (db.query(models.ProductionRecord)
                  .join(models.Machine, models.ProductionRecord.machine_id == models.Machine.id)
                  .filter(models.Machine.name == machine)
                  .order_by(models.ProductionRecord.id.desc())
                  .first())
    finally:
        db.close()


def _all_records(session):
    db = session()
    try:
        return db.query(models.ProductionRecord).all()
    finally:
        db.close()


def _pooled(records):
    """OEE over these records, computed exactly as oee_contract._sums does in SQL
    (sum planned, runtime, total, good, and sum(ideal_cycle x total)) so the test
    reconciles against the live contract rather than a second formula."""
    planned = sum(r.planned_minutes for r in records)
    runtime = sum(r.runtime_minutes for r in records)
    total = sum(r.total_count for r in records)
    good = sum(r.good_count for r in records)
    ideal_seconds = sum(r.ideal_cycle_time_seconds * r.total_count for r in records)
    return oee_contract.as_percentages(
        oee_contract.oee_from_sums(planned, runtime, total, good, ideal_seconds))


def main():
    failures.clear()
    try:
        # ── 1. a counts-only report writes a quality-only record ────────────────
        section("1. COUNTS WITHOUT A TIME WINDOW RECORD 0/0/0, NOT A FABRICATED SHIFT")

        Session = _setup()
        # No planned_minutes, no runtime_minutes, no ideal_cycle_time_seconds.
        _send({"machine": "PRESS-01", "status": "Running",
               "total_count": 100, "good_count": 90, "rejected_count": 10})
        row = _record(Session, "PRESS-01")
        check("the counts-only record is still written", row is not None)
        if row is not None:
            # Independently derived: the absent window is recorded as absent (0),
            # never the old 480 / 0 / 60 defaults.
            check("planned_minutes is 0, not the fabricated 480 shift",
                  row.planned_minutes == 0, f"planned_minutes={row.planned_minutes}")
            check("runtime_minutes is 0", row.runtime_minutes == 0,
                  f"runtime_minutes={row.runtime_minutes}")
            check("ideal_cycle_time_seconds is 0, not the fabricated 60s",
                  row.ideal_cycle_time_seconds == 0,
                  f"ideal_cycle_time_seconds={row.ideal_cycle_time_seconds}")
            # The counts themselves are recorded exactly — quality IS supported.
            check("the counts are recorded exactly (quality is real)",
                  (row.total_count, row.good_count, row.rejected_count) == (100, 90, 10),
                  f"{(row.total_count, row.good_count, row.rejected_count)}")

        # ── 2. the honest OEE: availability/performance not measurable, quality real
        section("2. AVAILABILITY & PERFORMANCE ARE 'NOT MEASURABLE', QUALITY IS 90%")

        oee = _pooled([row])
        # planned 0 -> availability None; runtime 0 -> performance None; quality
        # 90/100 = 90%. oee is a product of all three, so None. has_data is True
        # because something was made (is_measurable reads total > 0).
        check("availability is not measurable (None), not a fabricated 0%",
              oee["availability"] is None, f"availability={oee['availability']}")
        check("performance is not measurable (None)",
              oee["performance"] is None, f"performance={oee['performance']}")
        check("quality is the real 90% (90 good / 100 total)",
              oee["quality"] == 90, f"quality={oee['quality']}")
        check("oee is not measurable (None) — a product needs all three",
              oee["oee"] is None, f"oee={oee['oee']}")
        check("has_data is True — something was made",
              oee["has_data"] is True, f"has_data={oee['has_data']}")

        # ── 3. phantom downtime is gone ─────────────────────────────────────────
        section("3. A COUNTS-ONLY RECORD ADDS ZERO PHANTOM DOWNTIME")

        # ai.cost.downtime_minutes is max(0, planned - runtime) per record. The old
        # 480/0 defaults made this 480 minutes of downtime the plant never had;
        # 0/0 makes it 0.
        dm = downtime_minutes([row])
        check("downtime_minutes is 0, not the phantom 480", dm == 0, f"downtime_minutes={dm}")

        # ── 4. a windowed report is unchanged ───────────────────────────────────
        section("4. A REPORT THAT CARRIES ITS WINDOW IS UNCHANGED")

        Session = _setup()
        _send({"machine": "CELL-02", "status": "Running",
               "planned_minutes": 120, "runtime_minutes": 100,
               "ideal_cycle_time_seconds": 50,
               "total_count": 200, "good_count": 190, "rejected_count": 10})
        row = _record(Session, "CELL-02")
        check("the windowed record is written", row is not None)
        if row is not None:
            check("planned/runtime/ideal are stored exactly as sent",
                  (row.planned_minutes, row.runtime_minutes, row.ideal_cycle_time_seconds)
                  == (120, 100, 50),
                  f"{(row.planned_minutes, row.runtime_minutes, row.ideal_cycle_time_seconds)}")
            # Independently derived from the contract: availability 100/120 = 83%,
            # performance (50*200)/(100*60) = 10000/6000 = 1.667 -> clamped 100%,
            # quality 190/200 = 95%.
            oee = _pooled([row])
            check("availability is the real 83% (100/120)",
                  oee["availability"] == 83, f"availability={oee['availability']}")
            check("performance clamps to 100% ((50x200)/(100x60) = 1.667)",
                  oee["performance"] == 100, f"performance={oee['performance']}")
            check("quality is 95% (190/200)", oee["quality"] == 95, f"quality={oee['quality']}")
            dm = downtime_minutes([row])
            check("downtime_minutes is 20 (120 planned - 100 runtime)", dm == 20,
                  f"downtime_minutes={dm}")

        # ── 5. reconciliation: a counts-only record cannot move the pooled window
        section("5. A COUNTS-ONLY RECORD DOES NOT MOVE THE POOLED DENOMINATORS")

        Session = _setup()
        # One windowed record and one counts-only record in the same window.
        _send({"machine": "LINE-03", "status": "Running",
               "planned_minutes": 120, "runtime_minutes": 100,
               "ideal_cycle_time_seconds": 50,
               "total_count": 200, "good_count": 190, "rejected_count": 10})
        _send({"machine": "LINE-03", "status": "Running",
               "total_count": 100, "good_count": 100, "rejected_count": 0})
        records = _all_records(Session)
        check("both records were written", len(records) == 2, f"n={len(records)}")

        windowed = [r for r in records if r.planned_minutes > 0]
        pooled_all = _pooled(records)
        pooled_windowed = _pooled(windowed)
        # The counts-only record adds 0 to planned and 0 to runtime, so pooled
        # availability is identical with and without it: 100 / 120 = 83%.
        check("pooled availability is unchanged by the counts-only record (83%)",
              pooled_all["availability"] == pooled_windowed["availability"] == 83,
              f"all={pooled_all['availability']} windowed={pooled_windowed['availability']}")
        # Pooled performance is likewise unmoved: the counts-only record adds
        # 0 x 100 = 0 ideal-seconds and 0 runtime. Both clamp from 1.667 -> 100%.
        check("pooled performance is unchanged by the counts-only record",
              pooled_all["performance"] == pooled_windowed["performance"],
              f"all={pooled_all['performance']} windowed={pooled_windowed['performance']}")
        # Pooled quality DOES include the counts-only record's units, correctly:
        # (190 + 100) good / (200 + 100) total = 290/300 = 97% (rounded).
        check("pooled quality includes the counts-only units (290/300 -> 97%)",
              pooled_all["quality"] == round(290 / 300 * 100),
              f"quality={pooled_all['quality']}")
        # And phantom downtime stays out of the pooled figure: 20 from the
        # windowed record, 0 from the counts-only one — not 20 + 480.
        dm = downtime_minutes(records)
        check("pooled downtime_minutes is 20, not 500 (no 480 phantom)", dm == 20,
              f"downtime_minutes={dm}")

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


def test_mqtt_production_window():
    """The pytest entry point, per CONVENTIONS: the coverage job runs pytest,
    which collects module-level ``test_*`` functions and nothing else."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
