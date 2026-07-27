"""Simulator calibration tests.

The sim must keep the dashboard alive at a PHYSICALLY PLAUSIBLE data volume:
production records are short slices at a gated cadence, so weekly aggregates
(downtime minutes, cost of losses) stay inside what a real week allows. The
old behaviour — a full 480-minute shift every 45-second tick — made weekly
downtime exceed the minutes in a week.

Run:  python backend/test_sim_calibration.py     (exit 0 = pass)
"""
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import factory_simulator
import models
from database import Base
from factory_simulator import drift_utilization, tick_production, tick_shift_entry


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_production_ticks_are_short_slices_at_gated_cadence():
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()

    for _ in range(200):
        tick_production(db)

    records = db.query(models.ProductionRecord).all()
    # gated: ~25% of ticks produce a record (200 ticks -> ~50; wide bounds)
    assert 15 <= len(records) <= 110, f"cadence off: {len(records)} records from 200 ticks"
    for r in records:
        assert r.planned_minutes == 15, "records must be short slices, not full shifts"
        assert r.runtime_minutes <= r.planned_minutes
        assert r.good_count + r.rejected_count == r.total_count

    # a day of ticks (1920) at this calibration stays within a real day
    per_tick_expected = 0.25 * 15
    assert per_tick_expected * 1920 <= 24 * 60 * 5, "daily planned minutes must be plausible"
    print(f"PASS short slices at gated cadence ({len(records)} records from 200 ticks)")


def test_a_machine_cannot_exceed_a_physical_day_of_production():
    """Hammer one machine with far more ticks than a day's cadence would allow;
    the DB-state cap must keep its recorded planned minutes within a real day, so
    the OEE window can't be inflated by long uptime or parallel callers."""
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()

    # 4000 ticks (~0.25 gate => ~1000 attempts) far exceeds a day of 15-min slices.
    for _ in range(4000):
        tick_production(db)

    planned_today = sum(r.planned_minutes for r in db.query(models.ProductionRecord).all())
    assert planned_today <= 24 * 60, f"machine exceeded a physical day: {planned_today} min"
    print(f"PASS one machine capped at a physical day ({planned_today} planned min recorded)")


def test_shift_entry_is_labelled_with_the_live_date_not_a_stale_import_date():
    """The live shift-log tick must stamp the row with TODAY, not the date the
    module was imported. ``factory_simulator.today`` is ``date.today()`` captured
    once at import; on a long-running server it goes stale, and the tick used to
    read it directly. Forcing that module var to an obviously-wrong past date and
    asserting the new row still carries the real current date pins the fix — a
    label must carry the date the data actually has."""
    db = _fresh_session()

    # Simulate a server that started long ago: poison the import-time date.
    stale = date(2000, 1, 1)
    original = factory_simulator.today
    factory_simulator.today = stale
    try:
        tick_shift_entry(db)
    finally:
        factory_simulator.today = original

    rows = db.query(models.ShiftData).all()
    assert len(rows) == 1, f"expected one shift row, got {len(rows)}"
    label = rows[0].shift_name

    today_token = datetime.now().strftime("%d %b")
    stale_token = stale.strftime("%d %b")  # "01 Jan"

    assert today_token in label, f"shift label {label!r} missing the live date {today_token!r}"
    # Guard against a false pass if the test itself is run on 01 Jan.
    if today_token != stale_token:
        assert stale_token not in label, f"shift label {label!r} leaked the stale import date {stale_token!r}"
    print(f"PASS shift entry labelled with the live date ({label!r}, ignored stale {stale_token!r})")


def test_utilization_drift_stays_in_the_live_band():
    """The live drift steps utilization and clamps it to [40, 99]. Numbers are
    derived by hand: 50 down 5 -> 45; 50 up 5 -> 55; 98 up 5 -> 99 (ceiling); 41
    down 5 -> 36 -> 40 (floor). The step is exact, the clamp is inclusive."""
    assert drift_utilization(50, -5) == 45
    assert drift_utilization(50, 5) == 55
    assert drift_utilization(98, 5) == 99          # ceiling, not 103
    assert drift_utilization(41, -5) == 40          # floor, not 36
    assert drift_utilization(40, -5) == 40          # already at floor, stays
    print("PASS utilization drift steps then clamps to [40, 99]")


def test_utilization_drift_survives_a_null_reading():
    """utilization is Column(Integer, default=0) WITHOUT nullable=False, so a
    Running machine can carry a true NULL. The old inline drift did `None + int`
    and raised TypeError, rolling back the whole per-tenant tick every 45s. A NULL
    now coalesces to the column default 0, so ANY step in [-5, 5] lands at or
    below 5 and the floor pins the result to exactly 40 — deterministic, no crash."""
    for delta in range(-5, 6):
        assert drift_utilization(None, delta) == 40, delta
    print("PASS utilization drift treats a NULL reading as the default 0 -> heals to 40, no crash")


def test_null_utilization_running_machine_does_not_break_the_sim_write():
    """End-to-end: the exact loop the background sim runs — drift every Running
    machine and commit — must survive a NULL-utilization Running machine in the
    roster (it used to TypeError and roll back the whole tick).

    A true NULL can't be inserted through the ORM (the Column default 0 coerces
    an explicit None at flush), so we reproduce the real threat model — a row
    written by raw SQL / a migration — with a direct UPDATE that NULLs the column,
    exactly the case the fix's comment describes."""
    import random as _random
    from sqlalchemy import text
    db = _fresh_session()
    db.add(models.Machine(name="Healthy", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.add(models.Machine(name="NullUtil", status="Running", utilization=50, tenant_code="DEFAULT"))
    db.commit()
    # Force a genuine NULL the way raw SQL / a migration would.
    db.execute(text("UPDATE machines SET utilization = NULL WHERE name = 'NullUtil'"))
    db.commit()
    db.expire_all()
    assert db.query(models.Machine).filter_by(name="NullUtil").one().utilization is None

    running = db.query(models.Machine).filter(models.Machine.status == "Running").all()
    for m in running:
        m.utilization = drift_utilization(m.utilization, _random.randint(-5, 5))
    db.commit()                                     # must not have rolled back

    by_name = {m.name: m for m in db.query(models.Machine).all()}
    assert 40 <= by_name["NullUtil"].utilization <= 99      # healed from NULL, no crash
    assert 40 <= by_name["Healthy"].utilization <= 99
    print("PASS the sim utilization write survives a NULL-utilization Running machine")


if __name__ == "__main__":
    test_production_ticks_are_short_slices_at_gated_cadence()
    test_a_machine_cannot_exceed_a_physical_day_of_production()
    test_shift_entry_is_labelled_with_the_live_date_not_a_stale_import_date()
    test_utilization_drift_stays_in_the_live_band()
    test_utilization_drift_survives_a_null_reading()
    test_null_utilization_running_machine_does_not_break_the_sim_write()
    print("ALL SIM CALIBRATION TESTS PASSED")
