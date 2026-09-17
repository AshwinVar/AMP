"""Held-out evaluation of the telemetry anomaly scorer against two baselines, and its adoption gate.

Standard library plus ``amp_ai.core`` only. Never opens a database, never reads
customer data: every machine here comes from ``synthetic`` (committed, with
tests, BEFORE this file; its source hash is recorded and keys the ledger).

RUN (from backend/)
-------------------
  python -m amp_ai.telemetry_anomaly.build_eval
      The gated evaluation: the TEST split (gated), the misspecification
      variants (reported, not gated) and the VALIDATION split (development,
      reported). Always writes artifacts/telemetry_anomaly_v1.eval.json,
      carrying that file's ledger forward, and prints the SHA-256 to pin as
      ``EVAL_SHA256`` in service.py. Test metrics are printed only after the
      run is recorded and written.
  python -m amp_ai.telemetry_anomaly.build_eval --validation-only
      Development: prints validation-split results. Writes nothing and never
      generates the test split.
  python -m amp_ai.telemetry_anomaly.build_eval --small --out PATH
      A seconds-long smoke build on its own split, with an empty ledger.
      Refuses to write the committed evaluation.

HOW A WINDOW IS SCORED
----------------------
Exactly as production scores it. For a window ending at minute A, the baseline
is the machine's STORED readings in [A - 14 d, A - 1 h) and the score window is
the SCORED copy of [A - 1 h, A) - the only place an anomaly is injected - so a
window is never part of its own baseline. Machine state comes from the
generator's running/idle transitions, as MachineEvent rows would give it.
``SeriesBuckets`` computes each machine's 5-minute medians once and shifts them
per window; test_amp_ai_anomaly_eval.py pins that this equals
``series.build_buckets`` on the raw readings, window by window.

METHODS (every one on the same windows)
---------------------------------------
  robust                       THE MODEL: ``baseline.assess`` with its defaults,
                               i.e. exactly what ``service.score_machine`` runs.
  mean_std                     baseline (b): identical buckets, states,
                               fit/calibration split, Mahalanobis term, window
                               statistic and calibration; only location = mean and
                               scale = standard deviation (floored as the model's
                               scale is: 1.4826 * the same MAD floor).
  static_range                 baseline (a): the worst excursion of any RAW reading
                               outside the generator's declared ranges, as a share of
                               the range width; alarms on any excursion - what an
                               engineer types into a PLC alarm.
  static_range_bucket_median   the same check on the 5-minute medians (a debounced
                               static alarm). Also gated: the model has to beat both.
  ablations (reported only)    robust without the Mahalanobis term; robust with the
                               other window statistic.
A window the scorer refuses (insufficient_history) scores 0 and never alarms;
``n_refused`` counts them.

METRICS
-------
PR-AUC and ROC-AUC of the window score against the injected label; recall and
clean false-alarm rate at the alarm (calibrated methods: score >= 0.99; static:
any excursion); per anomaly type (that type's windows plus every clean window).
Intervals are 95% percentile bootstraps that resample whole MACHINES, since
windows of one machine share a baseline. The paired PR-AUC difference against
each baseline uses the same resamples for both scores.

GATE (all must hold, else ``adopted: false`` with reasons)
----------------------------------------------------------
  1. the paired PR-AUC difference against EVERY gated baseline has a 95% lower
     bound > 0 (a withheld bound fails);
  2. the model's clean false-alarm rate at 0.99 is <= 2%;
  3. this is the FIRST recorded evaluation of this test data (ledger).
Not adopted means ``service`` labels every score ``experimental``, never an alert.
Adopted or not, it was evaluated on synthetic machines only.

LEDGER
------
Test data is identified by ``data_hash`` = SHA-256 of (generator name, generator
source hash, seed, split, variant, sizes). Every build records one run per test
dataset BEFORE scoring it. Re-running on the same data - for any reason - makes
the count 2 and the gate fails; only fresh test data (a new seed, sizes or
generator) starts a new count.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core import metrics as M
from ..core.artifact import SCHEMA_VERSION, ArtifactIntegrityError, canonical_json, load_artifact, save_artifact, \
    validate_artifact
from ..core.ledger import record_test_evaluation, run_count
from ..core.robust import MAD_SCALE
from . import baseline as B
from . import series as SR
from . import synthetic as SY

__all__ = [
    "MODEL_TYPE", "MODEL_NAME", "VERSION", "SEED", "TEST_SET", "TEST_SPLIT", "VALIDATION_SPLIT", "SMOKE_SPLIT",
    "EVAL_PATH", "CAVEAT", "MAIN_CONFIG", "MISSPECIFICATION_VARIANTS", "MISSPECIFICATION_CONFIG", "VALIDATION_CONFIG",
    "SMALL_CONFIG", "N_BOOT", "SMALL_N_BOOT", "ALARM_SCORE", "MAX_CLEAN_FALSE_ALARM_RATE", "MODEL_METHOD", "MEAN_STD",
    "STATIC_RAW", "STATIC_MEDIAN", "GATED_BASELINES", "ABLATIONS", "METHODS", "WindowInputs", "SeriesBuckets",
    "generator_sha256", "data_hash", "static_range_exceedance", "mean_std_fitter", "is_alarm", "recall_fn",
    "false_alarm_rate_fn", "score_series", "summarise", "evaluate", "decide_adoption", "evaluation_config",
    "committed_ledger", "build", "main",
]

MODEL_TYPE = "telemetry_anomaly"
MODEL_NAME = "telemetry_anomaly"
VERSION = "v1"
SEED = 20260917
TEST_SET = "test"
TEST_SPLIT = "test"
VALIDATION_SPLIT = "validation"
SMOKE_SPLIT = "smoke"
EVAL_PATH = str(Path(__file__).resolve().parent.parent / "artifacts" / "telemetry_anomaly_v1.eval.json")
CAVEAT = "Evaluated on synthetic machines only; not evidence of accuracy on real plants."

# 200 machines x 18 one-hour windows = 3600 windows, 5% (180) anomalous: 30 of each type.
# History of 7-14 days: machines with a full 14-day baseline and machines still filling it.
MAIN_CONFIG = {"n_series": 200, "windows_per_series": 18, "min_history_days": 7, "max_history_days": 14,
               "anomaly_fraction": 0.05}
MISSPECIFICATION_VARIANTS = ("heavy_tails", "regime_switching")
MISSPECIFICATION_CONFIG = {"n_series": 100, "windows_per_series": 18, "min_history_days": 7, "max_history_days": 14,
                           "anomaly_fraction": 0.05}
VALIDATION_CONFIG = {"n_series": 100, "windows_per_series": 18, "min_history_days": 7, "max_history_days": 14,
                     "anomaly_fraction": 0.05}
SMALL_CONFIG = {"n_series": 6, "windows_per_series": 3, "min_history_days": 5, "max_history_days": 6,
                "anomaly_fraction": 0.5}
N_BOOT = 1000
SMALL_N_BOOT = 40
ALPHA = 0.05

ALARM_SCORE = 0.99
MAX_CLEAN_FALSE_ALARM_RATE = 0.02

# What was decided on DEVELOPMENT data (the validation split and a separate "diagnostics" split of
# the same generator), before the test split was scored for the first time. Recorded so a reader
# knows which choices were informed by looking at data, and which data that was.
DEVELOPMENT_NOTES = (
    "First validation run (bucket state = state at the bucket's start): the robust scorer ranked windows barely "
    "better than chance and below the mean/std baseline; stop/start buckets scored against the wrong state's "
    "baseline dominated every calibration sample.",
    "Change adopted on development data: a bucket whose running state changes inside it is 'Transition', never "
    "fitted and never scored (series.bucket_states). It raised the robust scorer's ranking and recall on the "
    "diagnostics split.",
    "Tried and rejected on development data: calibrating only against hours with the same dominant machine state "
    "(raised clean false alarms and refused more windows); fitting on the newest 70% and calibrating on the oldest "
    "30% (no clear difference, so the planned oldest-70%-fit order was kept).",
    "Not changed after seeing development data: the robust median/MAD estimator, the window statistic, the "
    "Mahalanobis term, the MAD floors and every minimum-data threshold; the mean/std baseline still ranked better "
    "than the robust scorer on development data, and the scorer was not tuned further towards this generator.",
)

US_PER_MINUTE = 60 * 1_000_000
_MINUTES_PER_BUCKET = SR.BUCKET_SECONDS // 60


# --------------------------------------------------------------------------- baselines
def mean_std_fitter(values_by_signal, integer_signals):
    """{signal: (mean, max(population std, 1.4826 * baseline.mad_floor(mean)))} - the non-robust baseline."""
    out = {}
    for name in sorted(values_by_signal):
        values = [float(v) for v in values_by_signal[name]]
        if not values:
            raise ValueError(f"signal {name!r}: no values")
        mean = math.fsum(values) / len(values)
        sd = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / len(values))
        out[name] = (mean, max(sd, MAD_SCALE * B.mad_floor(mean, name in integer_signals)))
    return out


mean_std_fitter.method_name = "mean_std"


def static_range_exceedance(values_by_signal, ranges) -> float:
    """The worst excursion of any reading outside its declared [lo, hi], as a share of hi - lo (0 if none)."""
    worst = 0.0
    for name in sorted(values_by_signal):
        lo, hi = ranges[name]
        width = hi - lo
        if not width > 0:
            raise ValueError(f"signal {name!r}: declared range must have hi > lo")
        for v in values_by_signal[name]:
            if v is None:
                continue
            excursion = max(lo - v, v - hi, 0.0) / width
            if excursion > worst:
                worst = excursion
    return worst


# --------------------------------------------------------------------------- methods
def _other_statistic():
    return next(a for a in B.AGGREGATES if a != B.WINDOW_AGGREGATE)


MODEL_METHOD = "robust"
MEAN_STD = "mean_std"
STATIC_RAW = "static_range"
STATIC_MEDIAN = "static_range_bucket_median"
GATED_BASELINES = (STATIC_RAW, STATIC_MEDIAN, MEAN_STD)
ABLATION_NO_MAHALANOBIS = "robust_no_mahalanobis"
ABLATION_OTHER_STATISTIC = f"robust_{_other_statistic()}_statistic"
ABLATIONS = (ABLATION_NO_MAHALANOBIS, ABLATION_OTHER_STATISTIC)
METHODS = (MODEL_METHOD,) + GATED_BASELINES + ABLATIONS

_ASSESS_OPTIONS = {
    MODEL_METHOD: {},                                   # exactly service.score_machine's call
    MEAN_STD: {"fitter": mean_std_fitter},
    ABLATION_NO_MAHALANOBIS: {"mahalanobis": False},
    ABLATION_OTHER_STATISTIC: {"aggregate": _other_statistic()},
}
_STATIC_METHODS = (STATIC_RAW, STATIC_MEDIAN)
_ALARM_RULES = dict(
    {m: f"score >= {ALARM_SCORE} (rarer than {ALARM_SCORE:.0%} of this machine's recent normal hours)"
     for m in _ASSESS_OPTIONS},
    **{m: "any reading outside the declared static range" for m in _STATIC_METHODS})


def is_alarm(method, score) -> bool:
    if method in _ASSESS_OPTIONS:
        return score is not None and score >= ALARM_SCORE
    if method in _STATIC_METHODS:
        return score is not None and score > 0.0
    raise ValueError(f"unknown method {method!r}")


def recall_fn(method):
    """metric(y, s): alarmed positives / positives, None without positives."""
    is_alarm(method, None)   # refuse an unknown method now, not inside a bootstrap

    def recall(y, s):
        hits = [is_alarm(method, v) for t, v in zip(y, s) if t]
        return sum(hits) / len(hits) if hits else None
    return recall


def false_alarm_rate_fn(method):
    """metric(y, s): alarmed clean windows / clean windows, None without clean windows."""
    is_alarm(method, None)

    def false_alarm_rate(y, s):
        hits = [is_alarm(method, v) for t, v in zip(y, s) if not t]
        return sum(hits) / len(hits) if hits else None
    return false_alarm_rate


# --------------------------------------------------------------------------- provenance
def generator_sha256(path=None) -> str:
    """SHA-256 of the generator source with CRLF normalised to LF (a Windows checkout hashes the same)."""
    path = SY.__file__ if path is None else path
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def data_hash(*, generator_sha256, seed, split, variant, config) -> str:
    """Identity of one synthetic test dataset: what the ledger counts evaluations of."""
    return hashlib.sha256(canonical_json({
        "generator": f"{SY.GENERATOR_NAME}@{SY.GENERATOR_VERSION}", "generator_sha256": generator_sha256,
        "seed": seed, "split": split, "variant": variant, "config": dict(config),
    })).hexdigest()


# --------------------------------------------------------------------------- assembling windows
@dataclass(frozen=True)
class WindowInputs:
    baseline: list
    score: list
    integer_signals: frozenset


class SeriesBuckets:
    """A synthetic machine's stored history, bucketed once; each window gets its own baseline and score buckets."""

    def __init__(self, series):
        if series.minutes % _MINUTES_PER_BUCKET:
            raise ValueError("a series must end on a bucket boundary")
        self.end = series.minutes
        readings = [((self.end - m) * US_PER_MINUTE, name, v) for name, values in series.signals.items()
                    for m, v in enumerate(values) if v is not None]
        self.medians = SR.bucket_medians(readings)
        grouped = {}
        for reading in readings:
            grouped.setdefault(SR.bucket_index(reading[0]), []).append(reading)
        self.present, self.fractional = {}, {}
        for k, rows in grouped.items():
            names = frozenset(name for _, name, _ in rows)
            self.present[k] = names
            self.fractional[k] = names - SR.integer_valued_signals(rows)
        self.transitions = SY.state_transitions(series)
        events = [((self.end - m) * US_PER_MINUTE, running) for m, running in self.transitions]
        self.states = SR.bucket_states(sorted(self.medians), events, None)

    def window_inputs(self, window) -> WindowInputs:
        anchor = window.end_minute
        if anchor % _MINUTES_PER_BUCKET or not SY.WINDOW_MINUTES <= anchor <= self.end:
            raise ValueError(f"window ending at minute {anchor} is not on the bucket grid of this series")
        shift = (self.end - anchor) // _MINUTES_PER_BUCKET
        baseline, seen, fractional = [], set(), set()
        for k in range(SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS):      # never 0..11: the scored hour is not baseline
            g = k + shift
            values = self.medians.get(g)
            if values is None:
                continue
            baseline.append(SR.Bucket(k, self.states[g], dict(values)))
            seen |= self.present[g]
            fractional |= self.fractional[g]
        start = anchor - SY.WINDOW_MINUTES
        score_readings = [((anchor - (start + i)) * US_PER_MINUTE, name, v)
                          for name, values in window.readings.items() for i, v in enumerate(values) if v is not None]
        events = [((anchor - m) * US_PER_MINUTE, running) for m, running in self.transitions if m < anchor]
        score = SR.build_buckets(score_readings, events, None, first_k=0, last_k=SR.SCORE_BUCKETS - 1)
        return WindowInputs(baseline, score, frozenset(seen - fractional))


def score_series(series, methods=METHODS) -> list:
    """One row per evaluation window: its label, anomaly type and every method's score (None = refused)."""
    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        raise ValueError(f"unknown methods {unknown}")
    buckets = SeriesBuckets(series)
    rows = []
    for window in series.windows:
        inputs = buckets.window_inputs(window)
        scores = {}
        for method in methods:
            if method in _ASSESS_OPTIONS:
                result = B.assess(inputs.baseline, inputs.score, integer_signals=inputs.integer_signals,
                                  **_ASSESS_OPTIONS[method])
                scores[method] = result["score"] if result["status"] == B.STATUS_OK else None
            elif method == STATIC_RAW:
                scores[method] = static_range_exceedance(window.readings, series.declared_ranges)
            else:
                medians = {}
                for bucket in inputs.score:
                    for name, value in bucket.values.items():
                        medians.setdefault(name, []).append(value)
                scores[method] = static_range_exceedance(medians, series.declared_ranges)
        rows.append({"series": series.series_id, "label": window.label, "anomaly_type": window.anomaly_type,
                     "scores": scores})
    return rows


# --------------------------------------------------------------------------- metrics
def _method_metrics(method, y, scores, groups, types, *, seed, n_boot):
    filled = [0.0 if s is None else s for s in scores]
    boot = dict(groups=groups, n=n_boot, seed=seed, alpha=ALPHA)
    out = {
        "alarm_rule": _ALARM_RULES[method],
        "n_windows": len(y),
        "n_positive": sum(y),
        "n_series": len(set(groups)),
        "n_refused": sum(s is None for s in scores),
        "pr_auc": M.bootstrap_ci(M.pr_auc, y, filled, **boot),
        "roc_auc": M.bootstrap_ci(M.roc_auc, y, filled, **boot),
        "recall_at_alarm": M.bootstrap_ci(recall_fn(method), y, filled, **boot),
        "clean_false_alarm_rate": M.bootstrap_ci(false_alarm_rate_fn(method), y, filled, **boot),
        "per_anomaly_type": {},
    }
    clean = [i for i, label in enumerate(y) if not label]
    for kind in SY.ANOMALY_TYPES:
        idx = sorted(clean + [i for i, t in enumerate(types) if t == kind])
        yy = [y[i] for i in idx]
        ss = [filled[i] for i in idx]
        out["per_anomaly_type"][kind] = {
            "n_positive": sum(yy), "recall_at_alarm": recall_fn(method)(yy, ss),
            "pr_auc": M.pr_auc(yy, ss), "roc_auc": M.roc_auc(yy, ss),
        }
    return out


def summarise(rows, *, methods, seed, n_boot) -> dict:
    if not rows:
        raise ValueError("no windows to summarise")
    y = [r["label"] for r in rows]
    groups = [r["series"] for r in rows]
    types = [r["anomaly_type"] for r in rows]
    per_method = {m: _method_metrics(m, y, [r["scores"][m] for r in rows], groups, types, seed=seed, n_boot=n_boot)
                  for m in methods}
    paired_pr, paired_roc = {}, {}
    if MODEL_METHOD in methods:
        model = [0.0 if r["scores"][MODEL_METHOD] is None else r["scores"][MODEL_METHOD] for r in rows]
        for name in GATED_BASELINES:
            if name not in methods:
                continue
            other = [0.0 if r["scores"][name] is None else r["scores"][name] for r in rows]
            paired_pr[name] = M.paired_bootstrap_diff(M.pr_auc, y, model, other, groups, n=n_boot, seed=seed,
                                                      alpha=ALPHA)
            paired_roc[name] = M.paired_bootstrap_diff(M.roc_auc, y, model, other, groups, n=n_boot, seed=seed,
                                                       alpha=ALPHA)
    return {"n_windows": len(rows), "n_positive": sum(y), "n_series": len(set(groups)), "per_method": per_method,
            "paired_pr_auc_vs": paired_pr, "paired_roc_auc_vs": paired_roc}


def evaluate(*, seed, split, variant, config, n_boot, methods=METHODS, progress=None) -> dict:
    rows = []
    for i, series in enumerate(SY.generate_dataset(seed, split, variant=variant, **config)):
        rows.extend(score_series(series, methods))
        if progress is not None:
            progress(f"{split}/{variant}: machine {i + 1}/{config['n_series']}")
    return summarise(rows, methods=methods, seed=seed, n_boot=n_boot)


# --------------------------------------------------------------------------- gate
def _number(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def decide_adoption(metrics, ledger, *, data_hash, test_set=TEST_SET) -> dict:
    """The adoption verdict: a pure function of the model's test metrics and the ledger."""
    reasons, checks = [], {}
    paired = metrics.get("paired_pr_auc_vs") or {}
    for name in GATED_BASELINES:
        lo = _number((paired.get(name) or {}).get("lo"))
        passed = lo is not None and lo > 0.0
        checks[f"paired_pr_auc_lower_bound_above_zero_vs_{name}"] = passed
        if not passed:
            shown = "withheld" if lo is None else f"{lo:.4f}"
            reasons.append(f"PR-AUC is not reliably better than the {name} baseline: the 95% lower bound of the "
                           f"paired difference is {shown}, and adoption needs it above 0")
    far = _number((metrics.get("clean_false_alarm_rate") or {}).get("estimate"))
    passed = far is not None and far <= MAX_CLEAN_FALSE_ALARM_RATE
    checks["clean_false_alarm_rate_at_most_limit"] = passed
    if not passed:
        shown = "undefined" if far is None else f"{far:.4f}"
        reasons.append(f"clean false-alarm rate at score >= {ALARM_SCORE} is {shown}; adoption needs at most "
                       f"{MAX_CLEAN_FALSE_ALARM_RATE}")
    count = run_count(ledger, data_hash, test_set)
    checks["first_evaluation_of_this_test_data"] = count == 1
    if count == 0:
        reasons.append("this evaluation is not recorded in the ledger for this test data")
    elif count > 1:
        reasons.append(f"this test data has been evaluated {count} times; only the first evaluation may adopt, "
                       "so a fresh test set (new seed, sizes or generator) is needed")
    return {"adopted": not reasons, "reasons": reasons, "checks": checks, "test_set_run_count": count,
            "test_set_reused": count > 1}


# --------------------------------------------------------------------------- build
def evaluation_config(*, seed, split, config, variants, misspecification_config, validation_config, n_boot) -> dict:
    return {
        "seed": seed, "split": split, "config": dict(config),
        "misspecification_variants": list(variants), "misspecification_config": dict(misspecification_config),
        "validation_split": VALIDATION_SPLIT, "validation_config": dict(validation_config),
        "window_minutes": SY.WINDOW_MINUTES, "n_boot": n_boot, "alpha": ALPHA,
        "bootstrap": "percentile; resamples whole machines (series)",
        "alarm_score": ALARM_SCORE, "max_clean_false_alarm_rate": MAX_CLEAN_FALSE_ALARM_RATE,
        "model_method": MODEL_METHOD, "gated_baselines": list(GATED_BASELINES), "ablations": list(ABLATIONS),
        "refused_windows": "score 0, never alarm",
    }


def _check_created_at(created_at):
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("created_at must be an ISO-8601 string")
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def build(*, created_at, prior_ledger, seed=SEED, split=TEST_SPLIT, config=MAIN_CONFIG,
          variants=MISSPECIFICATION_VARIANTS, misspecification_config=MISSPECIFICATION_CONFIG,
          validation_config=VALIDATION_CONFIG, n_boot=N_BOOT, timing=True, progress=None) -> dict:
    """The evaluation artifact (without its sha256; ``save_artifact`` adds it)."""
    _check_created_at(created_at)
    if split == VALIDATION_SPLIT:
        raise ValueError("the gated split cannot be the validation split")
    started = time.perf_counter()
    gen_sha = generator_sha256()

    # record every test evaluation BEFORE any test window is scored
    main_hash = data_hash(generator_sha256=gen_sha, seed=seed, split=split, variant="main", config=config)
    ledger = record_test_evaluation(prior_ledger, data_hash=main_hash, test_set=TEST_SET)
    variant_hashes = {}
    for variant in variants:
        variant_hashes[variant] = data_hash(generator_sha256=gen_sha, seed=seed, split=split, variant=variant,
                                            config=misspecification_config)
        ledger = record_test_evaluation(ledger, data_hash=variant_hashes[variant], test_set=TEST_SET)

    main = evaluate(seed=seed, split=split, variant="main", config=config, n_boot=n_boot, methods=METHODS,
                    progress=progress)
    misspecification = {}
    for variant in variants:
        result = evaluate(seed=seed, split=split, variant=variant, config=misspecification_config, n_boot=n_boot,
                          methods=(MODEL_METHOD,) + GATED_BASELINES, progress=progress)
        misspecification[variant] = dict(result, data_hash=variant_hashes[variant],
                                         test_set_run_count=run_count(ledger, variant_hashes[variant], TEST_SET),
                                         note="reported, never gated")
    validation = evaluate(seed=seed, split=VALIDATION_SPLIT, variant="main", config=validation_config,
                          n_boot=n_boot, methods=METHODS, progress=progress)

    metrics = dict(main["per_method"][MODEL_METHOD], method=MODEL_METHOD, split=split, variant="main",
                   paired_pr_auc_vs=main["paired_pr_auc_vs"], paired_roc_auc_vs=main["paired_roc_auc_vs"])
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "model_type": MODEL_TYPE,
        "model_name": MODEL_NAME,
        "version": VERSION,
        "created_at": created_at,
        "training_data_source": (
            f"none: nothing is trained ahead of time; each request fits a baseline from that machine's own last "
            f"{SR.BASELINE_DAYS} days of telemetry, only with the tenant's telemetry_baseline consent, and discards "
            f"it. Evaluation data: synthetic:{SY.GENERATOR_NAME}@{SY.GENERATOR_VERSION} seed={seed}"),
        "consent_basis": (
            "evaluation: none required, synthetic machines only, no customer data used; inference: the tenant's "
            "explicit, revocable telemetry_baseline learning consent, checked on every request before any "
            "telemetry is read"),
        "features": {
            "inputs": "every numeric telemetry signal stored for the machine: iot_telemetry rows (iot:<name>) and "
                      "industrial_signals rows with quality Good (plc:<name>)",
            "bucket": f"{SR.BUCKET_SECONDS // 60}-minute median per signal",
            # OUT OF DATE (review round 1): the method also has a Transition state for a bucket whose state
            # changes inside it (series.bucket_states, parameters.states). Left unchanged so the committed, pinned
            # v1 evaluation still reproduces byte for byte; the model card says so. Correct it in the next
            # evaluation build (which needs a fresh test set anyway).
            "state": "machine state at each bucket's start from MachineEvent: Running / NotRunning / Unknown",
            "score_window": "[now - 1 h, now)",
            "baseline_window": f"[now - {SR.BASELINE_DAYS} d, now - 1 h); oldest 70% fit, newest 30% calibrate",
        },
        "parameters": B.method_parameters(),
        "metrics": metrics,
        "baseline_metrics": {name: main["per_method"][name] for name in GATED_BASELINES},
        "ablations": {name: main["per_method"][name] for name in ABLATIONS},
        "misspecification": misspecification,
        "validation": dict(validation, split=VALIDATION_SPLIT,
                           note="development split: examined while building; not held out, never gated"),
        "development_notes": list(DEVELOPMENT_NOTES),
        "adoption": decide_adoption(metrics, ledger, data_hash=main_hash),
        "gate": {
            "data_hash": main_hash, "test_set": TEST_SET, "alarm_score": ALARM_SCORE,
            "max_clean_false_alarm_rate": MAX_CLEAN_FALSE_ALARM_RATE, "gated_baselines": list(GATED_BASELINES),
            "criteria": [
                "paired PR-AUC difference (model - baseline) has a 95% lower bound above 0, for every gated baseline",
                f"clean false-alarm rate at score >= {ALARM_SCORE} is at most {MAX_CLEAN_FALSE_ALARM_RATE}",
                "first recorded evaluation of this test data",
            ],
            "if_not_adopted": "scores are served as experimental and never presented as alerts",
        },
        "evaluation_config": evaluation_config(seed=seed, split=split, config=config, variants=variants,
                                               misspecification_config=misspecification_config,
                                               validation_config=validation_config, n_boot=n_boot),
        "seed": seed,
        "generator": f"{SY.GENERATOR_NAME}@{SY.GENERATOR_VERSION}",
        "generator_sha256": gen_sha,
        "eval_ledger": ledger,
        "caveat": CAVEAT,
    }
    if timing:
        artifact["timing"] = {"build_seconds": round(time.perf_counter() - started, 1),
                              "python": ".".join(str(p) for p in sys.version_info[:3])}
    validate_artifact(artifact, require_sha256=False)
    return artifact


def committed_ledger(path=None) -> dict:
    """The ledger of the committed evaluation ({} if there is none yet). Refuses a file it cannot verify."""
    path = EVAL_PATH if path is None else path
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            embedded = json.loads(fh.read().decode("utf-8"))["sha256"]
        artifact = load_artifact(path, expected_model_type=MODEL_TYPE, expected_sha256=embedded)
    except ArtifactIntegrityError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactIntegrityError(f"cannot read the committed evaluation's ledger ({exc.__class__.__name__})") \
            from None
    return artifact["eval_ledger"]


# --------------------------------------------------------------------------- CLI
def _brief(summary) -> dict:
    def ci(d):
        return None if d is None else {k: (None if d.get(k) is None else round(d[k], 4)) for k in ("estimate", "lo", "hi")}
    return {
        "n_windows": summary["n_windows"], "n_positive": summary["n_positive"],
        "methods": {m: {"pr_auc": ci(r["pr_auc"]), "roc_auc": ci(r["roc_auc"]),
                        "recall_at_alarm": ci(r["recall_at_alarm"]),
                        "clean_false_alarm_rate": ci(r["clean_false_alarm_rate"]), "n_refused": r["n_refused"],
                        "recall_by_type": {t: (None if v["recall_at_alarm"] is None else round(v["recall_at_alarm"], 3))
                                           for t, v in r["per_anomaly_type"].items()}}
                    for m, r in summary["per_method"].items()},
        "paired_pr_auc_vs": {b: {k: (None if d[k] is None else round(d[k], 4)) for k in ("estimate", "lo", "hi")}
                             for b, d in summary["paired_pr_auc_vs"].items()},
    }


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m amp_ai.telemetry_anomaly.build_eval",
                                     description="Evaluate the telemetry anomaly scorer on synthetic machines.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--small", action="store_true", help="seconds-long smoke build on its own split (needs --out)")
    mode.add_argument("--validation-only", action="store_true", help="print validation results; write nothing")
    parser.add_argument("--out", help="output path (--small only; the gated build always writes the committed file)")
    parser.add_argument("--created-at", help="ISO-8601 timestamp to record (default: now, UTC)")
    args = parser.parse_args(argv)
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        _check_created_at(created_at)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def progress(message):
        print(message, file=sys.stderr, flush=True)

    if args.validation_only:
        if args.out:
            print("error: --validation-only writes nothing", file=sys.stderr)
            return 2
        summary = evaluate(seed=SEED, split=VALIDATION_SPLIT, variant="main", config=VALIDATION_CONFIG,
                           n_boot=N_BOOT, methods=METHODS, progress=progress)
        print(json.dumps(_brief(summary), indent=2))
        return 0

    if args.small:
        if not args.out:
            print("error: --small needs --out", file=sys.stderr)
            return 2
        if _same_path(args.out, EVAL_PATH):
            print("error: refusing to overwrite the committed evaluation with a smoke build", file=sys.stderr)
            return 2
        artifact = build(created_at=created_at, prior_ledger={}, seed=SEED, split=SMOKE_SPLIT, config=SMALL_CONFIG,
                         variants=MISSPECIFICATION_VARIANTS, misspecification_config=SMALL_CONFIG,
                         validation_config=SMALL_CONFIG, n_boot=SMALL_N_BOOT, timing=True)
        digest = save_artifact(args.out, artifact)
        print(f"wrote {args.out} (smoke build, sha256 {digest})")
        return 0

    if args.out and not _same_path(args.out, EVAL_PATH):
        print("error: the gated evaluation always writes the committed file, so its ledger cannot be bypassed",
              file=sys.stderr)
        return 2
    try:
        prior = committed_ledger()
    except ArtifactIntegrityError as exc:
        print(f"error: {exc.reason}; refusing to guess the test-set ledger", file=sys.stderr)
        return 3
    artifact = build(created_at=created_at, prior_ledger=prior, progress=progress)
    digest = save_artifact(EVAL_PATH, artifact)
    print(f"wrote {EVAL_PATH}")
    print(f"sha256 {digest}  <- pin as EVAL_SHA256 in amp_ai/telemetry_anomaly/service.py")
    print(json.dumps({"adoption": artifact["adoption"], "test": _brief({
        "n_windows": artifact["metrics"]["n_windows"], "n_positive": artifact["metrics"]["n_positive"],
        "per_method": dict({MODEL_METHOD: artifact["metrics"]}, **artifact["baseline_metrics"], **artifact["ablations"]),
        "paired_pr_auc_vs": artifact["metrics"]["paired_pr_auc_vs"]}), "timing": artifact.get("timing")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
