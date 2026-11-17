"""Unit tests for the shared quality fail-rate metric.

analytics_engine.quality_fail_rate is the ONE definition of `failed / inspected`
as a percentage, shared by the quality escalation generator (quality_routes) and
the AI quality recommendation (recommendations_routes), which each kept their own
inline copy of the formula. Before it was shared and clamped, a corrupt row with
failed > inspected (a legacy / raw-SQL / migration row that violates the
passed+failed <= inspected invariant the write paths enforce) surfaced as a fail
rate above 100% in an escalation note / recommendation — a number the metric
can't support. These lock the correct behaviour: the clamp is a strict no-op on
every valid row and only corrects the dishonest > 100% (or < 0%) display.

Run:  python backend/test_quality_fail_rate.py     (exit 0 = pass)
"""
from analytics_engine import quality_fail_rate as q


def test_valid_rows_are_the_plain_ratio():
    # Independently derived: failed / inspected * 100.
    assert q(5, 100) == 5.0            # 5 / 100
    assert q(13, 50) == 26.0           # 13 / 50
    assert q(3, 40) == 7.5             # 3 / 40
    assert q(0, 100) == 0.0            # no failures
    assert q(7, 7) == 100.0            # every unit failed — valid, exactly at the ceiling
    # A strict no-op on a valid row: identical to the raw formula it replaces.
    assert q(13, 50) == 13 / 50 * 100
    assert q(1, 3) == 1 / 3 * 100      # unrounded (caller rounds at display)
    print("PASS valid rows return the plain failed/inspected ratio")


def test_nothing_inspected_has_no_rate():
    # No basis for a rate — matches the `if inspected else 0` guard both callers had.
    assert q(0, 0) == 0.0
    assert q(5, 0) == 0.0              # a failed count with nothing inspected is still 0, not a crash
    print("PASS zero inspected -> 0.0 (no basis), never a ZeroDivision")


def test_null_columns_do_not_raise():
    # failed / inspected are nullable-without-default columns; a legacy/raw-SQL row
    # can hold NULL. Coalesced to 0, never a TypeError.
    assert q(None, 100) == 0.0
    assert q(5, None) == 0.0
    assert q(None, None) == 0.0
    print("PASS None failed / inspected -> 0.0 (coalesced, no TypeError)")


def test_corrupt_row_is_clamped_to_the_honest_ceiling():
    # THE bug this fixes: a row with failed > inspected (only reachable by raw SQL /
    # a migration — the API write paths enforce passed+failed <= inspected) made the
    # old inline `failed / inspected * 100` print e.g. 150% / 250% in an escalation
    # note and a recommendation. A fail rate physically cannot exceed 100%.
    assert q(150, 100) == 100.0        # NOT 150.0
    assert q(150, 100) != 150.0
    assert q(250, 100) == 100.0        # NOT 250.0
    assert q(60, 50) == 100.0          # NOT 120.0
    print("PASS failed > inspected clamps to 100%, not an impossible > 100% rate")


def test_negative_failed_is_floored_not_sub_zero():
    # Symmetric with the OEE quality clamp: a negative failed on a corrupt row
    # cannot print a sub-zero rate.
    assert q(-5, 100) == 0.0           # NOT -5.0
    assert q(-5, 100) >= 0.0
    print("PASS negative failed floors to 0%, never a sub-zero rate")


def test_the_call_sites_round_a_clamped_value():
    # The recommendation rounds to a whole percent; a corrupt row reads 100%, not 250%.
    assert round(q(250, 100)) == 100
    assert round(q(13, 50)) == 26      # valid row unchanged
    # The escalation note rounds to one decimal off the same clamped float.
    assert round(q(250, 100), 1) == 100.0
    assert round(q(3, 40), 1) == 7.5   # valid row unchanged
    print("PASS both call sites round a value that is already clamped to <= 100")


if __name__ == "__main__":
    test_valid_rows_are_the_plain_ratio()
    test_nothing_inspected_has_no_rate()
    test_null_columns_do_not_raise()
    test_corrupt_row_is_clamped_to_the_honest_ceiling()
    test_negative_failed_is_floored_not_sub_zero()
    test_the_call_sites_round_a_clamped_value()
    print("ALL QUALITY FAIL-RATE TESTS PASSED")
