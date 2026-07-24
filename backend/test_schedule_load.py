"""Schedule load board read-model tests (ADR-0007).

Forward dispatch board over production_schedules: booked jobs/minutes for the
next 7 days, per-day / per-machine / per-shift breakdowns that reconcile to the
same headline, the busiest machine and peak day, and the slipped backlog
(open-and-past-due, or Delayed) to chase.

Run:  python backend/test_schedule_load.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import schedule_load


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, id_, name, line=""):
    db.add(models.Machine(id=id_, name=name, status="Running", line=line))


def _sched(no, machine_id, day_offset, minutes=480, shift="Shift A",
           status="Scheduled", planned=100, priority="Medium"):
    return models.ProductionSchedule(
        schedule_no=no, machine_id=machine_id,
        scheduled_date=(datetime.utcnow().date() + timedelta(days=day_offset)),
        shift_name=shift, status=status, planned_quantity=planned,
        estimated_minutes=minutes, priority=priority,
    )


def test_forward_load_rolls_up_and_reconciles():
    db = _fresh_session()
    _machine(db, 1, "SMT-01", line="SMT")
    _machine(db, 2, "IC-01", line="IC")
    db.add_all([
        # In-horizon, open -> counted as forward load
        _sched("S-1", 1, 0, minutes=480, shift="Shift A"),   # today, SMT-01
        _sched("S-2", 1, 2, minutes=360, shift="Shift B"),   # +2d, SMT-01
        _sched("S-3", 2, 2, minutes=240, shift="Shift A"),   # +2d, IC-01
        # In-horizon but DONE -> excluded from load and from open
        _sched("S-4", 2, 1, minutes=999, status="Completed"),
        # Past-due open -> slipped backlog, NOT forward load
        _sched("S-5", 1, -3, minutes=300, status="Scheduled"),
        # Outside horizon (beyond +7) -> excluded entirely by the SQL window
        _sched("S-6", 2, 20, minutes=480),
    ])
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")

    # Window is lookback(-30) .. horizon(+7): S-6 (+20) is excluded; the other 5 are in.
    assert r["total"] == 5
    # Open = everything not Completed: S-1,S-2,S-3,S-5 (S-4 is Completed) = 4
    assert r["open"] == 4

    # Forward load = open AND today..+7: S-1,S-2,S-3 (S-5 is past due, S-4 is done).
    # Independently: 3 jobs, minutes = 480 + 360 + 240 = 1080.
    assert r["scheduled_jobs"] == 3
    assert r["scheduled_minutes"] == 1080

    # Reconciliation (rule 3): every breakdown sums to the same headline.
    assert sum(m["minutes"] for m in r["by_machine"]) == r["scheduled_minutes"]
    assert sum(m["jobs"] for m in r["by_machine"]) == r["scheduled_jobs"]
    assert sum(d["minutes"] for d in r["by_day"]) == r["scheduled_minutes"]
    assert sum(d["jobs"] for d in r["by_day"]) == r["scheduled_jobs"]
    assert sum(s["minutes"] for s in r["by_shift"]) == r["scheduled_minutes"]
    assert sum(s["jobs"] for s in r["by_shift"]) == r["scheduled_jobs"]

    # Busiest machine is SMT-01: 480 + 360 = 840 min over 2 jobs.
    assert r["busiest_machine"]["name"] == "SMT-01"
    assert r["busiest_machine"]["minutes"] == 840 and r["busiest_machine"]["jobs"] == 2

    # Peak day is +2 (S-2 360 + S-3 240 = 600) vs today (480).
    assert r["peak_day"]["minutes"] == 600
    assert r["peak_day"]["date"] == (datetime.utcnow().date() + timedelta(days=2)).isoformat()

    # by_day spans today..+7 inclusive (8 entries), zero-filled.
    assert len(r["by_day"]) == 8

    # Slippage: S-5 is the only open past-due entry.
    assert r["overdue"] == 1 and r["needs_attention"] == 1
    assert r["chase"][0]["schedule_no"] == "S-5" and r["chase"][0]["days_overdue"] == 3
    assert r["tone"] == "bad"       # overdue drives the verdict


def test_delayed_status_is_flagged_even_when_not_past_due():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add_all([
        _sched("S-1", 1, 3, status="Delayed"),   # future-dated but explicitly Delayed
        _sched("S-2", 1, 1, status="Scheduled"),
    ])
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")
    # Not past due, so overdue is 0, but Delayed status is a slip signal.
    assert r["overdue"] == 0
    assert r["delayed"] == 1
    assert r["needs_attention"] == 1
    assert r["chase"][0]["schedule_no"] == "S-1" and r["chase"][0]["delayed"] is True
    # A future-dated Delayed job still contributes to forward load.
    assert r["scheduled_jobs"] == 2


def test_none_minutes_and_unassigned_machine_are_safe():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add_all([
        _sched("S-1", 1, 1, minutes=480),         # will be forced to a real NULL below
        _sched("S-2", None, 1, minutes=120),      # no machine -> Unassigned bucket
    ])
    db.commit()
    # The column carries a Python-side default of 480, so an explicit None on the
    # ORM object is filled at flush — force a genuine NULL to exercise the guard.
    db.execute(text("UPDATE production_schedules SET estimated_minutes = NULL "
                    "WHERE schedule_no = 'S-1'"))
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")
    assert r["scheduled_jobs"] == 2
    assert r["scheduled_minutes"] == 120          # None counted as 0, not a crash
    # Unassigned machine still reconciles into the per-machine breakdown.
    assert sum(m["minutes"] for m in r["by_machine"]) == 120
    assert sum(m["jobs"] for m in r["by_machine"]) == 2
    names = {m["name"] for m in r["by_machine"]}
    assert schedule_load.UNASSIGNED in names


def test_empty_board_is_zeroed_not_crashed():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")   # a machine but nothing scheduled
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")
    assert r["total"] == 0 and r["open"] == 0
    assert r["scheduled_jobs"] == 0 and r["scheduled_minutes"] == 0
    assert r["busiest_machine"] is None
    assert r["peak_day"] is None                  # no max() blow-up on an empty board
    assert r["overdue"] == 0 and r["needs_attention"] == 0
    assert r["chase"] == []
    assert r["tone"] == "warn"
    # by_day is still the full zero-filled horizon.
    assert len(r["by_day"]) == 8 and all(d["minutes"] == 0 for d in r["by_day"])


def test_all_healthy_forward_load_reads_good():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add_all([
        _sched("S-1", 1, 1, minutes=480),
        _sched("S-2", 1, 4, minutes=240),
    ])
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")
    assert r["overdue"] == 0 and r["needs_attention"] == 0
    assert r["scheduled_jobs"] == 2 and r["scheduled_minutes"] == 720
    assert r["tone"] == "good"


def test_window_bounds_exclude_old_and_far_future():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add_all([
        _sched("S-old", 1, -40, status="Scheduled"),   # older than the 30d lookback
        _sched("S-far", 1, 40, status="Scheduled"),     # beyond the 7d horizon
        _sched("S-now", 1, 0, minutes=480),              # in horizon
    ])
    db.commit()

    r = schedule_load.build_schedule_load(db, "DEFAULT")
    # Only S-now falls in the [lookback, horizon] SQL window.
    assert r["total"] == 1
    assert r["scheduled_jobs"] == 1 and r["scheduled_minutes"] == 480
    assert r["overdue"] == 0     # the 40-day-old slip is out of the bounded window


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
