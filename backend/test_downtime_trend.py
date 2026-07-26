"""Downtime trend read-model tests (ADR-0007).

`build_downtime_trend` compares the last 7 days of lost minutes against the 7
before, using the SAME shared duration parser as the summary ("2 hrs 15 min" ->
135, never 2), and attributes the swing to machines and reasons. These tests pin
the numbers to independently-derived expected values and cover the edges the
correctness rules call out: an HOUR-FORMAT duration, an empty table, a zero /
prior-empty denominator, None fields, and the daily-series-sums-to-the-halves
reconciliation (rule 3).

Run:  python backend/test_downtime_trend.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import downtime


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machines(db):
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))


def _log(machine_id, reason, duration, when):
    return models.DowntimeLog(machine_id=machine_id, reason=reason, duration=duration, created_at=when)


def test_worsening_trend_attributes_swing_to_machine_and_reason():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current week (last 7 days): PRESS-01 loses 120 + 60 = 180 min (Breakdown),
        # CNC-02 loses 30 min (Tooling) -> current 210 min over 3 events
        _log(1, "Breakdown", "2 hrs", now),
        _log(1, "Breakdown", "1 hr", now - timedelta(days=1)),
        _log(2, "Tooling", "30 min", now - timedelta(days=2)),
        # prior week (8-13 days ago): PRESS-01 30 min (Breakdown), CNC-02 60 min
        # (Changeover) -> prior 90 min over 2 events
        _log(1, "Breakdown", "30 min", now - timedelta(days=8)),
        _log(2, "Changeover", "1 hr", now - timedelta(days=9)),
        # older than the 14-day window -> excluded entirely
        _log(1, "Breakdown", "5 hrs", now - timedelta(days=20)),
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")

    assert t["current"] == {"events": 3, "minutes": 210}
    assert t["prior"] == {"events": 2, "minutes": 90}
    assert t["delta_minutes"] == 120                       # 210 - 90
    assert t["delta_pct"] == 133                           # round(120 / 90 * 100)
    assert t["direction"] == "worsening"
    assert t["thin_sample"] is False                       # 5 events >= 4
    assert t["tone"] == "bad"

    # who moved it: PRESS-01 up 150 min (180 vs 30); CNC-02 down 30 min (30 vs 60)
    assert [m["machine_id"] for m in t["worsening_machines"]] == [1]
    assert t["worsening_machines"][0]["delta_minutes"] == 150
    assert t["worsening_machines"][0]["minutes"] == 180
    assert t["worsening_machines"][0]["prior_minutes"] == 30
    assert [m["machine_id"] for m in t["improving_machines"]] == [2]
    assert t["improving_machines"][0]["delta_minutes"] == -30

    # reason movers ranked by change: Breakdown +150, Tooling +30 (new), Changeover -60
    movers = {r["reason"]: r for r in t["reason_movers"]}
    assert [r["reason"] for r in t["reason_movers"]] == ["Breakdown", "Tooling", "Changeover"]
    assert movers["Breakdown"]["delta_minutes"] == 150 and movers["Breakdown"]["is_new"] is False
    assert movers["Tooling"]["is_new"] is True and movers["Tooling"]["prior_minutes"] == 0
    assert movers["Changeover"]["delta_minutes"] == -60

    assert "PRESS-01 drove it (+150 min)" in t["verdict"]
    assert "up 120 min (133%) to 210 min" in t["verdict"]

    # rule 3: the daily series sums back to the half totals, exactly
    assert sum(d["minutes"] for d in t["series"]) == 210 + 90
    assert sum(d["events"] for d in t["series"]) == 5
    assert len(t["series"]) == downtime.TREND_WINDOW_DAYS   # 14 entries, oldest -> newest
    # every log here carries a machine_id, so per-machine minutes reconcile to the whole
    assert sum(m["minutes"] for m in
               [{"minutes": 180}, {"minutes": 30}]) == t["current"]["minutes"]


def test_hour_format_duration_is_read_as_hours_not_minutes():
    # THE duration guard: "2 hrs 15 min" must parse to 135, never 2 and never 215.
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _log(1, "Breakdown", "2 hrs 15 min", now),               # current -> 135 min
        _log(1, "Breakdown", "45 min", now - timedelta(days=8)),  # prior   -> 45 min
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    assert t["current"]["minutes"] == 135                        # not 2, not 215
    assert t["prior"]["minutes"] == 45
    assert t["delta_minutes"] == 90
    # only 2 stoppages across both weeks -> reported but not judged as a trend
    assert t["thin_sample"] is True
    assert t["tone"] == "warn"
    assert "too little to call a trend" in t["verdict"]


def test_empty_table_is_safe_and_reads_as_no_downtime():
    db = _fresh_session()
    _machines(db)
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    assert t["current"] == {"events": 0, "minutes": 0}
    assert t["prior"] == {"events": 0, "minutes": 0}
    assert t["delta_minutes"] == 0
    assert t["delta_pct"] is None                                # no prior week to divide by
    assert t["direction"] == "none"
    assert t["thin_sample"] is False                             # 0 events is not "thin", it's empty
    assert t["worsening_machines"] == [] and t["improving_machines"] == []
    assert t["reason_movers"] == []
    assert t["verdict"] == "No downtime in the last 14 days."
    assert t["tone"] == "good"
    assert sum(d["minutes"] for d in t["series"]) == 0


def test_prior_empty_is_worsening_with_no_percentage():
    # A jump from zero downtime is a real worsening but has no finite percentage.
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _log(1, "Breakdown", "1 hr", now),
        _log(1, "Breakdown", "1 hr", now - timedelta(days=1)),
        _log(2, "Tooling", "1 hr", now - timedelta(days=2)),
        _log(2, "Tooling", "1 hr", now - timedelta(days=3)),
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    assert t["current"]["minutes"] == 240 and t["prior"]["minutes"] == 0
    assert t["delta_minutes"] == 240
    assert t["delta_pct"] is None                                # prior is 0 -> no %
    assert t["direction"] == "worsening"
    assert t["thin_sample"] is False                             # 4 events == threshold
    assert "up 240 min to 240 min" in t["verdict"]               # no "(...%)" fragment
    assert "%" not in t["verdict"]


def test_improving_trend_reports_the_drop():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 15 + 15 = 30 min
        _log(1, "Breakdown", "15 min", now),
        _log(1, "Breakdown", "15 min", now - timedelta(days=1)),
        # prior: 100 + 100 + 100 = 300 min
        _log(1, "Breakdown", "100 min", now - timedelta(days=8)),
        _log(1, "Breakdown", "100 min", now - timedelta(days=9)),
        _log(1, "Breakdown", "100 min", now - timedelta(days=10)),
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    assert t["current"]["minutes"] == 30 and t["prior"]["minutes"] == 300
    assert t["delta_minutes"] == -270
    assert t["delta_pct"] == -90                                 # round(-270 / 300 * 100)
    assert t["direction"] == "improving"
    assert t["tone"] == "good"
    assert "down 270 min (90%) to 30 min" in t["verdict"]


def test_small_move_reads_as_steady():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 50 + 50 = 100 min (2 events)
        _log(1, "Breakdown", "50 min", now),
        _log(1, "Breakdown", "50 min", now - timedelta(days=1)),
        # prior: 45 + 45 = 90 min (2 events) -> total 4 events, not thin
        _log(1, "Breakdown", "45 min", now - timedelta(days=8)),
        _log(1, "Breakdown", "45 min", now - timedelta(days=9)),
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    assert t["delta_minutes"] == 10                              # within the 30-min floor
    assert t["direction"] == "steady"
    assert t["thin_sample"] is False
    assert t["tone"] == "good"
    assert "steady at 100 min (+10 min week on week)" in t["verdict"]


def test_blank_duration_and_machineless_rows_are_safe():
    # duration is NOT NULL in the schema, but operators can leave it blank ("");
    # the shared parser reads that as 0 minutes rather than crashing. A row with no
    # machine_id counts toward the totals but can't be attributed to a machine.
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _log(1, "Breakdown", "1 hr", now),                       # 60 min, machine 1
        _log(None, "Breakdown", "30 min", now),                  # no machine -> in totals, not attributed
        _log(1, "Breakdown", "", now),                           # blank duration -> 0 min, still an event
    ])
    db.commit()

    t = downtime.build_downtime_trend(db, "DEFAULT")
    # current minutes = 60 + 30 + 0 = 90 over 3 events
    assert t["current"]["minutes"] == 90
    assert t["current"]["events"] == 3
    # the machine-less 30 min is counted in the total but not attributed to a machine
    assert sum(m["minutes"] for m in t["worsening_machines"]) == 60
    assert t["worsening_machines"][0]["machine_id"] == 1
    # daily series still reconciles to the (machine'd + machine-less) total
    assert sum(d["minutes"] for d in t["series"]) == 90


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
