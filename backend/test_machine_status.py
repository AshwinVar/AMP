"""Unit tests for the shared machine-status normalisation (machine_status.py).

Machine status drives every status-based rollup; utilization is a percentage.
Both are written straight from edge telemetry, so these lock down that an
unknown status maps to None (caller leaves state untouched) and a percentage is
clamped into range.

Run:  python backend/test_machine_status.py     (exit 0 = pass)
"""
import machine_status as ms


def test_normalize_status_is_case_insensitive_and_rejects_unknown():
    assert ms.normalize_machine_status("running") == "Running"
    assert ms.normalize_machine_status("BREAKDOWN") == "Breakdown"
    assert ms.normalize_machine_status("  Idle ") == "Idle"
    assert ms.normalize_machine_status("Maintenance") == "Maintenance"
    # unknown / empty / None -> None, so the caller leaves machine.status alone
    assert ms.normalize_machine_status("faulted") is None
    assert ms.normalize_machine_status("Frobnicate") is None
    assert ms.normalize_machine_status("") is None
    assert ms.normalize_machine_status(None) is None
    print("PASS normalize_machine_status: case-insensitive canonical, None on unknown")


def test_clamp_utilization_bounds_the_percentage():
    assert ms.clamp_utilization(150) == 100        # over 100 -> 100
    assert ms.clamp_utilization(-20) == 0          # negative -> 0
    assert ms.clamp_utilization(73) == 73          # in range unchanged
    assert ms.clamp_utilization(100) == 100 and ms.clamp_utilization(0) == 0
    assert ms.clamp_utilization(73.6) == 74        # rounded to a whole percent
    assert ms.clamp_utilization("nan-ish") is None  # non-numeric -> None (skip write)
    assert ms.clamp_utilization(None) is None
    print("PASS clamp_utilization: bounds a raw sensor reading into [0, 100]")


def test_clamp_utilization_rejects_nan_and_infinity():
    """NaN / +-inf pass float() but round(nan) raises ValueError and round(inf)
    OverflowError — the float()-only guard didn't catch them, so a glitching
    sensor reading (or a NaN/Infinity token in inbound JSON, which json.loads
    decodes) crashed the ingest path instead of being skipped. They are not real
    percentages, so the helper's own contract ("None if it isn't a number") wants
    None. Both the float forms and the JSON string tokens are exercised."""
    assert ms.clamp_utilization(float("nan")) is None
    assert ms.clamp_utilization(float("inf")) is None
    assert ms.clamp_utilization(float("-inf")) is None
    # str() of these decode back to non-finite floats via float(), same as a raw
    # JSON NaN/Infinity token would reach the helper.
    assert ms.clamp_utilization("nan") is None
    assert ms.clamp_utilization("inf") is None
    assert ms.clamp_utilization("-inf") is None
    assert ms.clamp_utilization("Infinity") is None
    # a real reading still clamps normally (the guard didn't over-reach)
    assert ms.clamp_utilization(87.4) == 87
    print("PASS clamp_utilization: NaN / +-inf are rejected as None, not a crash")


if __name__ == "__main__":
    test_normalize_status_is_case_insensitive_and_rejects_unknown()
    test_clamp_utilization_bounds_the_percentage()
    test_clamp_utilization_rejects_nan_and_infinity()
    print("ALL MACHINE-STATUS TESTS PASSED")
