"""OEE trend read-model tests (ADR-0007).

`build_oee_trend` compares this week's pooled OEE against last week's on the same
output-weighted basis as build_oee_summary, using the shared oee_direction
dead-band (±2 pts), and attributes the swing to components and machines. These
tests pin the numbers and cover the edges: a worsening/improving/steady move, an
empty table, a prior-empty week (unknown, not a crash), a current-empty week
(reported, not shown as OEE 0), and the pooled current/prior halves.

The seeding trick: with planned=runtime=total=100 and ideal_cycle=60, a record's
availability and performance are both 100%, so its OEE equals its good_count, and
pooling two such records averages their good_counts — every OEE below is derived
by hand from that.

Run:  python backend/test_oee_trend.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import oee


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machines(db):
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))


def _mk(machine_id, good, when):
    # a = 100/100 = 1.0, p = 60*100/(100*60) = 1.0, q = good/100 -> OEE == good.
    r = models.ProductionRecord(
        machine_id=machine_id, planned_minutes=100, runtime_minutes=100,
        ideal_cycle_time_seconds=60, total_count=100, good_count=good,
        rejected_count=100 - good,
    )
    r.created_at = when
    return r


def test_worsening_attributes_swing_to_quality_and_machine():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current week: M1 OEE 60, M2 OEE 80 -> pooled 70
        _mk(1, 60, now),
        _mk(2, 80, now - timedelta(days=1)),
        # prior week: M1 OEE 90, M2 OEE 80 -> pooled 85
        _mk(1, 90, now - timedelta(days=8)),
        _mk(2, 80, now - timedelta(days=9)),
        # older than the 14-day window -> excluded
        _mk(1, 10, now - timedelta(days=20)),
    ])
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["current"]["oee"] == 70 and t["prior"]["oee"] == 85
    assert t["delta_pts"] == -15
    assert t["direction"] == "worsening"
    assert t["tone"] == "bad"

    # only quality moved (A and P stayed 100); it's the biggest drag too
    q = next(c for c in t["components"] if c["component"] == "quality")
    assert q["delta"] == -15
    assert t["biggest_drag"] == "quality"
    assert "Quality fell 15 pts" in t["verdict"]
    assert "OEE down 15 pts to 70%" in t["verdict"]

    # M1 fell 30 (60 vs 90); M2 flat -> M1 is the sole decliner
    assert [m["machine_id"] for m in t["declining_machines"]] == [1]
    assert t["declining_machines"][0]["delta"] == -30
    assert t["improving_machines"] == []


def test_improving_reads_as_up_and_good():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([_mk(1, 90, now), _mk(1, 60, now - timedelta(days=8))])
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["current"]["oee"] == 90 and t["prior"]["oee"] == 60
    assert t["delta_pts"] == 30
    assert t["direction"] == "improving"
    assert t["tone"] == "good"
    assert "OEE up 30 pts to 90% week on week" in t["verdict"]


def test_small_move_reads_as_steady():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([_mk(1, 80, now), _mk(1, 81, now - timedelta(days=8))])
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["delta_pts"] == -1                       # within the ±2pt dead-band
    assert t["direction"] == "steady"
    assert t["tone"] == "good"
    assert "OEE steady at 80% (-1 pts week on week)" in t["verdict"]


def test_empty_table_is_safe():
    db = _fresh_session()
    _machines(db)
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["current"]["has_data"] is False and t["prior"]["has_data"] is False
    assert t["delta_pts"] == 0
    assert t["direction"] == "none"
    assert t["tone"] == "warn"
    assert t["verdict"] == "No production recorded in the last 14 days."
    assert t["improving_machines"] == [] and t["declining_machines"] == []
    assert t["biggest_drag"] is None


def test_prior_empty_reads_as_unknown_not_a_crash():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([_mk(1, 75, now)])
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["current"]["oee"] == 75
    assert t["direction"] == "unknown"
    assert t["tone"] == "warn"
    assert "nothing to compare yet" in t["verdict"]


def test_current_empty_reports_it_not_zero_oee():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([_mk(1, 70, now - timedelta(days=8))])   # only prior week ran
    db.commit()

    t = oee.build_oee_trend(db, "DEFAULT")
    assert t["current"]["has_data"] is False
    assert t["direction"] == "unknown"
    assert t["tone"] == "warn"
    assert "No production recorded this week" in t["verdict"]
    assert "was 70% last week" in t["verdict"]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
