"""Telemetry anomaly: the synthetic evaluation generator.

WHY THIS EXISTS
---------------
The anomaly scorer is judged on synthetic machines, because AMP never trains or
evaluates on a customer's telemetry without consent. A synthetic evaluation is
only worth something if the generator is:

* DETERMINISTIC - the committed numbers can be rebuilt from the seed;
* INDEPENDENT of the scorer - it must not import the scorer, the rule-based
  predictors, the demo simulators or the database, or the evaluation just
  measures how well the model agrees with itself;
* HONEST about what it injects - an anomaly lives only in the scored copy of a
  window, never in the history the baseline is fitted on, and the declared
  static ranges really are the envelopes of normal operation.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_eval.py
"""
import math
import os

from amp_ai.core import purity as P
from amp_ai.telemetry_anomaly import synthetic as S

BACKEND = os.path.dirname(os.path.abspath(__file__))
SYNTHETIC_PY = os.path.join(BACKEND, "amp_ai", "telemetry_anomaly", "synthetic.py")

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


SMALL = dict(n_series=6, windows_per_series=4, min_history_days=3, max_history_days=4, anomaly_fraction=0.5)


def _dataset(seed=7, split="test", variant="main", **overrides):
    cfg = dict(SMALL, **overrides)
    return list(S.generate_dataset(seed, split, variant=variant, **cfg))


def _fingerprint(series_list):
    out = []
    for s in series_list:
        out.append((s.series_id, s.minutes, tuple(sorted(s.signals)),
                    tuple(tuple(s.signals[n]) for n in sorted(s.signals)),
                    tuple(s.running),
                    tuple((w.end_minute, w.label, w.anomaly_type,
                           tuple(tuple(w.readings[n]) for n in sorted(w.readings))) for w in s.windows)))
    return out


# --------------------------------------------------------------------------- generator
def section_determinism():
    print("\n[generator: deterministic, stream-separated]")
    a = _dataset()
    b = _dataset()
    check("same seed and split -> identical series, windows and labels", _fingerprint(a) == _fingerprint(b))
    c = _dataset(split="validation")
    check("validation and test splits draw different machines", _fingerprint(a) != _fingerprint(c))
    d = _dataset(seed=8)
    check("a different seed gives different data", _fingerprint(a) != _fingerprint(d))


def section_shape():
    print("\n[generator: shape of a machine]")
    data = _dataset()
    check("n_series machines generated", len(data) == SMALL["n_series"], str(len(data)))
    for s in data:
        names = set(s.signals)
        if not (set(S.BASE_SIGNALS) <= names and 4 <= len(names) <= 6 and names <= set(S.ALL_SIGNALS)):
            check(f"{s.series_id}: 4-6 signals including the base set", False, str(sorted(names)))
            return
        if any(len(v) != s.minutes for v in s.signals.values()) or len(s.running) != s.minutes:
            check(f"{s.series_id}: one reading slot per minute for every signal", False)
            return
        if s.minutes % (5 * 1) or s.minutes % S.MINUTES_PER_DAY:
            check(f"{s.series_id}: the series ends on a whole day", False, str(s.minutes))
            return
    check("every machine has 4-6 signals including the base set, one slot per minute", True)
    optional_counts = {len(s.signals) for s in _dataset(n_series=24, windows_per_series=1, anomaly_fraction=0.0)}
    check("optional signals vary between machines", len(optional_counts) >= 2, str(optional_counts))

    s = data[0]
    running = sum(s.running)
    check("both running and not-running minutes occur", 0 < running < s.minutes, f"{running}/{s.minutes}")
    transitions = S.state_transitions(s)
    check("state transitions are reported as (minute, running) changes",
          all(s.running[m] == r and (m == 0 or s.running[m - 1] != r) for m, r in transitions)
          and len(transitions) >= 2)
    total = sum(len(v) for v in s.signals.values())
    missing = sum(1 for v in s.signals.values() for x in v if x is None)
    check("dropouts exist but are a minority of readings", 0.003 < missing / total < 0.15, f"{missing}/{total}")
    ints = all(float(x).is_integer() for n in S.INTEGER_SIGNALS if n in s.signals for x in s.signals[n] if x is not None)
    check("integer-valued sensors report whole numbers", ints)
    check("integer_signals lists exactly the integer sensors present",
          s.integer_signals == frozenset(n for n in S.INTEGER_SIGNALS if n in s.signals))


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


def section_physics():
    print("\n[generator: stated physics hold]")
    s = _dataset(n_series=1, windows_per_series=1, anomaly_fraction=0.0, min_history_days=4, max_history_days=4)[0]
    pairs = [(s.signals["spindle_load_pct"][m], s.signals["motor_current_a"][m]) for m in range(s.minutes)
             if s.running[m] and s.signals["spindle_load_pct"][m] is not None
             and s.signals["motor_current_a"][m] is not None and m not in s.benign_spikes.get("spindle_load_pct", {})
             and m not in s.benign_spikes.get("motor_current_a", {})]
    r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
    check("while running, motor current tracks spindle load (r > 0.9)", r > 0.9, f"r={r:.3f}")
    run_t = [s.signals["bearing_temp_c"][m] for m in range(s.minutes) if s.running[m] and s.signals["bearing_temp_c"][m] is not None]
    idle_t = [s.signals["bearing_temp_c"][m] for m in range(s.minutes) if not s.running[m] and s.signals["bearing_temp_c"][m] is not None]
    check("bearings run warmer than they idle", sum(run_t) / len(run_t) > sum(idle_t) / len(idle_t) + 3)

    # the declared static ranges are honest envelopes of normal operation
    inside = total = 0
    for data in (_dataset(n_series=8, windows_per_series=1, anomaly_fraction=0.0, min_history_days=3, max_history_days=3),):
        for s in data:
            for n, values in s.signals.items():
                lo, hi = s.declared_ranges[n]
                spikes = s.benign_spikes.get(n, {})
                for m, v in enumerate(values):
                    if v is None or m in spikes:
                        continue
                    total += 1
                    inside += lo <= v <= hi
    check("clean readings stay inside the declared static ranges (>= 99.5%)", inside / total >= 0.995,
          f"{inside}/{total}")


def section_windows_and_labels():
    print("\n[generator: evaluation windows and injected anomalies]")
    data = _dataset(n_series=12, windows_per_series=5, anomaly_fraction=0.2)
    windows = [(s, w) for s in data for w in s.windows]
    check("every machine gets windows_per_series windows", all(len(s.windows) == 5 for s in data))
    positives = [w for _, w in windows if w.label == 1]
    check("exactly round(anomaly_fraction * total) positives", len(positives) == 12, str(len(positives)))
    counts = {}
    for w in positives:
        counts[w.anomaly_type] = counts.get(w.anomaly_type, 0) + 1
    check("all six anomaly types are injected", set(counts) == set(S.ANOMALY_TYPES), str(counts))
    check("anomaly types are balanced", max(counts.values()) - min(counts.values()) <= 1, str(counts))
    check("negatives carry no anomaly type", all(w.anomaly_type is None for _, w in windows if w.label == 0))

    ok_bounds = ok_running = ok_overlap = ok_neg = ok_pos = ok_onset = True
    for s, w in windows:
        start = w.end_minute - S.WINDOW_MINUTES
        ok_bounds &= (w.end_minute % 5 == 0 and start >= s.history_days * S.MINUTES_PER_DAY
                      and w.end_minute <= s.minutes)
        ok_running &= s.running[start]
        for n in s.signals:
            history = s.signals[n][start:w.end_minute]
            if w.label == 0:
                ok_neg &= (w.readings[n] == history)
        if w.label == 1:
            changed = [n for n in s.signals if w.readings[n] != s.signals[n][start:w.end_minute]]
            ok_pos &= bool(changed) and set(changed) <= set(w.detail["signals"])
            onset = w.detail["onset_minute"]
            ok_onset &= all(w.readings[n][:onset] == s.signals[n][start:start + onset] for n in s.signals)
    for s in data:
        ends = sorted(w.end_minute for w in s.windows)
        ok_overlap &= all(b - a >= S.WINDOW_MINUTES for a, b in zip(ends, ends[1:]))
    check("windows end on a 5-minute boundary inside the evaluation period", ok_bounds)
    check("every window starts with the machine running", ok_running)
    check("windows of one machine never overlap", ok_overlap)
    check("a clean window's readings ARE the machine's history", ok_neg)
    check("an anomaly changes only the scored copy, and only the signals it names", ok_pos)
    check("nothing changes before the anomaly's onset", ok_onset)
    same_machine = dict(history_days=3, windows_per_series=len(S.ANOMALY_TYPES))
    clean = S.generate_series(5, "test", 0, positives={}, **same_machine)
    injected = S.generate_series(5, "test", 0, positives=dict(enumerate(S.ANOMALY_TYPES)), **same_machine)
    check("injecting all six anomaly types leaves the machine's history byte-identical",
          clean.signals == injected.signals and clean.running == injected.running
          and [w.end_minute for w in clean.windows] == [w.end_minute for w in injected.windows]
          and all(w.label == 1 for w in injected.windows))


def section_variants():
    print("\n[generator: misspecification variants]")
    main = _dataset(n_series=3, windows_per_series=1, anomaly_fraction=0.0)
    regime = _dataset(n_series=3, windows_per_series=1, anomaly_fraction=0.0, variant="regime_switching")
    per_day = lambda d: sum(len(S.state_transitions(s)) for s in d) / sum(s.minutes for s in d) * S.MINUTES_PER_DAY
    check("regime_switching changes state at least 3x as often", per_day(regime) >= 3 * per_day(main),
          f"{per_day(regime):.1f} vs {per_day(main):.1f} per day")
    rng_main = S._noise_source(11, "main")
    rng_heavy = S._noise_source(11, "heavy_tails")
    draws = 40000
    tail_main = sum(abs(rng_main()) > 4 for _ in range(draws)) / draws
    tail_heavy = sum(abs(rng_heavy()) > 4 for _ in range(draws)) / draws
    check("heavy_tails noise has far more 4-sigma draws than Gaussian noise", tail_heavy > 10 * max(tail_main, 1e-5),
          f"{tail_heavy:.4f} vs {tail_main:.5f}")
    sd = math.sqrt(sum(rng_heavy() ** 2 for _ in range(draws)) / draws)
    check("heavy_tails noise keeps unit scale (sd within 0.7-1.4)", 0.7 < sd < 1.4, f"{sd:.3f}")
    try:
        _dataset(variant="nope")
        check("an unknown variant is refused", False)
    except ValueError:
        check("an unknown variant is refused", True)


def section_independence():
    print("\n[generator: independent of the scorer, the rules and the database]")
    forbidden = {
        "predictive_engine", "ai", "models", "database", "tenancy", "work_order_status",
        "industrial_adapters", "industrial_demo", "factory_simulator", "oem_telemetry", "mqtt_service",
        "amp_ai.telemetry_anomaly.baseline", "amp_ai.telemetry_anomaly.series",
        "amp_ai.telemetry_anomaly.db_telemetry", "amp_ai.telemetry_anomaly.service",
        "amp_ai.telemetry_anomaly.build_eval", "amp_ai.core.robust", "amp_ai.core.metrics",
    }
    try:
        P.assert_no_forbidden_imports([SYNTHETIC_PY], forbidden, min_files=1)
        check("synthetic.py imports none of the scorer, rules, simulators or database", True)
    except P.PurityViolation as exc:
        check("synthetic.py imports none of the scorer, rules, simulators or database", False, str(exc))
    try:
        P.assert_stdlib_only([SYNTHETIC_PY], min_files=1)
        check("synthetic.py is standard library only", True)
    except P.PurityViolation as exc:
        check("synthetic.py is standard library only", False, str(exc))


SECTIONS = [section_determinism, section_shape, section_physics, section_windows_and_labels,
            section_variants, section_independence]


def main():
    print("=" * 74)
    print("Telemetry anomaly: synthetic generator and evaluation")
    print("=" * 74)
    for section in SECTIONS:
        section()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_anomaly_eval():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
