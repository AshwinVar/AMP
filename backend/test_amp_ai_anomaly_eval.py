"""Telemetry anomaly: the synthetic evaluation generator, the evaluation harness and its adoption gate.

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

And the harness (``build_eval``) is only worth something if:

* each window is scored exactly as production scores it - the fast per-series
  bucket assembly equals ``series.build_buckets`` on the raw readings, and the
  scored hour never enters its own baseline (mutation-tested);
* both baselines see the same windows under the same rules;
* the adoption verdict is a pure function of the committed metrics and the
  test-set ledger, so the committed verdict can be recomputed here, and a
  second look at the same test set can never adopt;
* the committed evaluation still describes THIS generator, THIS method and
  THESE settings.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_eval.py
"""
import hashlib
import json
import math
import os
import shutil
import tempfile

from amp_ai.core import artifact as ART
from amp_ai.core import ledger as LG
from amp_ai.core import purity as P
from amp_ai.core.robust import MAD_SCALE
from amp_ai.telemetry_anomaly import baseline as B
from amp_ai.telemetry_anomaly import build_eval as BE
from amp_ai.telemetry_anomaly import series as SR
from amp_ai.telemetry_anomaly import synthetic as S

BACKEND = os.path.dirname(os.path.abspath(__file__))
SYNTHETIC_PY = os.path.join(BACKEND, "amp_ai", "telemetry_anomaly", "synthetic.py")
US_PER_MINUTE = 60 * 1_000_000

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


# --------------------------------------------------------------------------- harness
def _reference_inputs(series, window):
    """The production path: raw readings sliced to the two windows and bucketed by series.build_buckets."""
    anchor = window.end_minute
    start = anchor - S.WINDOW_MINUTES
    first = max(0, anchor - SR.TOTAL_BUCKETS * SR.BUCKET_SECONDS // 60)
    base = [((anchor - m) * US_PER_MINUTE, n, v[m]) for n, v in series.signals.items()
            for m in range(first, start) if v[m] is not None]
    score = [((anchor - (start + i)) * US_PER_MINUTE, n, x) for n, vals in window.readings.items()
             for i, x in enumerate(vals) if x is not None]
    events = [((anchor - m) * US_PER_MINUTE, r) for m, r in S.state_transitions(series) if m < anchor]
    return (SR.build_buckets(base, events, None, first_k=SR.SCORE_BUCKETS, last_k=SR.TOTAL_BUCKETS - 1),
            SR.build_buckets(score, events, None, first_k=0, last_k=SR.SCORE_BUCKETS - 1),
            SR.integer_valued_signals(base))


def _rows(buckets):
    return [(b.k, b.state, dict(b.values)) for b in buckets]


def _small_series(seed=5, history_days=4):
    return S.generate_series(seed, "harness", 0, history_days=history_days,
                             windows_per_series=len(S.ANOMALY_TYPES) + 2,
                             positives=dict(enumerate(S.ANOMALY_TYPES)))


def section_harness_parity():
    print("\n[harness: every window is assembled exactly as production assembles it]")
    series = _small_series()
    fast = BE.SeriesBuckets(series)
    ok_base = ok_score = ok_int = ok_assess = True
    scored = 0
    for w in series.windows:
        ref_base, ref_score, ref_int = _reference_inputs(series, w)
        got = fast.window_inputs(w)
        ok_base &= _rows(got.baseline) == _rows(ref_base)
        ok_score &= _rows(got.score) == _rows(ref_score)
        ok_int &= got.integer_signals == ref_int
        a = B.assess(got.baseline, got.score, integer_signals=got.integer_signals)
        b = B.assess(ref_base, ref_score, integer_signals=ref_int)
        ok_assess &= a == b
        scored += a["status"] == "ok"
    check("baseline buckets (k, state, medians) equal series.build_buckets on the raw readings", ok_base)
    check("score buckets come from the scored (anomalous) copy, exactly as the reference builds them", ok_score)
    check("integer-valued signals equal series.integer_valued_signals on the baseline readings", ok_int)
    check("the model's result is identical on both assemblies", ok_assess)
    check("the parity windows include anomalous ones and are actually scored",
          scored >= 4 and sum(w.label for w in series.windows) == len(S.ANOMALY_TYPES), f"scored={scored}")

    print("\n[harness: the scored hour never enters its own baseline]")
    w = series.windows[0]
    anchor = w.end_minute
    before = fast.window_inputs(w)
    check("every baseline bucket lies in k = 12..4031",
          all(SR.SCORE_BUCKETS <= b.k < SR.TOTAL_BUCKETS for b in before.baseline) and before.baseline)
    wild = _small_series()
    for n in wild.signals:
        for m in range(anchor - S.WINDOW_MINUTES, anchor):
            if wild.signals[n][m] is not None:
                wild.signals[n][m] = 9999.0
    after = BE.SeriesBuckets(wild).window_inputs(w)
    check("overwriting the window's own stored hour leaves its baseline untouched",
          _rows(after.baseline) == _rows(before.baseline) and after.integer_signals == before.integer_signals)
    probe = _small_series()
    for n in probe.signals:
        for m in range(anchor - S.WINDOW_MINUTES - 5, anchor - S.WINDOW_MINUTES):
            probe.signals[n][m] = 9999.0
    moved = BE.SeriesBuckets(probe).window_inputs(w)
    check("... while overwriting the five minutes just before it does change the baseline (the probe can see)",
          _rows(moved.baseline) != _rows(before.baseline))


def section_baselines():
    print("\n[harness: the two baselines]")
    ranges = {"a": (0.0, 100.0), "t": (5.0, 80.0)}
    check("static range: readings inside the declared range exceed by 0",
          BE.static_range_exceedance({"a": [0.0, 50.0, 100.0], "t": [5.0, None, 80.0]}, ranges) == 0.0)
    check("static range: 25 above a 0-100 range is an exceedance of 0.25",
          BE.static_range_exceedance({"a": [50.0, 125.0], "t": [20.0]}, ranges) == 0.25)
    check("static range: below the range counts, normalised by the range width",
          abs(BE.static_range_exceedance({"t": [-10.0]}, ranges) - 15.0 / 75.0) < 1e-12)
    check("static range: the worst reading across signals is the window's score",
          abs(BE.static_range_exceedance({"a": [110.0], "t": [95.0]}, ranges) - 0.2) < 1e-12)
    check("static range: missing readings are ignored, an empty window scores 0",
          BE.static_range_exceedance({"a": [None, None]}, ranges) == 0.0 and BE.static_range_exceedance({}, ranges) == 0.0)

    values = {"s": [1.0, 2.0, 3.0, 4.0, 100.0], "c": [7.0] * 5}
    fitted = BE.mean_std_fitter(values, frozenset({"c"}))
    mean = 22.0
    sd = math.sqrt(sum((v - mean) ** 2 for v in values["s"]) / 5)
    check("mean/std baseline: location is the mean and scale the population standard deviation",
          abs(fitted["s"][0] - mean) < 1e-12 and abs(fitted["s"][1] - sd) < 1e-12, str(fitted["s"]))
    check("mean/std baseline: a constant integer sensor uses the same floor as the robust model (1.4826 * 1.0)",
          fitted["c"] == (7.0, MAD_SCALE * B.mad_floor(7.0, True)), str(fitted["c"]))
    robust = B.robust_fitter(values, frozenset({"c"}))
    check("... and the robust model's location ignores the outlier the mean follows",
          robust["s"][0] == 3.0 and fitted["s"][0] == 22.0)
    check("the mean/std fitter names itself", BE.mean_std_fitter.method_name == "mean_std")
    check("the gated baselines are the static range checks and mean/std",
          set(BE.GATED_BASELINES) == {BE.STATIC_RAW, BE.STATIC_MEDIAN, BE.MEAN_STD})
    check("the model method is not a baseline of itself", BE.MODEL_METHOD not in BE.GATED_BASELINES)


def section_alarms():
    print("\n[harness: alarm rules, recall and clean false-alarm rate]")
    check("model alarm: score >= 0.99 alarms, including exactly 0.99",
          BE.is_alarm(BE.MODEL_METHOD, 0.99) and not BE.is_alarm(BE.MODEL_METHOD, 0.9899999))
    check("a refused (None) score never alarms", not BE.is_alarm(BE.MODEL_METHOD, None))
    check("static alarm: any exceedance alarms, none does not",
          BE.is_alarm(BE.STATIC_RAW, 1e-9) and not BE.is_alarm(BE.STATIC_RAW, 0.0))
    try:
        BE.is_alarm("nope", 1.0)
        check("an unknown method is refused", False)
    except ValueError:
        check("an unknown method is refused", True)
    y = [1, 1, 0, 0, 0, 0]
    s = [0.995, 0.5, 0.99, 0.1, 0.2, 0.0]
    check("recall at the alarm = alarmed positives / positives", BE.recall_fn(BE.MODEL_METHOD)(y, s) == 0.5)
    check("clean false-alarm rate = alarmed negatives / negatives", BE.false_alarm_rate_fn(BE.MODEL_METHOD)(y, s) == 0.25)
    check("undefined without negatives / positives: None, never 0",
          BE.false_alarm_rate_fn(BE.MODEL_METHOD)([1, 1], [0.1, 0.2]) is None
          and BE.recall_fn(BE.MODEL_METHOD)([0, 0], [0.1, 0.2]) is None)


def _gate_metrics(lo=0.05, far=0.01, drop=None):
    paired = {name: {"estimate": 0.1, "lo": lo, "hi": 0.2} for name in BE.GATED_BASELINES if name != drop}
    return {"paired_pr_auc_vs": paired, "clean_false_alarm_rate": {"estimate": far, "lo": 0.0, "hi": 0.03}}


def section_gate():
    print("\n[gate: a pure function of the metrics and the ledger]")
    h = "a" * 64
    once = LG.record_test_evaluation({}, data_hash=h, test_set=BE.TEST_SET)
    twice = LG.record_test_evaluation(once, data_hash=h, test_set=BE.TEST_SET)
    ok = BE.decide_adoption(_gate_metrics(), once, data_hash=h)
    check("every criterion met on the first test run: adopted, no reasons",
          ok["adopted"] is True and ok["reasons"] == [] and ok["test_set_reused"] is False, str(ok))
    for label, metrics, ledger in [
        ("a PR-AUC lower bound of exactly 0 against a baseline", _gate_metrics(lo=0.0), once),
        ("a withheld (None) lower bound", _gate_metrics(lo=None), once),
        ("a gated baseline missing from the comparison", _gate_metrics(drop=BE.MEAN_STD), once),
        ("a clean false-alarm rate just over 2%", _gate_metrics(far=0.0200001), once),
        ("an undefined clean false-alarm rate", _gate_metrics(far=None), once),
        ("a second run on the same test set", _gate_metrics(), twice),
        ("a test run that was never recorded", _gate_metrics(), {}),
        ("a run recorded for different data", _gate_metrics(),
         LG.record_test_evaluation({}, data_hash="b" * 64, test_set=BE.TEST_SET)),
    ]:
        d = BE.decide_adoption(metrics, ledger, data_hash=h)
        check(f"not adopted: {label} (with a reason)", d["adopted"] is False and d["reasons"], str(d))
    edge = BE.decide_adoption(_gate_metrics(far=0.02), once, data_hash=h)
    check("a clean false-alarm rate of exactly 2% passes", edge["adopted"] is True, str(edge))
    reused = BE.decide_adoption(_gate_metrics(), twice, data_hash=h)
    check("a reused test set is flagged test_set_reused with its run count",
          reused["test_set_reused"] is True and reused["test_set_run_count"] == 2)
    per_baseline = BE.decide_adoption(_gate_metrics(lo=0.0), once, data_hash=h)
    check("the reasons name every baseline the model did not beat",
          all(any(name in r for r in per_baseline["reasons"]) for name in BE.GATED_BASELINES), str(per_baseline["reasons"]))
    check("the gate constants are the documented ones",
          BE.ALARM_SCORE == 0.99 and BE.MAX_CLEAN_FALSE_ALARM_RATE == 0.02)


def section_hashes():
    print("\n[provenance: generator hash and data hash]")
    with open(SYNTHETIC_PY, "rb") as fh:
        expected = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
    check("generator_sha256 is the SHA-256 of synthetic.py with line endings normalised",
          BE.generator_sha256() == expected)
    root = tempfile.mkdtemp(prefix="amp_anomaly_hash_")
    try:
        lf, crlf, edited = (os.path.join(root, n) for n in ("lf.py", "crlf.py", "edited.py"))
        for path, data in ((lf, b"x = 1\ny = 2\n"), (crlf, b"x = 1\r\ny = 2\r\n"), (edited, b"x = 1\ny = 3\n")):
            with open(path, "wb") as fh:
                fh.write(data)
        check("a CRLF checkout hashes the same as LF", BE.generator_sha256(lf) == BE.generator_sha256(crlf))
        check("a one-character edit changes the hash", BE.generator_sha256(lf) != BE.generator_sha256(edited))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    base = dict(generator_sha256="c" * 64, seed=1, split="test", variant="main", config=dict(BE.SMALL_CONFIG))
    h = BE.data_hash(**base)
    check("data_hash is deterministic 64-hex", h == BE.data_hash(**base) and len(h) == 64)
    for key, value in (("generator_sha256", "d" * 64), ("seed", 2), ("split", "validation"), ("variant", "heavy_tails"),
                       ("config", dict(BE.SMALL_CONFIG, n_series=BE.SMALL_CONFIG["n_series"] + 1))):
        check(f"a different {key} is different test data (new hash)", BE.data_hash(**dict(base, **{key: value})) != h)


SMALL_BUILD = dict(created_at="2026-09-17T00:00:00+00:00", seed=7, split="smoke", config=BE.SMALL_CONFIG,
                   variants=("heavy_tails",), misspecification_config=BE.SMALL_CONFIG,
                   validation_config=BE.SMALL_CONFIG, n_boot=40, timing=False)


def section_small_build():
    print("\n[build: a small evaluation end to end]")
    first = BE.build(prior_ledger={}, **SMALL_BUILD)
    second = BE.build(prior_ledger={}, **SMALL_BUILD)
    check("two builds with the same seed and created_at are identical (payload hash)",
          ART.payload_hash(first) == ART.payload_hash(second))
    try:
        ART.validate_artifact(first, require_sha256=False)
        check("the evaluation is a structurally valid artifact", True)
    except (ValueError, TypeError) as exc:
        check("the evaluation is a structurally valid artifact", False, str(exc))
    check("model_type, provenance and caveat",
          first["model_type"] == BE.MODEL_TYPE and first["generator_sha256"] == BE.generator_sha256()
          and "synthetic:amp_ai.telemetry_anomaly.synthetic@v1 seed=7" in first["training_data_source"]
          and first["caveat"] == BE.CAVEAT, str({k: first[k] for k in ("model_type", "training_data_source")}))
    check("parameters are exactly the method the service runs", first["parameters"] == B.method_parameters())
    main_hash = BE.data_hash(generator_sha256=BE.generator_sha256(), seed=7, split="smoke", variant="main",
                             config=BE.SMALL_CONFIG)
    variant_hash = BE.data_hash(generator_sha256=BE.generator_sha256(), seed=7, split="smoke", variant="heavy_tails",
                                config=BE.SMALL_CONFIG)
    check("the gate records which test data it judged", first["gate"]["data_hash"] == main_hash)
    check("the ledger records one run of the main test set and one of each misspecification variant",
          LG.run_count(first["eval_ledger"], main_hash, BE.TEST_SET) == 1
          and LG.run_count(first["eval_ledger"], variant_hash, BE.TEST_SET) == 1)
    check("the committed verdict is the gate recomputed from its own metrics and ledger",
          first["adoption"] == BE.decide_adoption(first["metrics"], first["eval_ledger"], data_hash=main_hash))
    total = BE.SMALL_CONFIG["n_series"] * BE.SMALL_CONFIG["windows_per_series"]
    m = first["metrics"]
    check("every window is counted, positives as the generator placed them",
          m["n_windows"] == total and m["n_positive"] == math.floor(BE.SMALL_CONFIG["anomaly_fraction"] * total + 0.5),
          f"{m['n_windows']} {m['n_positive']}")
    check("the model and every baseline are judged on the same windows",
          all(first["baseline_metrics"][b]["n_windows"] == total for b in BE.GATED_BASELINES)
          and set(m["paired_pr_auc_vs"]) == set(BE.GATED_BASELINES))
    check("ablations, misspecification and validation are reported",
          set(first["ablations"]) == set(BE.ABLATIONS) and set(first["misspecification"]) == {"heavy_tails"}
          and first["validation"]["n_windows"] == total)
    check("no build-time field when timing is off (so the payload is reproducible)", "timing" not in first)

    again = BE.build(prior_ledger=first["eval_ledger"], **SMALL_BUILD)
    check("building again on the same test set counts a second run and cannot adopt",
          LG.run_count(again["eval_ledger"], main_hash, BE.TEST_SET) == 2 and again["adoption"]["adopted"] is False
          and again["adoption"]["test_set_reused"] is True, str(again["adoption"]))
    check("... and the metrics themselves are unchanged (only the verdict and ledger move)",
          again["metrics"] == first["metrics"])

    root = tempfile.mkdtemp(prefix="amp_anomaly_build_")
    try:
        out = os.path.join(root, "small.eval.json")
        before = _read(BE.EVAL_PATH)
        code = BE.main(["--small", "--out", BE.EVAL_PATH, "--created-at", SMALL_BUILD["created_at"]])
        check("the --small smoke build refuses to overwrite the committed evaluation",
              code != 0 and _read(BE.EVAL_PATH) == before)
        code = BE.main(["--small", "--out", out, "--created-at", SMALL_BUILD["created_at"]])
        try:
            loaded = ART.load_artifact(out, expected_model_type=BE.MODEL_TYPE, expected_sha256=_embedded_sha(out))
        except (OSError, ValueError, ART.ArtifactIntegrityError) as exc:
            loaded = {"error": str(exc)}
        check("the --small smoke build writes a loadable evaluation of the SMOKE split where it is told to",
              code == 0 and loaded.get("evaluation_config", {}).get("split") == BE.SMOKE_SPLIT
              and loaded.get("created_at") == SMALL_BUILD["created_at"], f"code={code} {str(loaded)[:200]}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _embedded_sha(path):
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))["sha256"]


def section_committed_evaluation():
    print("\n[the committed evaluation describes this generator, this method and these settings]")
    from amp_ai.telemetry_anomaly import service   # imports models; kept local to this section
    check("the harness and the service agree on model type and caveat",
          BE.MODEL_TYPE == service.MODEL_TYPE and BE.CAVEAT == service.CAVEAT
          and os.path.abspath(BE.EVAL_PATH) == os.path.abspath(service.EVAL_ARTIFACT_PATH))
    try:
        art = ART.load_artifact(BE.EVAL_PATH, expected_model_type=BE.MODEL_TYPE, expected_sha256=service.EVAL_SHA256)
    except (ART.ArtifactIntegrityError, ValueError) as exc:
        check("the committed evaluation loads against the hash pinned in service.py", False,
              getattr(exc, "reason", str(exc)))
        return
    check("the committed evaluation loads against the hash pinned in service.py", True)
    check("it was produced from THIS generator (source hash matches)", art["generator_sha256"] == BE.generator_sha256())
    cfg = art["evaluation_config"]
    check("it used the harness's seed, split, sizes, variants and bootstrap settings",
          cfg == BE.evaluation_config(seed=BE.SEED, split=BE.TEST_SET, config=BE.MAIN_CONFIG,
                                      variants=BE.MISSPECIFICATION_VARIANTS,
                                      misspecification_config=BE.MISSPECIFICATION_CONFIG,
                                      validation_config=BE.VALIDATION_CONFIG, n_boot=BE.N_BOOT), str(cfg))
    main_hash = BE.data_hash(generator_sha256=art["generator_sha256"], seed=art["seed"], split=BE.TEST_SET,
                             variant="main", config=BE.MAIN_CONFIG)
    check("its gate judged the test data those settings produce", art["gate"]["data_hash"] == main_hash)
    check("its parameters are the method this code runs", art["parameters"] == B.method_parameters())
    decision = BE.decide_adoption(art["metrics"], art["eval_ledger"], data_hash=main_hash)
    check("its adoption verdict is the gate recomputed from its own metrics and ledger",
          art["adoption"] == decision, f"committed={art['adoption']} recomputed={decision}")
    runs = LG.run_count(art["eval_ledger"], main_hash, BE.TEST_SET)
    check("the ledger records at least one run of this test set, and reuse is flagged", runs >= 1
          and art["adoption"]["test_set_reused"] == (runs > 1), f"runs={runs}")
    total = BE.MAIN_CONFIG["n_series"] * BE.MAIN_CONFIG["windows_per_series"]
    m = art["metrics"]
    check("every generated test window was scored and counted",
          m["n_windows"] == total and m["n_positive"] == math.floor(BE.MAIN_CONFIG["anomaly_fraction"] * total + 0.5),
          f"{m['n_windows']} {m['n_positive']}")
    check("provenance names the synthetic generator and seed; consent basis and caveat present",
          f"synthetic:amp_ai.telemetry_anomaly.synthetic@v1 seed={BE.SEED}" in art["training_data_source"]
          and art["consent_basis"].strip() and art["caveat"] == service.CAVEAT)
    card = service.evaluation_card()
    check("the service card reports the committed verdict (experimental unless adopted)",
          card["available"] and card["adopted"] == art["adoption"]["adopted"]
          and card["experimental"] == (not art["adoption"]["adopted"]), str(card))


SECTIONS = [section_determinism, section_shape, section_physics, section_windows_and_labels,
            section_variants, section_independence, section_harness_parity, section_baselines, section_alarms,
            section_gate, section_hashes, section_small_build, section_committed_evaluation]


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
