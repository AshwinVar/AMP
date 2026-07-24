"""Unit tests for the shared duration parser.

parse_duration_to_minutes feeds every downtime rollup and the predictive risk
score. Before it was shared, predictive_engine's copy misread hour formats
("1 hr" -> 1 minute) and understated risk. These lock the correct behaviour down
for the free-text formats the data actually uses.

Run:  python backend/test_duration.py     (exit 0 = pass)
"""
from duration import parse_duration_to_minutes as p


def test_hour_and_minute_formats():
    # The formats the simulator seed data actually produces.
    assert p("2 hrs 15 min") == 135
    assert p("1 hr 10 min") == 70
    assert p("1 hr") == 60
    assert p("2 hrs") == 120
    assert p("45 min") == 45
    assert p("50 min") == 50
    print("PASS hours + minutes sum correctly (the data's real formats)")


def test_compact_and_bare_formats():
    assert p("2h 30m") == 150
    assert p("1h30m") == 90
    assert p("3h") == 180
    assert p("90") == 90          # bare number = minutes
    assert p("15 minutes") == 15  # only the first (\d+)\s*m matters
    print("PASS compact (2h30m) and bare-number formats")


def test_decimal_hours_are_not_read_as_the_trailing_digit():
    # The bug this fixes: an integer-only \d+ skipped the "1." and matched the "5"
    # in "1.5 h", reading a 90-minute stop as 5 hours (300 min).
    assert p("1.5 hrs") == 90            # NOT 300
    assert p("1.5 hrs") != 300
    assert p("0.5 hr") == 30             # half an hour, not 5 hours
    assert p("2.5 hrs") == 150           # NOT 300
    assert p("1.5 hrs 30 min") == 120    # 90 + 30
    assert p("0.25 hr") == 15            # quarter hour
    assert p("1.25 hr") == 75            # rounds 75.0
    print("PASS decimal hours parse to real minutes (1.5 hrs -> 90, not 300)")


def test_empty_and_none_are_zero():
    assert p("") == 0
    assert p(None) == 0
    assert p("no digits here") == 0
    print("PASS empty / None / no-digits -> 0")


def test_regression_hour_formats_are_not_digit_concatenated():
    # The exact bug this replaced: a digit-concatenation parser read these as
    # 215 / 1 / 120-as-2. They must now be minute-accurate.
    assert p("2 hrs 15 min") != 215
    assert p("1 hr") == 60 and p("2 hrs") == 120
    print("PASS hour formats are parsed, not digit-concatenated (the fixed bug)")


if __name__ == "__main__":
    test_hour_and_minute_formats()
    test_compact_and_bare_formats()
    test_decimal_hours_are_not_read_as_the_trailing_digit()
    test_empty_and_none_are_zero()
    test_regression_hour_formats_are_not_digit_concatenated()
    print("ALL DURATION TESTS PASSED")
