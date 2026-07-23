"""Unit tests for the OEE recovery-opportunity read-model (ai.recovery).

build_recovery_summary answers "how much more good output would we make if we
closed the gap to world-class OEE?" — the point gap, recoverable good units over
the window + annualised, and the per-factor gap so the biggest lever is obvious.
Pure over records; the DB fetch is stubbed so no DB is needed.

Run:  python backend/test_recovery.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import ai.recovery as rec


def _run(records, rate=None, prior=None):
    # build_recovery_summary calls module-level _recent_production, _prior_production
    # and _unit_value; stub all three so no DB is needed (rate=None means the tenant
    # hasn't configured one; prior=None means no prior week -> trend "new").
    orig_prod, orig_prior, orig_val = rec._recent_production, rec._prior_production, rec._unit_value
    rec._recent_production = lambda db, days=7: records
    rec._prior_production = lambda db, days=7: (prior or [])
    rec._unit_value = lambda db, tenant: rate
    try:
        return rec.build_recovery_summary(db=None, tenant="DEFAULT")
    finally:
        rec._recent_production, rec._prior_production, rec._unit_value = orig_prod, orig_prior, orig_val


def _r(**kw):
    return SimpleNamespace(**kw)


def test_recoverable_units_and_gap():
    # OEE 72; world-class 85; 690 good this week -> 690*(85/72 - 1) = 125 recoverable,
    # annualised 125*365/7 = 6518.
    out = _run([_r(planned_minutes=480, runtime_minutes=400, ideal_cycle_time_seconds=30,
                   total_count=700, good_count=690)])
    assert out["has_data"] and out["oee"] == 72 and out["world_class"] == 85
    assert out["gap_points"] == 13 and out["at_world_class"] is False
    assert out["recoverable_units_window"] == 125
    assert out["recoverable_units_per_year"] == 6518
    print("PASS gap to world-class translates to recoverable units (window + annual)")


def test_component_gaps_and_biggest_lever():
    out = _run([_r(planned_minutes=480, runtime_minutes=400, ideal_cycle_time_seconds=30,
                   total_count=700, good_count=690)])
    comps = {c["key"]: c for c in out["components"]}
    assert comps["availability"]["current"] == 83 and comps["availability"]["target"] == 90
    assert comps["availability"]["gap_points"] == 7
    assert comps["quality"]["gap_points"] == 0          # quality already at target
    assert out["biggest_lever"] in ("availability", "performance")  # both gap 7
    print("PASS per-component gaps to world-class + biggest lever")


def test_biggest_lever_prize_is_quantified():
    # OEE 72 = availability 83 x performance 87 x quality ~99. Availability is the
    # biggest lever; closing just it (83 -> 90) scales good output by 90/83.
    recs = [_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
               ideal_cycle_time_seconds=30, total_count=700, good_count=690)]
    out = _run(recs, rate=4.50)
    assert out["biggest_lever"] == "availability"
    assert out["lever_label"] == "Availability"
    assert "downtime" in out["lever_action"].lower()
    good = out["good_units_window"]
    expected_year = round(round(good * (90 / 83 - 1)) * 365 / 7)
    assert out["lever_recoverable_units_per_year"] == expected_year
    assert out["lever_recoverable_value_per_year"] == round(expected_year * 4.50)
    # the single lever is a slice of the total gap, never larger than it
    assert out["lever_recoverable_units_per_year"] <= out["recoverable_units_per_year"]
    print("PASS biggest-lever prize (units + £) is quantified for 'fix this first'")


def test_lever_fields_empty_at_world_class():
    out = _run([_r(machine_id=1, planned_minutes=100, runtime_minutes=100,
                   ideal_cycle_time_seconds=60, total_count=100, good_count=100)])
    assert out["at_world_class"] is True and out["biggest_lever"] is None
    assert out["lever_label"] is None and out["lever_action"] is None
    assert out["lever_recoverable_units_per_year"] == 0
    assert out["lever_recoverable_value_per_year"] is None
    print("PASS lever fields are empty when already at world-class")


def test_at_world_class_has_no_recoverable_units():
    # A near-perfect run above the 85% benchmark: nothing to recover.
    out = _run([_r(planned_minutes=100, runtime_minutes=100, ideal_cycle_time_seconds=60,
                   total_count=100, good_count=100)])
    assert out["oee"] >= 85 and out["at_world_class"] is True
    assert out["gap_points"] == 0 and out["recoverable_units_window"] == 0
    assert out["biggest_lever"] is None
    print("PASS at/above world-class -> zero gap, zero recoverable, no lever")


def test_pound_value_when_rate_configured():
    # 6518 recoverable units/yr at £4.50/unit -> £29,331/yr.
    recs = [_r(planned_minutes=480, runtime_minutes=400, ideal_cycle_time_seconds=30,
               total_count=700, good_count=690)]
    out = _run(recs, rate=4.50)
    assert out["unit_value_gbp"] == 4.50
    assert out["recoverable_value_per_year"] == 29331
    assert out["recoverable_value_window"] == round(out["recoverable_units_window"] * 4.50)
    print("PASS £ recovery value is computed when a per-unit rate is set")


def test_no_pound_value_when_rate_unset():
    # No configured rate -> report units only, never a made-up £ figure.
    recs = [_r(planned_minutes=480, runtime_minutes=400, ideal_cycle_time_seconds=30,
               total_count=700, good_count=690)]
    out = _run(recs, rate=None)
    assert out["unit_value_gbp"] is None
    assert out["recoverable_value_window"] is None and out["recoverable_value_per_year"] is None
    assert out["recoverable_units_per_year"] > 0  # units still reported
    print("PASS £ fields stay null when no rate is configured (units still shown)")


def test_zero_rate_yields_zero_pounds_not_null():
    # A configured rate of 0 is a real £0 margin (rate is SET), so the £ fields are
    # 0, not null. Using truthiness (`if rate`) treated 0 like unset and showed
    # units-only, diverging from build_management_summary — both now agree on £0.
    recs = [_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
               ideal_cycle_time_seconds=30, total_count=700, good_count=690)]
    out = _run(recs, rate=0)
    assert out["unit_value_gbp"] == 0
    assert out["recoverable_value_window"] == 0 and out["recoverable_value_per_year"] == 0
    assert out["lever_recoverable_value_per_year"] == 0
    print("PASS a configured £0 rate yields £0 (not null): rate is set, margin is zero")


def test_oee_trend_improving_vs_prior_week():
    # This week OEE 72; last week was lower -> the plant is closing the gap.
    cur = [_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
              ideal_cycle_time_seconds=30, total_count=700, good_count=690)]
    prior = [_r(machine_id=1, planned_minutes=480, runtime_minutes=300,
                ideal_cycle_time_seconds=60, total_count=300, good_count=280)]
    out = _run(cur, prior=prior)
    assert out["oee_trend"] == "improving"
    assert out["prior_oee"] is not None and out["prior_oee"] < out["oee"]
    assert out["oee_points_delta"] == out["oee"] - out["prior_oee"] > 0
    print("PASS OEE trend reads 'improving' when this week beats last week")


def test_oee_trend_new_when_no_prior_week():
    out = _run([_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
                   ideal_cycle_time_seconds=30, total_count=700, good_count=690)])
    assert out["oee_trend"] == "new"
    assert out["prior_oee"] is None and out["oee_points_delta"] is None
    print("PASS OEE trend is 'new' when there is no prior week to compare")


def test_no_production_is_safe():
    out = _run([])
    assert out["has_data"] is False and out["recoverable_units_per_year"] == 0
    assert out["biggest_lever"] is None
    print("PASS empty window returns safe empty summary")


def test_physical_cap_is_noop_on_plausible_data():
    # One real machine, one week of physically-possible production: nothing is
    # scaled — the cap only ever trims the impossible.
    recs = [_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
               ideal_cycle_time_seconds=30, total_count=700, good_count=690)]
    out = _run(recs)
    assert out["good_units_window"] == 690       # unchanged
    assert out["recoverable_units_window"] == 125
    print("PASS physical cap is a no-op on physically-plausible data")


def test_physical_cap_tames_impossible_volume():
    # 100 identical shift-records on ONE machine in a 7-day window = 48,000 planned
    # minutes, but one machine can run at most 7*1440 = 10,080. Good output is
    # scaled to physical capacity before annualising, so the figure can't explode.
    recs = [_r(machine_id=1, planned_minutes=480, runtime_minutes=400,
               ideal_cycle_time_seconds=30, total_count=700, good_count=690)
            for _ in range(100)]
    out = _run(recs)
    raw_good = 100 * 690
    ceiling = 1 * 7 * 24 * 60
    expected = round(raw_good * ceiling / (100 * 480))
    assert out["oee"] == 72, out["oee"]           # ratio is unaffected by the cap
    assert out["good_units_window"] == expected < raw_good
    # everything downstream derives from the capped good, bounded by a physical week
    expected_window = round(expected * (85 / 72 - 1))
    expected_year = round(expected_window * 365 / 7)
    assert out["recoverable_units_window"] == expected_window
    assert out["recoverable_units_per_year"] == expected_year
    # and that's a fraction of the un-capped (100x) figure it would otherwise show
    assert expected_year < round(raw_good * (85 / 72 - 1)) * 365 / 7 / 3
    print(f"PASS physical cap tames impossible volume ({raw_good:,} -> {expected:,} good)")


if __name__ == "__main__":
    test_recoverable_units_and_gap()
    test_component_gaps_and_biggest_lever()
    test_biggest_lever_prize_is_quantified()
    test_lever_fields_empty_at_world_class()
    test_at_world_class_has_no_recoverable_units()
    test_pound_value_when_rate_configured()
    test_no_pound_value_when_rate_unset()
    test_zero_rate_yields_zero_pounds_not_null()
    test_oee_trend_improving_vs_prior_week()
    test_oee_trend_new_when_no_prior_week()
    test_no_production_is_safe()
    test_physical_cap_is_noop_on_plausible_data()
    test_physical_cap_tames_impossible_volume()
    print("ALL RECOVERY READ-MODEL TESTS PASSED")
