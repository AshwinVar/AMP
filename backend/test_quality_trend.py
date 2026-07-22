"""Quality trend read-model tests (ADR-0007).

The quality summary answers "how good is it". This one answers "which way is it
going, and who moved it": this week's fail rate against last week's on the same
basis, the units the swing costs, and the machines and defect categories behind
it. Run:
    python backend/test_quality_trend.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import quality


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_seq = [0]


def _insp(machine_id, days_ago, inspected, failed, defect="Dimensional"):
    _seq[0] += 1
    # UTC like the read-model — date.today() is local and would drift the window.
    at = datetime.utcnow() - timedelta(days=days_ago)
    return models.QualityInspection(
        inspection_no=f"QI-T{_seq[0]}", machine_id=machine_id, inspector="Ada",
        inspected_quantity=inspected, passed_quantity=inspected - failed,
        failed_quantity=failed, defect_category=defect if failed else "",
        rework_quantity=0, scrap_quantity=0, status="Passed" if not failed else "Failed",
        created_at=at)


def _machines(db):
    db.add_all([
        models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90, line="SMT"),
        models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"),
    ])


def test_worsening_week_is_measured_priced_and_blamed():
    db = _fresh_session()
    _machines(db)
    db.add_all([
        # last week: 1000 inspected, 20 failed -> 2.0%
        _insp(1, 10, 500, 10), _insp(2, 9, 500, 10),
        # this week: 1000 inspected, 60 failed -> 6.0%, and machine 1 did it
        _insp(1, 2, 500, 50), _insp(2, 1, 500, 10),
    ])
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")

    assert d["prior"]["fail_rate"] == 2.0, d["prior"]
    assert d["current"]["fail_rate"] == 6.0, d["current"]
    assert d["delta_pts"] == 4.0, d["delta_pts"]
    assert d["direction"] == "worsening", d["direction"]
    # 4 points of drift on 1000 units inspected this week = 40 extra failures.
    assert d["units_swing"] == 40, d["units_swing"]
    assert d["tone"] == "bad", d
    assert not d["thin_sample"]

    assert [m["name"] for m in d["drifting"]] == ["SMT-Reflow-01"], d["drifting"]
    worst = d["drifting"][0]
    assert worst["prior_fail_rate"] == 2.0 and worst["fail_rate"] == 10.0, worst
    assert worst["delta_pts"] == 8.0, worst
    assert "SMT-Reflow-01" in d["verdict"] and "40" in d["verdict"], d["verdict"]
    print("PASS worsening week is measured, priced and blamed")


def test_improving_and_steady_weeks_read_good():
    db = _fresh_session()
    _machines(db)
    db.add_all([_insp(1, 10, 1000, 80), _insp(1, 2, 1000, 20)])   # 8.0% -> 2.0%
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    assert d["direction"] == "improving" and d["delta_pts"] == -6.0, d
    assert d["tone"] == "good" and "down 6.0 pts" in d["verdict"], d["verdict"]
    assert [m["name"] for m in d["improving"]] == ["SMT-Reflow-01"], d["improving"]
    assert d["drifting"] == []

    db = _fresh_session()
    _machines(db)
    db.add_all([_insp(1, 10, 1000, 30), _insp(1, 2, 1000, 33)])   # 3.0% -> 3.3%
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    # Below the drift threshold — noise, not a trend.
    assert d["direction"] == "steady" and d["tone"] == "good", d
    assert d["drifting"] == [] and d["improving"] == []
    print("PASS improving and steady weeks read good")


def test_thin_sample_is_reported_not_condemned():
    db = _fresh_session()
    _machines(db)
    # 10 units last week, 10 this week — a single failure swings 10 points.
    db.add_all([_insp(1, 10, 10, 0), _insp(1, 2, 10, 2)])
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    assert d["direction"] == "worsening", d["direction"]
    assert d["thin_sample"] is True
    assert d["tone"] == "warn", d           # not "bad" — the sample can't carry it
    assert "too little to call a trend" in d["verdict"], d["verdict"]
    # And no machine is blamed off 10 units either.
    assert d["drifting"] == [] and d["unscored_machines"] == 1, d
    print("PASS thin sample is reported, not condemned")


def test_one_week_of_history_has_nothing_to_compare():
    db = _fresh_session()
    _machines(db)
    db.add_all([_insp(1, 2, 500, 25), _insp(2, 1, 500, 25)])
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    assert d["prior"]["inspected"] == 0
    assert d["delta_pts"] is None and d["direction"] == "unknown", d
    assert d["units_swing"] == 0
    assert d["tone"] == "warn" and "nothing to compare" in d["verdict"], d["verdict"]
    print("PASS one week of history has nothing to compare")


def test_defect_movers_rank_growth_and_flag_new_categories():
    db = _fresh_session()
    _machines(db)
    db.add_all([
        _insp(1, 10, 500, 40, defect="Burr"),          # was 40, now 5 -> shrinking
        _insp(1, 9, 500, 10, defect="Solder Bridge"),  # was 10, now 60 -> the mover
        _insp(1, 2, 500, 5, defect="Burr"),
        _insp(1, 1, 500, 60, defect="Solder Bridge"),
        _insp(2, 1, 400, 12, defect="Tombstone"),      # brand new this week
    ])
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    movers = {m["category"]: m for m in d["defect_movers"]}
    assert d["defect_movers"][0]["category"] == "Solder Bridge", d["defect_movers"]
    assert movers["Solder Bridge"]["delta"] == 50, movers["Solder Bridge"]
    assert movers["Tombstone"]["is_new"] is True and movers["Tombstone"]["delta"] == 12
    assert movers["Burr"]["delta"] == -35 and movers["Burr"]["is_new"] is False
    print("PASS defect movers rank growth and flag new categories")


def test_series_is_zero_filled_and_window_bounded():
    db = _fresh_session()
    _machines(db)
    db.add_all([
        _insp(1, 2, 100, 5),
        _insp(1, 30, 100, 90),   # well outside the window — must not leak in
    ])
    db.commit()
    d = quality.build_quality_trend(db, "DEFAULT")
    assert len(d["series"]) == quality.TREND_WINDOW_DAYS == 14, len(d["series"])
    assert [s["date"] for s in d["series"]] == sorted(s["date"] for s in d["series"])
    assert sum(s["inspected"] for s in d["series"]) == 100, d["series"]
    # A day with no inspections reads as zero volume, not as perfect quality.
    quiet = [s for s in d["series"] if s["inspected"] == 0]
    assert len(quiet) == 13 and all(s["fail_rate"] == 0.0 for s in quiet)
    print("PASS series is zero-filled and window-bounded")


def test_empty_plant_is_shaped_not_crashed():
    db = _fresh_session()
    d = quality.build_quality_trend(db, "DEFAULT")
    assert d["current"]["inspected"] == 0 and d["prior"]["inspected"] == 0
    assert d["delta_pts"] is None and d["direction"] == "unknown"
    assert d["drifting"] == [] and d["improving"] == [] and d["defect_movers"] == []
    assert len(d["series"]) == 14 and d["tone"] == "warn"
    print("PASS empty plant is shaped, not crashed")


if __name__ == "__main__":
    test_worsening_week_is_measured_priced_and_blamed()
    test_improving_and_steady_weeks_read_good()
    test_thin_sample_is_reported_not_condemned()
    test_one_week_of_history_has_nothing_to_compare()
    test_defect_movers_rank_growth_and_flag_new_categories()
    test_series_is_zero_filled_and_window_bounded()
    test_empty_plant_is_shaped_not_crashed()
    print("\nAll quality-trend read-model tests passed.")
