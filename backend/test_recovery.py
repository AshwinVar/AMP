"""Unit tests for the OEE recovery-opportunity read-model (ai.recovery).

build_recovery_summary answers "how much more good output would we make if we
closed the gap to world-class OEE?" — the point gap, recoverable good units over
the window + annualised, and the per-factor gap so the biggest lever is obvious.
Pure over records; the DB fetch is stubbed so no DB is needed.

Run:  python backend/test_recovery.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import ai.recovery as rec


def _run(records):
    # build_recovery_summary calls the module-level _recent_production; stub it.
    original = rec._recent_production
    rec._recent_production = lambda db, days=7: records
    try:
        return rec.build_recovery_summary(db=None, tenant="DEFAULT")
    finally:
        rec._recent_production = original


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


def test_at_world_class_has_no_recoverable_units():
    # A near-perfect run above the 85% benchmark: nothing to recover.
    out = _run([_r(planned_minutes=100, runtime_minutes=100, ideal_cycle_time_seconds=60,
                   total_count=100, good_count=100)])
    assert out["oee"] >= 85 and out["at_world_class"] is True
    assert out["gap_points"] == 0 and out["recoverable_units_window"] == 0
    assert out["biggest_lever"] is None
    print("PASS at/above world-class -> zero gap, zero recoverable, no lever")


def test_no_production_is_safe():
    out = _run([])
    assert out["has_data"] is False and out["recoverable_units_per_year"] == 0
    assert out["biggest_lever"] is None
    print("PASS empty window returns safe empty summary")


if __name__ == "__main__":
    test_recoverable_units_and_gap()
    test_component_gaps_and_biggest_lever()
    test_at_world_class_has_no_recoverable_units()
    test_no_production_is_safe()
    print("ALL RECOVERY READ-MODEL TESTS PASSED")
