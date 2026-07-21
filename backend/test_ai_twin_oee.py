"""Unit tests for ai.twin._oee_from_records — the AI platform's pooled OEE.

This is the OEE the Executive-OEE card, the briefing headline, losses and
scorecard all report. It *pools* records (sum runtime/planned/counts, compute
once) rather than averaging per-record OEE, and clamps every component to [0,1].
Untested until now — and it's the headline number in a demo, so lock the formula,
the clamps and the zero-guards down. Pure function, stub rows, no DB.

Run:  python backend/test_ai_twin_oee.py     (exit 0 = pass)
"""
from types import SimpleNamespace

from ai.twin import _oee_from_records


def _r(**kw):
    return SimpleNamespace(**kw)


def test_pooled_oee_known_values():
    # planned 480, runtime 450 -> a .9375; ideal 30*800 / (450*60) -> p .8889;
    # good 790/800 -> q .9875; oee .8229 -> 82.
    o = _oee_from_records([_r(planned_minutes=480, runtime_minutes=450,
                              ideal_cycle_time_seconds=30, total_count=800, good_count=790)])
    assert o == {"oee": 82, "availability": 94, "performance": 89, "quality": 99,
                 "has_data": True}, o
    print("PASS pooled OEE computes the components from known values")


def test_pooled_across_records_uses_sums_not_average():
    # Two records with different planned/runtime: the pool weights by volume.
    recs = [_r(planned_minutes=480, runtime_minutes=480, ideal_cycle_time_seconds=30,
               total_count=900, good_count=900),
            _r(planned_minutes=480, runtime_minutes=240, ideal_cycle_time_seconds=30,
               total_count=300, good_count=300)]
    o = _oee_from_records(recs)
    # availability = (480+240)/(480+480) = 720/960 = .75 -> 75 (ratio of sums)
    assert o["availability"] == 75, o
    assert o["quality"] == 100 and o["has_data"] is True
    print("PASS pooled OEE aggregates by ratio-of-sums (volume-weighted)")


def test_components_clamped_to_100():
    # runtime>planned and an over-fast ideal cycle both clamp to 100%, not >100.
    o = _oee_from_records([_r(planned_minutes=100, runtime_minutes=130,
                              ideal_cycle_time_seconds=600, total_count=100, good_count=100)])
    assert o["availability"] == 100 and o["performance"] == 100 and o["quality"] == 100
    assert o["oee"] == 100
    print("PASS OEE components are clamped to 100%")


def test_zero_guards_and_empty():
    z = _oee_from_records([_r(planned_minutes=0, runtime_minutes=0,
                              ideal_cycle_time_seconds=0, total_count=0, good_count=0)])
    assert z == {"oee": 0, "availability": 0, "performance": 0, "quality": 0,
                 "has_data": True}, z
    empty = _oee_from_records([])
    assert empty["has_data"] is False and empty["oee"] == 0
    print("PASS zero production is guarded; empty input reports has_data False")


def test_handles_none_fields():
    # Records with NULL numeric fields (fresh rows) must not crash.
    o = _oee_from_records([_r(planned_minutes=None, runtime_minutes=None,
                              ideal_cycle_time_seconds=None, total_count=None, good_count=None)])
    assert o["oee"] == 0 and o["has_data"] is True
    print("PASS None numeric fields coalesce to 0")


if __name__ == "__main__":
    test_pooled_oee_known_values()
    test_pooled_across_records_uses_sums_not_average()
    test_components_clamped_to_100()
    test_zero_guards_and_empty()
    test_handles_none_fields()
    print("ALL AI-TWIN OEE TESTS PASSED")
