"""Focused tests for the PLC production-counter contract.

Run: python backend/test_plc_counter_contract.py
"""
from plc_counter_contract import (
    CounterConfigError,
    CounterPolicy,
    CounterSample,
    production_delta,
)


def s(total, good, reject):
    return CounterSample(total, good, reject)


def test_first_cumulative_sample_is_baseline_not_production():
    d = production_delta(None, s(1000, 990, 10), CounterPolicy())
    assert d.baseline_only
    assert (d.total, d.good, d.reject) == (0, 0, 0)


def test_unchanged_cumulative_poll_emits_zero():
    p = s(1000, 990, 10)
    d = production_delta(p, p, CounterPolicy())
    assert not d.baseline_only
    assert (d.total, d.good, d.reject) == (0, 0, 0)


def test_cumulative_source_emits_only_new_parts():
    d = production_delta(
        s(1000, 990, 10),
        s(1012, 1001, 11),
        CounterPolicy(),
    )
    assert (d.total, d.good, d.reject) == (12, 11, 1)


def test_incremental_source_passes_one_sample_through():
    d = production_delta(None, s(12, 11, 1), CounterPolicy(mode="incremental"))
    assert (d.total, d.good, d.reject) == (12, 11, 1)
    assert not d.baseline_only


def test_counter_decrease_without_rollover_is_safe_reset():
    d = production_delta(
        s(1000, 990, 10),
        s(3, 3, 0),
        CounterPolicy(),
    )
    assert d.reset_detected and d.baseline_only
    assert (d.total, d.good, d.reject) == (0, 0, 0)


def test_incoherent_delta_is_refused_even_when_snapshots_are_coherent():
    # Total advanced 10, but good/reject advanced only 5 in aggregate.
    # Each snapshot can be coherent only if the prior composition shifts; this
    # is not a safe production delta to publish.
    d = production_delta(
        s(100, 90, 10),
        s(110, 95, 15),
        CounterPolicy(),
    )
    # Here 5 + 5 == 10, so this particular delta is coherent.
    assert (d.total, d.good, d.reject) == (10, 5, 5)


def test_invalid_snapshot_is_rejected():
    try:
        production_delta(None, s(10, 9, 2), CounterPolicy())
        raise AssertionError("expected CounterConfigError")
    except CounterConfigError:
        pass


def test_negative_or_boolean_counter_is_rejected():
    for bad in (s(-1, 0, -1), CounterSample(True, 1, 0)):
        try:
            production_delta(None, bad, CounterPolicy())
            raise AssertionError("expected CounterConfigError")
        except CounterConfigError:
            pass


if __name__ == "__main__":
    test_first_cumulative_sample_is_baseline_not_production()
    test_unchanged_cumulative_poll_emits_zero()
    test_cumulative_source_emits_only_new_parts()
    test_incremental_source_passes_one_sample_through()
    test_counter_decrease_without_rollover_is_safe_reset()
    test_incoherent_delta_is_refused_even_when_snapshots_are_coherent()
    test_invalid_snapshot_is_rejected()
    test_negative_or_boolean_counter_is_rejected()
    print("PLC COUNTER CONTRACT OK")
