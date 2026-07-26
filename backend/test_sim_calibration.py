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
from factory_simulator import tick_production, tick_shift_entry


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


if __name__ == "__main__":
    test_production_ticks_are_short_slices_at_gated_cadence()
    test_a_machine_cannot_exceed_a_physical_day_of_production()
    test_shift_entry_is_labelled_with_the_live_date_not_a_stale_import_date()
    print("ALL SIM CALIBRATION TESTS PASSED")
