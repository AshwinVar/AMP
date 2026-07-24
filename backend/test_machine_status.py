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


if __name__ == "__main__":
    test_normalize_status_is_case_insensitive_and_rejects_unknown()
    test_clamp_utilization_bounds_the_percentage()
    print("ALL MACHINE-STATUS TESTS PASSED")
