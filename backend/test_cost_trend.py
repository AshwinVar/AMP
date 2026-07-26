"""Cost-of-losses trend read-model tests (ADR-0007).

`build_cost_trend` compares this week's loss cost (unplanned downtime + scrap,
priced at the standard rates) against last week's, on the SAME per-record basis
as build_cost_summary — downtime floored per record, never on the net — and
attributes the swing to machines and to the two drivers (downtime vs scrap).
These tests pin the numbers to independently-derived expected values and cover
the edges the correctness rules call out: an empty table, a zero / prior-empty
denominator, a thin sample, a zero-cost record excluded from the loss count, a
machineless row, and the daily-series-sums-to-the-halves reconciliation (rule 3).

Run:  python backend/test_cost_trend.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import cost


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machines(db):
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))


def _rec(machine_id, planned, runtime, rejected, when, total=100):
    # Only planned/runtime/rejected drive loss cost; the other NOT-NULL columns are
    # filled with self-consistent values so the row is valid.
    good = max(0, total - rejected)
    return models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=60, total_count=total, good_count=good,
        rejected_count=rejected, created_at=when,
    )


def test_worsening_trend_attributes_swing_to_machine_and_driver():
    # Rates: $12/downtime-min, $25/scrapped-unit.
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current week: M1 100 downtime-min ($1200) + 20 scrap ($500) = $1700;
        # M2 10 downtime-min ($120) + 4 scrap ($100) = $220 -> current $1920
        _rec(1, 200, 100, 0, now),                       # dt 100min -> $1200
        _rec(1, 100, 100, 20, now - timedelta(days=1)),  # scrap 20 -> $500
        _rec(2, 60, 50, 4, now - timedelta(days=2)),     # dt 10min $120 + scrap 4 $100
        # prior week: M1 4 scrap ($100); M2 30 downtime-min ($360) + 8 scrap ($200)
        # = $560 -> prior $660
        _rec(1, 100, 100, 4, now - timedelta(days=8)),   # scrap 4 -> $100
        _rec(2, 80, 50, 8, now - timedelta(days=9)),     # dt 30min $360 + scrap 8 $200
        # older than the 14-day window -> excluded entirely
        _rec(1, 500, 100, 40, now - timedelta(days=20)),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")

    assert t["current"] == {"cost": 1920, "downtime_cost": 1320, "scrap_cost": 600,
                            "records": 3, "loss_records": 3}
    assert t["prior"] == {"cost": 660, "downtime_cost": 360, "scrap_cost": 300,
                          "records": 2, "loss_records": 2}
    assert t["delta_cost"] == 1260                         # 1920 - 660
    assert t["delta_pct"] == 191                           # round(1260 / 660 * 100)
    assert t["direction"] == "worsening"
    assert t["thin_sample"] is False                       # 5 loss records >= 4
    assert t["tone"] == "bad"

    # who moved it: M1 up $1600 (1700 vs 100); M2 down $340 (220 vs 560)
    assert [m["machine_id"] for m in t["worsening_machines"]] == [1]
    assert t["worsening_machines"][0]["delta_cost"] == 1600
    assert t["worsening_machines"][0]["cost"] == 1700
    assert t["worsening_machines"][0]["prior_cost"] == 100
    assert [m["machine_id"] for m in t["improving_machines"]] == [2]
    assert t["improving_machines"][0]["delta_cost"] == -340

    # drivers ranked by change: downtime +$960 (1320 vs 360), scrap +$300 (600 vs 300)
    drivers = {d["key"]: d for d in t["drivers"]}
    assert [d["key"] for d in t["drivers"]] == ["downtime", "scrap"]
    assert drivers["downtime"]["delta_cost"] == 960 and drivers["downtime"]["cost"] == 1320
    assert drivers["scrap"]["delta_cost"] == 300 and drivers["scrap"]["prior_cost"] == 300

    assert "PRESS-01 drove it (+$1,600)" in t["verdict"]
    assert "up $1,260 (191%) to $1,920" in t["verdict"]

    # rule 3: the daily series sums back to the half totals, exactly
    assert sum(d["cost"] for d in t["series"]) == 1920 + 660
    assert sum(d["downtime_cost"] for d in t["series"]) == 1320 + 360
    assert sum(d["scrap_cost"] for d in t["series"]) == 600 + 300
    assert len(t["series"]) == cost.TREND_WINDOW_DAYS      # 14 entries, oldest -> newest


def test_thin_sample_reports_but_does_not_judge():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 300, 100, 0, now),                        # current: dt 200min -> $2400
        _rec(2, 100, 100, 0, now - timedelta(days=3)),    # current: zero-cost record
        _rec(1, 100, 100, 20, now - timedelta(days=8)),   # prior: scrap 20 -> $500
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 2400
    assert t["current"]["records"] == 2 and t["current"]["loss_records"] == 1  # zero-cost excluded
    assert t["prior"]["cost"] == 500
    assert t["delta_cost"] == 1900
    # only 2 loss-making records across both weeks -> reported but not a trend
    assert t["thin_sample"] is True
    assert t["tone"] == "warn"
    assert "2 loss-making records" in t["verdict"]
    assert "too little to call a trend" in t["verdict"]


def test_empty_table_is_safe_and_reads_as_no_cost():
    db = _fresh_session()
    _machines(db)
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"] == {"cost": 0, "downtime_cost": 0, "scrap_cost": 0,
                            "records": 0, "loss_records": 0}
    assert t["prior"]["cost"] == 0
    assert t["delta_cost"] == 0
    assert t["delta_pct"] is None                          # no prior week to divide by
    assert t["direction"] == "none"
    assert t["thin_sample"] is False                       # 0 is empty, not thin
    assert t["worsening_machines"] == [] and t["improving_machines"] == []
    assert t["drivers"] == []
    assert t["verdict"] == "No cost of losses in the last 14 days."
    assert t["tone"] == "good"
    assert sum(d["cost"] for d in t["series"]) == 0


def test_prior_empty_is_worsening_with_no_percentage():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 100, 100, 10, now),                       # $250
        _rec(1, 100, 100, 10, now - timedelta(days=1)),   # $250
        _rec(1, 100, 100, 10, now - timedelta(days=2)),   # $250
        _rec(1, 100, 100, 10, now - timedelta(days=3)),   # $250 -> current $1000, 4 loss recs
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 1000 and t["prior"]["cost"] == 0
    assert t["delta_cost"] == 1000
    assert t["delta_pct"] is None                          # prior is 0 -> no %
    assert t["direction"] == "worsening"
    assert t["thin_sample"] is False                       # 4 loss records == threshold
    assert "up $1,000 to $1,000 week on week" in t["verdict"]
    assert "%" not in t["verdict"]


def test_improving_trend_reports_the_drop():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 4 + 4 scrap = $100 + $100 = $200
        _rec(1, 100, 100, 4, now),
        _rec(1, 100, 100, 4, now - timedelta(days=1)),
        # prior: 400 downtime-min ($4800) + 40 scrap ($1000) = $5800
        _rec(1, 500, 100, 0, now - timedelta(days=8)),
        _rec(1, 100, 100, 40, now - timedelta(days=9)),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 200 and t["prior"]["cost"] == 5800
    assert t["delta_cost"] == -5600
    assert t["delta_pct"] == -97                           # round(-5600 / 5800 * 100)
    assert t["direction"] == "improving"
    assert t["tone"] == "good"
    assert "down $5,600 (97%) to $200 week on week" in t["verdict"]


def test_small_move_reads_as_steady():
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 20 + 20 scrap = $500 + $500 = $1000 (2 loss recs)
        _rec(1, 100, 100, 20, now),
        _rec(1, 100, 100, 20, now - timedelta(days=1)),
        # prior: 20 + 18 scrap = $500 + $450 = $950 (2 loss recs) -> 4 total, not thin
        _rec(1, 100, 100, 20, now - timedelta(days=8)),
        _rec(1, 100, 100, 18, now - timedelta(days=9)),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["delta_cost"] == 50                           # within the $300 floor
    assert t["direction"] == "steady"
    assert t["thin_sample"] is False
    assert t["tone"] == "good"
    assert "steady at $1,000 ($+50 week on week)" in t["verdict"]


def test_zero_cost_and_machineless_rows_are_safe():
    # A record with no loss (planned == runtime, 0 rejected) is counted as a record
    # but not a loss record. A machineless row counts toward the totals but can't be
    # attributed to a machine.
    db = _fresh_session()
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 100, 100, 20, now),                        # $500, machine 1
        _rec(None, 100, 100, 10, now),                     # $250, no machine -> total only
        _rec(2, 100, 100, 0, now),                         # $0 -> record, not a loss record
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 750                     # 500 + 250 + 0
    assert t["current"]["records"] == 3
    assert t["current"]["loss_records"] == 2               # the $0 row excluded
    # the machineless $250 is in the total but not attributed to a machine
    assert sum(m["cost"] for m in t["worsening_machines"]) == 500
    assert t["worsening_machines"][0]["machine_id"] == 1
    # daily series still reconciles to the (machine'd + machine-less) total
    assert sum(d["cost"] for d in t["series"]) == 750


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
