"""Pilot-safe PLC/Edge production counter normalization.

A PLC commonly exposes cumulative counters. AMP's existing MQTT ingest stores
each valid production payload as a ProductionRecord, so forwarding the raw
cumulative value on every poll would count the same parts repeatedly.

This module is deliberately independent of a PLC driver. Edge adapters can use
it after reading OPC UA/Modbus and before publishing production deltas.

No database, network or AMP cloud dependency: state can be persisted by the
Edge runtime that owns the connection.
"""
from dataclasses import dataclass
from typing import Optional


class CounterConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CounterSample:
    total: int
    good: int
    reject: int


@dataclass(frozen=True)
class CounterDelta:
    total: int
    good: int
    reject: int
    baseline_only: bool = False
    reset_detected: bool = False
    rollover_detected: bool = False


@dataclass(frozen=True)
class CounterPolicy:
    """How a three-counter PLC source behaves.

    mode:
      cumulative  - PLC values are lifetime/shift/job totals; publish deltas.
      incremental - each sample already represents new production.

    rollover_at is the largest representable counter value (for example 65535).
    A decrease without a configured rollover is treated as a reset: the new
    reading establishes a fresh baseline and publishes NO production. This is
    intentionally conservative; inventing the production made across a reset is
    worse than exposing a measurable gap.
    """

    mode: str = "cumulative"
    rollover_at: Optional[int] = None

    def __post_init__(self):
        if self.mode not in {"cumulative", "incremental"}:
            raise CounterConfigError("mode must be cumulative or incremental")
        if self.rollover_at is not None and self.rollover_at < 1:
            raise CounterConfigError("rollover_at must be a positive integer")


def _validate(sample: CounterSample) -> None:
    values = (sample.total, sample.good, sample.reject)
    if any(type(v) is not int or v < 0 for v in values):
        raise CounterConfigError("production counters must be non-negative integers")
    if sample.good + sample.reject != sample.total:
        raise CounterConfigError("good + reject must equal total")


def _rolled_delta(previous: int, current: int, rollover_at: int) -> int:
    # Counter sequence ... rollover_at-1, rollover_at, 0, 1 ...
    return (rollover_at - previous) + 1 + current


def production_delta(
    previous: Optional[CounterSample],
    current: CounterSample,
    policy: CounterPolicy,
) -> CounterDelta:
    """Convert one PLC counter sample into safe NEW production.

    First cumulative sample is baseline-only. For incremental sources every
    valid sample is already a delta.

    A cumulative decrease is accepted as rollover only when rollover_at is
    configured AND all three counters can produce a coherent rolled delta.
    Otherwise it is a reset and becomes a new baseline with no emitted count.
    """

    _validate(current)

    if policy.mode == "incremental":
        return CounterDelta(current.total, current.good, current.reject)

    if previous is None:
        return CounterDelta(0, 0, 0, baseline_only=True)

    _validate(previous)

    if (current.total >= previous.total
            and current.good >= previous.good
            and current.reject >= previous.reject):
        delta = CounterDelta(
            current.total - previous.total,
            current.good - previous.good,
            current.reject - previous.reject,
        )
        # The source snapshots can each be coherent yet independently reset
        # sub-counters in ways that make the delta incoherent. Refuse it.
        if delta.good + delta.reject != delta.total:
            return CounterDelta(0, 0, 0, baseline_only=True, reset_detected=True)
        return delta

    if policy.rollover_at is not None:
        limit = policy.rollover_at
        if all(v <= limit for v in (
            previous.total, previous.good, previous.reject,
            current.total, current.good, current.reject,
        )):
            dt = _rolled_delta(previous.total, current.total, limit)
            dg = _rolled_delta(previous.good, current.good, limit)
            dr = _rolled_delta(previous.reject, current.reject, limit)
            if dg + dr == dt:
                return CounterDelta(dt, dg, dr, rollover_detected=True)

    return CounterDelta(0, 0, 0, baseline_only=True, reset_detected=True)
