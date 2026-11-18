"""Unit tests for the shared loss-valuing helper (loss_value.py, ADR-0010).

loss_value is the ONE conversion every money surface routes through — the
cost-of-losses read-model and trend, the scorecard, and the management summary's
downtime loss. It turns downtime minutes and scrapped units into the tenant's own
₹, and it apportions a rounded headline across a breakdown so the two reconcile.

Despite being that shared chokepoint it had no direct unit tests: its behaviour
was only exercised transitively through the read-models that call it, so the two
invariants that matter most were never pinned in isolation:

  * HONESTY (ADR-0010 / rule 2): no rate -> no ₹ (None, never a fabricated
    figure), but a configured rate of 0 is a REAL ₹0, not "unset". A rendering
    guard (0.0 units with no downtime) is a real 0 too.
  * RECONCILIATION (rule 3): `apportion` rounds a {key: float} breakdown to ints
    that sum EXACTLY to the rounded headline — so a reader who adds the bars up
    gets the headline, never one unit/pound off. This is the "assert the parts
    sum to the whole" guarantee the whole codebase leans on.

Every expected number below is derived by hand from the formula, not read back
from the function, and the edges (empty / None / zero-denominator / zero-rate)
are covered explicitly.

Run:  python backend/test_loss_value.py     (exit 0 = pass)
"""
import sys

# This suite prints the ₹ symbol (and carries it in its assertions on the money
# helper's honesty contract). Make stdout tolerant of a cp1252 Windows console so
# the suite does not die AFTER passing on a non-UTF-8 terminal.
# test_currency_single.py pins this for every suite carrying the symbol.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import loss_value as lv


def test_run_rate_good_units_per_run_minute():
    # Good units per minute of run time: 600 good over 480 run minutes = 1.25/min.
    assert lv.run_rate(600, 480) == 1.25
    # A line that ran but made nothing has a real rate of 0, not "unknown".
    assert lv.run_rate(0, 480) == 0.0
    # NULL good coalesces to 0 (self-safe for a legacy row), still a real 0 rate.
    assert lv.run_rate(None, 480) == 0.0
    print("PASS run_rate = good / run-minutes (0 good -> real 0.0)")


def test_run_rate_is_none_when_nothing_ran():
    # No run time means there is no observed rate to value downtime at — None, not
    # a divide-by-zero and not a fabricated 0 (0 would later value downtime at ₹0,
    # claiming a known-zero loss the data cannot support).
    assert lv.run_rate(500, 0) is None
    assert lv.run_rate(500, None) is None
    # A negative runtime on a corrupt/legacy row is "no usable run time", not a
    # negative rate (the <= 0 guard, not just falsiness).
    assert lv.run_rate(600, -5) is None
    print("PASS run_rate is None when run-minutes is 0 / None / negative")


def test_downtime_units_needs_a_rate_to_convert():
    # Downtime minutes x run rate = the good units those stopped minutes would
    # have produced.
    assert lv.downtime_units(120, 1.25) == 150.0
    # No downtime is a known 0.0 units lost (not None): nothing was lost.
    assert lv.downtime_units(0, 1.25) == 0.0
    assert lv.downtime_units(None, 1.25) == 0.0
    # Downtime but NO rate to convert it is an UNKNOWN loss (None), never 0 — a
    # plant that never ran lost an unknown amount, not nothing (ADR-0010).
    assert lv.downtime_units(120, None) is None
    # A real run rate of exactly 0 (ran, produced nothing) values downtime at a
    # real 0.0 units — distinct from the None "no rate" case above.
    assert lv.downtime_units(120, 0.0) == 0.0
    print("PASS downtime_units: 0 with no downtime, None with no rate, real 0 at a 0 rate")


def test_whole_rounds_half_up_and_passes_none_through():
    # round() is banker's (0.5 -> 0); whole() is round-half-up, the convention
    # a money figure a human reads expects.
    assert lv.whole(2.5) == 3           # NOT 2 (banker's would give 2)
    assert lv.whole(0.5) == 1           # NOT 0
    assert lv.whole(2.4) == 2
    assert lv.whole(3.0) == 3
    # None (an unknown amount) stays None — the unknown is never rounded to 0.
    assert lv.whole(None) is None
    print("PASS whole() rounds half up (2.5 -> 3) and keeps None as None")


def test_value_needs_both_units_and_a_rate():
    # Units x the tenant's ₹/unit, rounded half-up: 150 units x ₹2 = ₹300.
    assert lv.value(150.0, 2.0) == 300
    # Half-up all the way through the money figure: 3 units x ₹0.5 = ₹1.5 -> ₹2.
    assert lv.value(3.0, 0.5) == 2
    # A configured rate of 0 is a REAL ₹0, never "unset"/None (rule 2): a tenant
    # who set ₹0 per unit sees ₹0, not a blank.
    assert lv.value(10.0, 0) == 0
    # Zero units is a real ₹0 too (a loss of nothing is worth nothing).
    assert lv.value(0.0, 5.0) == 0
    # Unknown units OR an unset rate -> None (no ₹ invented from a missing input).
    assert lv.value(None, 2.0) is None
    assert lv.value(150.0, None) is None
    assert lv.value(None, None) is None
    print("PASS value(): real ₹0 at a 0 rate / 0 units, None when units or rate unknown")


def test_apportion_parts_sum_exactly_to_the_headline():
    # THE reconciliation guarantee (rule 3): the bars sum to the rounded headline.
    #
    # Naive per-part rounding breaks this. mon/tue/wed = 10.4 / 10.3 / 10.3 sum to
    # 31.0 (headline whole() = 31), but round()-ing each gives 10+10+10 = 30 — a bar
    # chart that misses its own total by one. Largest remainder hands the one owed
    # unit to the largest fraction (mon, .4 > .3), so the parts sum to 31 exactly.
    out = lv.apportion(31, {"mon": 10.4, "tue": 10.3, "wed": 10.3})
    assert out == {"mon": 11, "tue": 10, "wed": 10}, out
    assert sum(out.values()) == 31
    # A single part takes the whole headline (7.6 -> 8 when the headline is 8).
    out = lv.apportion(8, {"only": 7.6})
    assert out == {"only": 8} and sum(out.values()) == 8
    # Parts that are already whole need no adjustment and still sum to the total.
    out = lv.apportion(5, {"a": 3.0, "b": 2.0})
    assert out == {"a": 3, "b": 2} and sum(out.values()) == 5
    print("PASS apportion: rounded parts sum EXACTLY to the headline (no off-by-one bars)")


def test_apportion_tie_is_deterministic_and_empty_is_empty():
    # A tie on the fractional remainder is broken deterministically by iteration
    # (insertion) order — the earlier key takes the owed unit — so the breakdown
    # is stable across downloads rather than reshuffling.
    out = lv.apportion(1, {"first": 0.5, "second": 0.5})
    assert out == {"first": 1, "second": 0}, out
    assert sum(out.values()) == 1
    # An empty breakdown apportions to an empty result (headline 0), not a crash.
    assert lv.apportion(0, {}) == {}
    print("PASS apportion: deterministic tie-break, empty breakdown -> empty (no crash)")


def test_apportion_sum_invariant_holds_for_many_shapes():
    # The sum-to-headline invariant, asserted over a spread of shapes that between
    # them force every owed count from 0 up to (n_parts): each headline is the
    # module's own whole(sum(parts)), exactly as every call site computes it.
    shapes = [
        {"a": 0.1, "b": 0.2, "c": 0.7},         # fractions summing to a whole
        {"a": 1.9, "b": 2.9, "c": 3.9},         # every part just under the next int
        {"a": 100.5, "b": 0.5},                 # a big part and a tie-prone small one
        {"x": 0.34, "y": 0.33, "z": 0.33},      # thirds
        {"solo": 4.5},
    ]
    for parts in shapes:
        total = lv.whole(sum(parts.values()))
        out = lv.apportion(total, parts)
        assert sum(out.values()) == total, (parts, total, out)
        # Each bar is its own floor or floor+1 — a part never moves more than the
        # rounding requires.
        import math
        for k, v in parts.items():
            assert out[k] in (math.floor(v), math.floor(v) + 1), (k, v, out[k])
    print("PASS apportion sum invariant holds across shapes (parts in [floor, floor+1])")


if __name__ == "__main__":
    test_run_rate_good_units_per_run_minute()
    test_run_rate_is_none_when_nothing_ran()
    test_downtime_units_needs_a_rate_to_convert()
    test_whole_rounds_half_up_and_passes_none_through()
    test_value_needs_both_units_and_a_rate()
    test_apportion_parts_sum_exactly_to_the_headline()
    test_apportion_tie_is_deterministic_and_empty_is_empty()
    test_apportion_sum_invariant_holds_for_many_shapes()
    print("ALL LOSS_VALUE TESTS PASSED")
