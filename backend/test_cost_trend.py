"""Cost-of-losses trend read-model tests (ADR-0007, ADR-0010).

`build_cost_trend` compares this week's losses (unplanned downtime + scrap, in
good units not made) against last week's, on the SAME per-record basis as
build_cost_summary — downtime floored per record, never on the net, converted at
each week's own run rate — and attributes the swing to machines and to the two
drivers. £ figures exist only when the tenant has set its unit value; without it
the verdict speaks in units. The noise floor is the downtime trend's 30 minutes,
in units at the fortnight's run rate.

These tests pin the numbers to independently-derived expected values and cover
the edges the correctness rules call out: an empty table, a zero / prior-empty
denominator, a thin sample, a no-loss record excluded from the loss count, a
machineless row, downtime with no run time, and the daily-series-sums-to-the-halves
reconciliation (rule 3).

Every fixture record makes exactly RATE good units per run minute unless it says
otherwise, so a downtime minute is RATE lost units and the arithmetic stays by hand.

Run:  python backend/test_cost_trend.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import cost

UNIT_VALUE = 2     # £ per good unit, when a test prices


def _fresh_session(unit_value=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    if unit_value is not None:
        db.add(models.TenantConfig(tenant_code="DEFAULT", plan="Pro", unit_value_gbp=unit_value))
        db.commit()
    return db


def _machines(db):
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))


def _rec(machine_id, planned, runtime, rejected, when, rate=1):
    # good = runtime x rate, so the run rate is exactly `rate`; the other NOT-NULL
    # columns are filled with self-consistent values so the row is valid.
    good = runtime * rate
    return models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=60, total_count=good + rejected, good_count=good,
        rejected_count=rejected, created_at=when,
    )


def _worsening_fixture(db):
    now = datetime.utcnow()
    db.add_all([
        # current week (run rate 1): M1 100 down-min + 20 scrap = 120 units;
        # M2 10 down-min + 4 scrap = 14 units -> 134 units (downtime 110, scrap 24)
        _rec(1, 200, 100, 0, now),
        _rec(1, 100, 100, 20, now - timedelta(days=1)),
        _rec(2, 60, 50, 4, now - timedelta(days=2)),
        # prior week (run rate 1): M1 4 scrap = 4 units; M2 50 down-min + 8 scrap = 58
        # -> 62 units (downtime 50, scrap 12)
        _rec(1, 100, 100, 4, now - timedelta(days=8)),
        _rec(2, 100, 50, 8, now - timedelta(days=9)),
        # older than the 14-day window -> excluded entirely
        _rec(1, 500, 100, 40, now - timedelta(days=20)),
    ])
    db.commit()


def test_worsening_trend_attributes_swing_to_machine_and_driver():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    _worsening_fixture(db)

    t = cost.build_cost_trend(db, "DEFAULT")

    assert t["priced"] is True
    assert t["current"] == {"lost_units": 134, "downtime_lost_units": 110, "scrap_units": 24,
                            "downtime_minutes": 110, "cost": 268, "downtime_cost": 220,
                            "scrap_cost": 48, "records": 3, "loss_records": 3}, t["current"]
    assert t["prior"] == {"lost_units": 62, "downtime_lost_units": 50, "scrap_units": 12,
                          "downtime_minutes": 50, "cost": 124, "downtime_cost": 100,
                          "scrap_cost": 24, "records": 2, "loss_records": 2}, t["prior"]
    assert t["delta_units"] == 72 and t["delta_cost"] == 144       # 268 - 124
    assert t["delta_pct"] == 116                                    # round(144 / 124 * 100)
    assert t["move_threshold_units"] == 30                          # 30 min x 1 unit/min
    assert t["direction"] == "worsening"
    assert t["thin_sample"] is False                                # 5 loss records >= 4
    assert t["tone"] == "bad"

    # who moved it: M1 up 116 units / £232 (120 vs 4); M2 down 44 units / £88 (14 vs 58)
    assert [m["machine_id"] for m in t["worsening_machines"]] == [1]
    w = t["worsening_machines"][0]
    assert (w["delta_units"], w["delta_cost"], w["cost"], w["prior_cost"]) == (116, 232, 240, 8), w
    assert [m["machine_id"] for m in t["improving_machines"]] == [2]
    assert t["improving_machines"][0]["delta_units"] == -44
    assert t["improving_machines"][0]["delta_cost"] == -88

    # drivers ranked by change: downtime +60 units / £120, scrap +12 units / £24
    drivers = {d["key"]: d for d in t["drivers"]}
    assert [d["key"] for d in t["drivers"]] == ["downtime", "scrap"]
    assert drivers["downtime"]["delta_cost"] == 120 and drivers["downtime"]["cost"] == 220
    assert drivers["scrap"]["delta_units"] == 12 and drivers["scrap"]["prior_cost"] == 24

    assert "PRESS-01 drove it (+£232)" in t["verdict"], t["verdict"]
    assert "up £144 (116%) to £268" in t["verdict"], t["verdict"]

    # rule 3: the daily series sums back to the half totals, exactly
    assert sum(d["cost"] for d in t["series"]) == 268 + 124
    assert sum(d["downtime_cost"] for d in t["series"]) == 220 + 100
    assert sum(d["scrap_cost"] for d in t["series"]) == 48 + 24
    assert sum(d["lost_units"] for d in t["series"]) == 134 + 62
    # every date the fortnight [now-14d, now) touches, oldest -> newest: fifteen when
    # it opens mid-day (the oldest flagged partial), fourteen when it opens at midnight
    partial = bool(t["series"][0].get("partial"))
    assert len(t["series"]) == cost.TREND_WINDOW_DAYS + (1 if partial else 0), len(t["series"])
    dates = [datetime.fromisoformat(d["date"]).date() for d in t["series"]]
    assert all((b - a).days == 1 for a, b in zip(dates, dates[1:])), dates
    assert not any(d.get("partial") for d in t["series"][1:])


def test_without_a_unit_value_the_trend_speaks_units_only():
    db = _fresh_session()
    _machines(db)
    _worsening_fixture(db)

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["priced"] is False and t["unit_value_gbp"] is None
    assert t["current"]["cost"] is None and t["prior"]["cost"] is None
    assert t["delta_cost"] is None and t["delta_units"] == 72
    assert t["delta_pct"] == 116                                    # round(72 / 62 * 100)
    assert t["direction"] == "worsening"
    assert t["worsening_machines"][0]["delta_cost"] is None
    assert all(d["cost"] is None for d in t["series"])
    assert "£" not in t["verdict"], t["verdict"]
    assert "up 72 good units (116%) to 134 good units" in t["verdict"], t["verdict"]
    assert "PRESS-01 drove it (+116 good units)" in t["verdict"], t["verdict"]


def test_thin_sample_reports_but_does_not_judge():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 300, 100, 0, now),                        # current: 200 down-min -> 200 units, £400
        _rec(2, 100, 100, 0, now - timedelta(days=3)),    # current: a record with no loss
        _rec(1, 100, 100, 20, now - timedelta(days=8)),   # prior: 20 scrap -> £40
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 400
    assert t["current"]["records"] == 2 and t["current"]["loss_records"] == 1  # no-loss row excluded
    assert t["prior"]["cost"] == 40
    assert t["delta_cost"] == 360
    # only 2 loss-making records across both weeks -> reported but not a trend
    assert t["thin_sample"] is True
    assert t["tone"] == "warn"
    assert "2 loss-making records" in t["verdict"]
    assert "too little to call a trend" in t["verdict"]
    assert "+£360 to £400" in t["verdict"], t["verdict"]


def test_empty_table_is_safe_and_reads_as_no_losses():
    db = _fresh_session()
    _machines(db)
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"] == {"lost_units": 0, "downtime_lost_units": 0, "scrap_units": 0,
                            "downtime_minutes": 0, "cost": None, "downtime_cost": None,
                            "scrap_cost": None, "records": 0, "loss_records": 0}
    assert t["prior"]["lost_units"] == 0
    assert t["delta_units"] == 0 and t["delta_cost"] is None
    assert t["delta_pct"] is None                          # no prior week to divide by
    assert t["direction"] == "none"
    assert t["thin_sample"] is False                       # 0 is empty, not thin
    assert t["worsening_machines"] == [] and t["improving_machines"] == []
    assert t["drivers"] == []
    assert t["verdict"] == "No losses in the last 14 days."
    assert t["tone"] == "good"
    assert sum(d["lost_units"] for d in t["series"]) == 0
    priced = cost.build_cost_trend(_fresh_session(UNIT_VALUE), "DEFAULT")
    assert priced["current"]["cost"] == 0 and priced["delta_cost"] == 0


def test_prior_empty_is_worsening_with_no_percentage():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([_rec(1, 100, 100, 10, now - timedelta(days=d)) for d in range(4)])  # 40 units, £80
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 80 and t["prior"]["cost"] == 0
    assert t["delta_cost"] == 80
    assert t["delta_pct"] is None                          # prior is 0 -> no %
    assert t["direction"] == "worsening"                   # 40 units >= 30
    assert t["thin_sample"] is False                       # 4 loss records == threshold
    assert "up £80 to £80 week on week" in t["verdict"], t["verdict"]
    assert "%" not in t["verdict"]


def test_improving_trend_reports_the_drop():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 4 + 4 scrap = 8 units, £16
        _rec(1, 100, 100, 4, now),
        _rec(1, 100, 100, 4, now - timedelta(days=1)),
        # prior: 400 down-min + 40 scrap = 440 units, £880
        _rec(1, 500, 100, 0, now - timedelta(days=8)),
        _rec(1, 100, 100, 40, now - timedelta(days=9)),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 16 and t["prior"]["cost"] == 880
    assert t["delta_cost"] == -864
    assert t["delta_pct"] == -98                           # round(-864 / 880 * 100)
    assert t["direction"] == "improving"
    assert t["tone"] == "good"
    assert "down £864 (98%) to £16 week on week" in t["verdict"], t["verdict"]


def test_small_move_reads_as_steady():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        # current: 20 + 20 scrap = 40 units, £80 (2 loss recs)
        _rec(1, 100, 100, 20, now),
        _rec(1, 100, 100, 20, now - timedelta(days=1)),
        # prior: 20 + 18 scrap = 38 units, £76 (2 loss recs) -> 4 total, not thin
        _rec(1, 100, 100, 20, now - timedelta(days=8)),
        _rec(1, 100, 100, 18, now - timedelta(days=9)),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["delta_units"] == 2 and t["delta_cost"] == 4  # within the 30-unit floor
    assert t["direction"] == "steady"
    assert t["thin_sample"] is False
    assert t["tone"] == "good"
    assert "steady at £80 (a move of £4 week on week)" in t["verdict"], t["verdict"]


def test_the_noise_floor_follows_the_plants_run_rate():
    """25 down-minutes at 3 good units a minute is 75 units: below the floor of
    30 minutes x 3 = 90 units, so steady. A fixed 30-unit floor would call it."""
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 125, 100, 0, now, rate=3),                      # current: 25 down-min -> 75 units
        _rec(1, 100, 100, 0, now - timedelta(days=8), rate=3),  # prior: ran, no loss
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["lost_units"] == 75 and t["delta_units"] == 75
    assert t["move_threshold_units"] == 90
    assert t["direction"] == "steady", t["direction"]


def test_each_week_is_valued_at_its_own_run_rate():
    """30 down-minutes both weeks, but the line made 2 units a minute this week and
    1 last week: 60 units lost against 30. A stop on a faster line loses more output;
    valuing both weeks at the fortnight's pooled 1.5 would read 45 against 45."""
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 130, 100, 0, now, rate=2),
        _rec(1, 130, 100, 0, now - timedelta(days=8), rate=1),
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["lost_units"] == 60 and t["prior"]["lost_units"] == 30, (t["current"], t["prior"])
    assert t["delta_units"] == 30 and t["delta_cost"] == 60
    assert t["move_threshold_units"] == 45                 # 30 min x the pooled 1.5
    assert t["direction"] == "steady"                      # 30 < 45


def test_downtime_with_no_run_time_cannot_be_compared():
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 480, 0, 0, now),                                # current: stopped all shift, nothing ran
        _rec(1, 100, 100, 5, now - timedelta(days=8)),          # prior: 5 units
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["downtime_minutes"] == 480
    assert t["current"]["lost_units"] is None and t["current"]["cost"] is None
    assert t["delta_units"] is None and t["delta_cost"] is None and t["delta_pct"] is None
    assert t["direction"] == "unknown" and t["tone"] == "warn"
    assert "cannot be compared" in t["verdict"], t["verdict"]


def test_no_loss_and_machineless_rows_are_safe():
    # A record with no loss (planned == runtime, 0 rejected) is counted as a record
    # but not a loss record. A machineless row counts toward the totals but can't be
    # attributed to a machine.
    db = _fresh_session(UNIT_VALUE)
    _machines(db)
    now = datetime.utcnow()
    db.add_all([
        _rec(1, 100, 100, 40, now),                        # 40 units, £80, machine 1
        _rec(None, 100, 100, 10, now),                     # 10 units, £20, no machine -> total only
        _rec(2, 100, 100, 0, now),                         # no loss -> record, not a loss record
    ])
    db.commit()

    t = cost.build_cost_trend(db, "DEFAULT")
    assert t["current"]["cost"] == 100                     # 80 + 20 + 0
    assert t["current"]["records"] == 3
    assert t["current"]["loss_records"] == 2               # the no-loss row excluded
    # the machineless £20 is in the total but not attributed to a machine
    assert sum(m["cost"] for m in t["worsening_machines"]) == 80
    assert t["worsening_machines"][0]["machine_id"] == 1
    # daily series still reconciles to the (machine'd + machine-less) total
    assert sum(d["cost"] for d in t["series"]) == 100


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
