"""Build and evaluate the AMP-native failure-risk base model: generate -> split -> fit -> evaluate ONCE -> artifact.

    cd backend
    DATABASE_URL="sqlite:///./ci.db" python -m amp_ai.failure_risk.build [--small] [--out DIR] [--created-at ISO]

``DATABASE_URL`` must be set only because the rule baseline imports
``predictive_engine``, which imports the ORM models; the build never opens a
database connection (test_amp_ai_failure_risk_build.py watches for one).

WHAT IS TRAINED, ON WHAT
------------------------
A binary logistic regression (Newton/IRLS, standard library only) on the 18
features of ``features.extract``, predicting whether a machine STARTS a new
breakdown in the next 7 days (``history.breakdown_in_horizon``). The data is
the synthetic fleet of ``synthetic.py`` - 400 machines x 400 days, seed
20260917 - and nothing else: no customer data, so no consent is needed and none
is claimed. ``generator_sha256`` records the generator's source (line endings
normalised) so a changed generator is a changed dataset.

MACHINE-WEEKS
-------------
One row per machine per weekly as-of date (10:00 on day 120, 127, ..., up to
the last day whose 7-day label window ends inside the generated span). A
machine already in Breakdown at as_of is excluded from BOTH the model's and the
rule's evaluation: it cannot start a new breakdown.

SPLIT (amp_ai.core.split.entity_time_split, horizon 7, gap 8)
-------------------------------------------------------------
Machines are partitioned FIRST: 25% test, 15% of the rest validation, the
remainder training. Then time:
    train   t + 7 < 240
    val     240 <= t and t + 7 < 300
    test    t >= 300
    train_end = 300 - 7 - 8 = 285: no model scored on the test set trains on a
    row later than that, so no training label reaches into the test period.
Plant-wide demand and disturbance days are shared by machines of one plant,
which is why the split separates time as well as machines.

MODEL SELECTION (validation only)
---------------------------------
L2 from {0.1, 1, 10, 100, 1000} by validation mean log-loss (ties -> the
stronger penalty). The plan's grid stopped at 100; the first ``--small`` build
(a different seed, so not the shipped test set) chose 100, the edge of the grid,
so 1000 was added before the full build was ever run. With the sum-of-losses
convention of ``core.logistic``, l2 is a prior precision per coefficient on
standardised features, so 1000 is strong but not absurd for ~4,600 rows. Class weights are not used by default; "balanced" weights at the
chosen L2 are tried and win only on strictly higher validation PR-AUC, in which
case the weighted model (fitted on training rows only) is recalibrated with
Platt scaling on validation, because class weights distort probabilities.
Otherwise the model is refitted on training + validation rows with
t <= train_end (``split.refit_rows``).

EVALUATED ONCE
--------------
The test set is scored once per build, and the build records that in the
evaluation ledger under ``generator_sha256`` BEFORE judging it. The CLI carries
the ledger over from the eval JSON already in ``--out``: building the shipped
model a second time on the same generator counts a second look at the same
test set, marks ``test_set_reused`` and fails the gate. (The ledger records
committed runs; it cannot see runs nobody commits. ``--small`` uses a different
seed so development never touches the shipped test set.)

METRICS (model vs the rule scorer, the same rows)
-------------------------------------------------
ROC-AUC, PR-AUC (average precision) and precision@k per as-of date with k = 10%
of machines, with 95% cluster-bootstrap intervals by machine (1000 resamples);
Brier, ECE and the calibration table for the model (the rule's 0-100 score is
not a probability); the base-rate Brier p(1 - p); paired bootstrap differences
model - rule. The misspecification variants of ``synthetic.py`` are scored with
the SAME fitted model on the same test machines and dates, reported and never
gated.

ADOPTION GATE (every criterion must hold)
-----------------------------------------
1. paired PR-AUC difference (model - rule): 95% lower bound > 0
2. ROC-AUC(model) >= ROC-AUC(rule) and precision@k(model) >= precision@k(rule)
3. Brier(model) < p(1 - p) and ECE(model) <= 0.05
4. this build is the FIRST recorded evaluation of this test set

WHAT "ADOPTED" MEANS
--------------------
Even when adopted, the model does NOT replace the rule on any existing screen
(/machine-health, the briefing, the agents). It only lets
/ai/native/failure-risk show the model as the primary column with the rule
beside it, and every surface carries the caveat "Evaluated on synthetic machines
only; not evidence of accuracy on real plants." Changing an existing screen
needs a real-data backtest, which needs its own consent capability and ADR.

After a full build, pin the printed hash as ``predict.ARTIFACT_SHA256``.
"""
import argparse
import copy
import hashlib
import json
import math
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core import metrics as M
from ..core import split as SP
from ..core.artifact import SCHEMA_VERSION, canonical_json, payload_hash, save_artifact, validate_artifact
from ..core.ledger import first_run_for, record_test_evaluation, run_count
from ..core.logistic import BinaryLogisticRegression, PlattCalibrator
from ..core.standardize import Standardizer
from . import baseline_rule, predict, synthetic
from .features import FEATURE_NAMES, extract
from .history import HORIZON_DAYS, LOOKBACK_DAYS, breakdown_in_horizon, in_breakdown_at

__all__ = [
    "SEED", "SMALL_SEED", "BuildConfig", "FULL", "SMALL", "ARTIFACTS_DIR", "ARTIFACT_FILE", "EVAL_FILE",
    "CONSENT_BASIS", "ECE_MAX", "BAND_MULTIPLES", "BuildResult", "generator_sha256", "training_data_source",
    "as_of_days", "make_samples", "split_samples", "select_model", "fit_final", "bands_for", "evaluate",
    "test_set_name", "adoption_decision", "build", "read_prior_ledger", "write_outputs", "main",
]

SEED = 20260917
SMALL_SEED = 20260918          # a DIFFERENT fleet: development runs never see the shipped test set
ARTIFACTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")
ARTIFACT_FILE = "failure_risk_v1.json"
EVAL_FILE = "failure_risk_v1.eval.json"
CONSENT_BASIS = "none required: no customer data used"
ECE_MAX = 0.05
BAND_MULTIPLES = (2.0, 4.0)    # elevated / high, as multiples of the fitted rows' base rate
LABEL = ("1 if a MachineEvent with new_status 'Breakdown' starts in [as_of, as_of + 7 days), else 0; "
         "machine-weeks already in Breakdown at as_of are excluded for the model and the rule alike")
SCOPE = ("Even when adopted, this model does not replace the rule-based scorer on any existing screen "
         "(/machine-health, briefing, agents). It may only be shown as the primary column on "
         "/ai/native/failure-risk, with the rule beside it. Changing an existing screen needs a real-data "
         "backtest under its own consent capability and ADR.")
RULE_SCORER = ("predictive_engine.calculate_predictive_risk through amp_ai.failure_risk.baseline_rule: "
               "counters over [as_of - 30d, as_of), status and utilisation from the last event, no work orders")


@dataclass(frozen=True)
class BuildConfig:
    name: str
    seed: int
    n_machines: int
    days: int
    n_boot: int
    first_day: int = LOOKBACK_DAYS
    step_days: int = 7
    val_start: int = 240
    test_start: int = 300
    horizon: int = HORIZON_DAYS
    gap: int = 8
    test_frac: float = 0.25
    val_share_of_rest: float = 0.15
    l2_grid: tuple = (0.1, 1.0, 10.0, 100.0, 1000.0)
    precision_k_frac: float = 0.10
    ece_bins: int = 10
    variants: tuple = field(default=tuple(v for v in synthetic.VARIANTS if v != "main"))

    @property
    def train_end(self) -> int:
        return self.test_start - self.horizon - self.gap

    @property
    def val_frac(self) -> float:
        # amp_ai.core.split takes fractions of ALL entities
        return self.val_share_of_rest * (1.0 - self.test_frac)

    def describe(self) -> dict:
        return {"name": self.name, "seed": self.seed, "n_machines": self.n_machines, "days": self.days,
                "n_boot": self.n_boot, "first_day": self.first_day, "step_days": self.step_days,
                "val_start": self.val_start, "test_start": self.test_start, "horizon": self.horizon,
                "gap": self.gap, "train_end": self.train_end, "test_frac": self.test_frac,
                "val_share_of_rest": self.val_share_of_rest, "val_frac_of_all": self.val_frac,
                "l2_grid": list(self.l2_grid), "precision_k_frac": self.precision_k_frac,
                "ece_bins": self.ece_bins, "variants": list(self.variants)}


FULL = BuildConfig("full", SEED, n_machines=400, days=400, n_boot=1000)
SMALL = BuildConfig("small", SMALL_SEED, n_machines=120, days=400, n_boot=200)


@dataclass
class BuildResult:
    artifact: dict
    eval_doc: dict
    sha256: str
    split: tuple


# --------------------------------------------------------------------------- provenance
def generator_sha256() -> str:
    """SHA-256 of synthetic.py with CRLF normalised to LF (a Windows checkout hashes like a Linux one)."""
    with open(synthetic.__file__, "rb") as fh:
        source = fh.read()
    return hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest()


def training_data_source(config) -> str:
    return f"synthetic:{synthetic.GENERATOR_ID} seed={config.seed}"


def test_set_name(config, variant) -> str:
    return (f"{variant}:seed={config.seed}:machines={config.n_machines}:days={config.days}"
            f":test_from_day={config.test_start}")


# --------------------------------------------------------------------------- samples
def as_of_days(config) -> list:
    """Weekly as-of days whose 7-day label window [as_of, as_of + horizon) ends inside the generated span."""
    last = config.days - config.horizon - 1
    return list(range(config.first_day, last + 1, config.step_days))


def make_samples(histories, config, *, min_day=None):
    """(samples, n_excluded): one dict per eligible machine-week; machines already down are excluded."""
    samples, excluded = [], 0
    for h in histories:
        for day in as_of_days(config):
            if min_day is not None and day < min_day:
                continue
            as_of = synthetic.as_of_for_day(day)
            if in_breakdown_at(h, as_of):
                excluded += 1
                continue
            samples.append({"machine_id": h.machine_id, "day": day, "as_of": as_of, "history": h,
                            "features": extract(h, as_of), "label": breakdown_in_horizon(h, as_of)})
    return samples, excluded


def split_samples(samples, config):
    return SP.entity_time_split(samples, "machine_id", "day", test_frac=config.test_frac, val_frac=config.val_frac,
                                seed=config.seed, train_end=config.train_end, val_start=config.val_start,
                                test_start=config.test_start, horizon=config.horizon, gap=config.gap)


def _xy(rows):
    return [r["features"] for r in rows], [r["label"] for r in rows]


# --------------------------------------------------------------------------- fitting
def _mean_log_loss(labels, logits):
    total = 0.0
    for t, z in zip(labels, logits):
        softplus = z + math.log1p(math.exp(-z)) if z > 0 else math.log1p(math.exp(z))
        total += softplus - t * z
    return total / len(labels)


def select_model(train, val, config) -> dict:
    """L2 by validation log-loss; class weights only if they win on validation PR-AUC. Validation data only."""
    x_train, y_train = _xy(train)
    x_val, y_val = _xy(val)
    standardizer = Standardizer.fit(x_train, FEATURE_NAMES)
    z_train, z_val = standardizer.transform(x_train), standardizer.transform(x_val)
    table = []
    for l2 in config.l2_grid:
        model = BinaryLogisticRegression(l2=l2).fit(z_train, y_train)
        logits = model.decision_function(z_val)
        table.append({"l2": l2, "val_log_loss": _mean_log_loss(y_val, logits),
                      "val_pr_auc": M.pr_auc(y_val, logits), "converged": model.converged, "n_iter": model.n_iter})
    best = min(table, key=lambda row: (row["val_log_loss"], -row["l2"]))
    weighted = BinaryLogisticRegression(l2=best["l2"], class_weight="balanced").fit(z_train, y_train)
    weighted_pr_auc = M.pr_auc(y_val, weighted.decision_function(z_val))
    use_weights = (weighted_pr_auc is not None and best["val_pr_auc"] is not None
                   and weighted_pr_auc > best["val_pr_auc"])
    return {"l2_grid": table, "l2": best["l2"], "val_pr_auc_unweighted": best["val_pr_auc"],
            "val_pr_auc_balanced": weighted_pr_auc, "class_weight": "balanced" if use_weights else None,
            "val_rows": len(val), "val_positives": sum(y_val)}


def fit_final(train, val, selection, config) -> dict:
    """The shipped fit. Unweighted: refit on train + validation up to train_end. Weighted: train only + Platt on val.

    A calibrator must be INCREASING. Platt scaling fitted on a small validation
    set on which the weighted model ranks no better than chance can come out
    with a negative slope, which would silently reverse every ranking (seen on
    the first --small build: a = -0.248). So a slope <= 0, or a validation set
    Platt cannot be fitted on, rejects the class weights and the unweighted
    refit is shipped instead, with the reason recorded.
    """
    l2, class_weight = selection["l2"], selection["class_weight"]
    if class_weight is None:
        rows = SP.refit_rows(train, val, "day", train_end=config.train_end)
        fitted_on = "training + validation machine-weeks with day <= train_end"
    else:
        rows = list(train)
        fitted_on = "training machine-weeks only; Platt-calibrated on validation"
    x, y = _xy(rows)
    standardizer = Standardizer.fit(x, FEATURE_NAMES)
    model = BinaryLogisticRegression(l2=l2, class_weight=class_weight).fit(standardizer.transform(x), y)
    calibrator = None
    if class_weight is not None:
        x_val, y_val = _xy(val)
        try:
            platt = PlattCalibrator().fit(model.decision_function(standardizer.transform(x_val)), y_val)
            problem = None if platt.a > 0.0 else f"the Platt slope on validation is {platt.a:.4f} (<= 0)"
        except ValueError as exc:
            problem = f"Platt scaling cannot be fitted on validation ({exc})"
        if problem is not None:
            fallback = fit_final(train, val, dict(selection, class_weight=None), config)
            fallback["calibration_rejected"] = (f"class weights won on validation PR-AUC but {problem}; "
                                                "shipped the unweighted refit instead")
            return fallback
        calibrator = platt.to_dict()
    return {"standardizer": standardizer.to_dict(), "logistic": model.to_dict(), "calibrator": calibrator,
            "l2": l2, "class_weight": class_weight, "fitted_on": fitted_on, "fitted_rows": len(rows),
            "fitted_rows_max_day": max(r["day"] for r in rows), "fitted_positives": sum(y),
            "base_rate": sum(y) / len(y), "converged": model.converged, "n_iter": model.n_iter,
            "calibration_rejected": None}


def bands_for(base_rate) -> dict:
    elevated, high = (m * base_rate for m in BAND_MULTIPLES)
    if not 0.0 < elevated < high < 1.0:
        raise ValueError(f"base rate {base_rate} gives unusable bands")
    return {"basis": "multiples of the base rate of the synthetic machine-weeks the model was fitted on",
            "base_rate": base_rate, "elevated_multiple": BAND_MULTIPLES[0], "high_multiple": BAND_MULTIPLES[1],
            "elevated_at": elevated, "high_at": high}


# --------------------------------------------------------------------------- evaluation
def _interval(result) -> dict:
    return {"estimate": result["estimate"], "lo": result["lo"], "hi": result["hi"],
            "n_boot": result["n_boot"], "n_valid": result["n_valid"], "alpha": result["alpha"]}


def _paired(result) -> dict:
    return {"mean": result["mean"], "estimate": result["estimate"], "lo": result["lo"], "hi": result["hi"],
            "n_boot": result["n_boot"], "n_valid": result["n_valid"], "alpha": result["alpha"]}


def evaluate(samples, scorer, config, *, intervals=True) -> dict:
    """Model vs rule on exactly these rows. ``intervals=False`` (misspecification) bootstraps only the paired PR-AUC."""
    y = [s["label"] for s in samples]
    p = [scorer.score(s["features"])["probability"] for s in samples]
    rule = [baseline_rule.rule_score(s["history"], s["as_of"]) for s in samples]
    machines = [s["machine_id"] for s in samples]
    dates = [s["day"] for s in samples]
    frac, bins, n_boot, seed = config.precision_k_frac, config.ece_bins, config.n_boot, config.seed
    positives = sum(y)
    prevalence = positives / len(y)

    def pak(labels, scores, groups):
        return M.precision_at_k_grouped(labels, scores, groups, frac)

    def pak_paired(labelled, scores):
        return M.precision_at_k_grouped([t for t, _ in labelled], scores, [d for _, d in labelled], frac)

    def ece(labels, probs):
        return M.ece(labels, probs, bins)

    out = {"rows": len(y), "machines": len(set(machines)), "dates": len(set(dates)), "positives": positives,
           "prevalence": prevalence, "base_rate_brier": prevalence * (1.0 - prevalence)}
    paired_pr = _paired(M.paired_bootstrap_diff(M.pr_auc, y, p, rule, machines, n_boot, seed=seed))
    if not intervals:
        out.update({
            "model_roc_auc": M.roc_auc(y, p), "rule_roc_auc": M.roc_auc(y, rule),
            "model_pr_auc": M.pr_auc(y, p), "rule_pr_auc": M.pr_auc(y, rule),
            "model_precision_at_k": pak(y, p, dates), "rule_precision_at_k": pak(y, rule, dates),
            "model_brier": M.brier(y, p), "model_ece": ece(y, p), "model_mean_probability": math.fsum(p) / len(p),
            "paired_pr_auc_model_minus_rule": paired_pr,
        })
        return out

    def interval(fn, *arrays):
        return _interval(M.bootstrap_ci(fn, *arrays, groups=machines, n=n_boot, seed=seed))

    labelled = list(zip(y, dates))
    out.update({
        "model": {"roc_auc": interval(M.roc_auc, y, p), "pr_auc": interval(M.pr_auc, y, p),
                  "precision_at_k": interval(pak, y, p, dates), "brier": interval(M.brier, y, p),
                  "ece": interval(ece, y, p), "mean_probability": math.fsum(p) / len(p)},
        "rule": {"roc_auc": interval(M.roc_auc, y, rule), "pr_auc": interval(M.pr_auc, y, rule),
                 "precision_at_k": interval(pak, y, rule, dates)},
        "paired_model_minus_rule": {
            "pr_auc": paired_pr,
            "roc_auc": _paired(M.paired_bootstrap_diff(M.roc_auc, y, p, rule, machines, n_boot, seed=seed)),
            "precision_at_k": _paired(M.paired_bootstrap_diff(pak_paired, labelled, p, rule, machines, n_boot,
                                                              seed=seed)),
        },
        "calibration_table": M.calibration_table(y, p, bins),
        "interval_method": (f"95% percentile cluster bootstrap by machine, {n_boot} resamples, seed {seed}; "
                            f"precision@k is per as-of date with k = ceil({frac} x machines scored that date)"),
    })
    return out


def _fmt(value):
    return "undefined" if value is None else f"{value:.4f}"


def adoption_decision(evaluation, ledger, data_hash, test_set) -> dict:
    """The gate, as a pure function of the test evaluation and the ledger (so it can be recomputed from the eval JSON)."""
    model, rule = evaluation["model"], evaluation["rule"]
    lo = evaluation["paired_model_minus_rule"]["pr_auc"]["lo"]
    model_roc, rule_roc = model["roc_auc"]["estimate"], rule["roc_auc"]["estimate"]
    model_pak, rule_pak = model["precision_at_k"]["estimate"], rule["precision_at_k"]["estimate"]
    brier, base = model["brier"]["estimate"], evaluation["base_rate_brier"]
    ece = model["ece"]["estimate"]
    runs = run_count(ledger, data_hash, test_set)
    criteria = [
        {"name": "paired_pr_auc_lower_bound_above_zero", "value": lo, "threshold": 0.0,
         "passed": lo is not None and lo > 0.0,
         "description": f"paired PR-AUC (model - rule) 95% lower bound {_fmt(lo)} must be > 0"},
        {"name": "roc_auc_not_below_rule", "value": model_roc, "threshold": rule_roc,
         "passed": model_roc is not None and rule_roc is not None and model_roc >= rule_roc,
         "description": f"ROC-AUC model {_fmt(model_roc)} must be >= rule {_fmt(rule_roc)}"},
        {"name": "precision_at_k_not_below_rule", "value": model_pak, "threshold": rule_pak,
         "passed": model_pak is not None and rule_pak is not None and model_pak >= rule_pak,
         "description": f"precision@k model {_fmt(model_pak)} must be >= rule {_fmt(rule_pak)}"},
        {"name": "brier_below_base_rate", "value": brier, "threshold": base,
         "passed": brier is not None and base is not None and brier < base,
         "description": f"Brier {_fmt(brier)} must be < base-rate Brier p(1-p) {_fmt(base)}"},
        {"name": "ece_at_most_0_05", "value": ece, "threshold": ECE_MAX,
         "passed": ece is not None and ece <= ECE_MAX,
         "description": f"ECE {_fmt(ece)} must be <= {ECE_MAX}"},
        {"name": "first_test_run_for_generator", "value": runs, "threshold": 1,
         "passed": first_run_for(ledger, data_hash, test_set),
         "description": f"this must be the first recorded evaluation of the test set (recorded runs: {runs})"},
    ]
    return {"adopted": all(c["passed"] for c in criteria),
            "reasons": [c["description"] for c in criteria if not c["passed"]],
            "criteria": criteria, "test_set": test_set, "test_runs_for_this_test_set": runs,
            "test_set_reused": runs > 1, "scope": SCOPE, "caveat": predict.CAVEAT}


# --------------------------------------------------------------------------- build
def _check_created_at(created_at):
    if not isinstance(created_at, str):
        raise TypeError("created_at must be an ISO-8601 string")
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def build(config, *, created_at, prior_ledger) -> BuildResult:
    """Everything, in memory. Deterministic for a fixed (config, created_at, prior_ledger)."""
    _check_created_at(created_at)
    started = time.perf_counter()
    data_hash = generator_sha256()

    fleet = synthetic.generate_fleet(config.n_machines, config.days, config.seed, "main")
    samples, excluded = make_samples(fleet.histories, config)
    train, val, test = split_samples(samples, config)

    selection = select_model(train, val, config)
    fitted = fit_final(train, val, selection, config)
    parameters = {"standardizer": fitted["standardizer"], "logistic": fitted["logistic"],
                  "calibrator": fitted["calibrator"], "bands": bands_for(fitted["base_rate"]),
                  "lookback_days": LOOKBACK_DAYS, "horizon_days": HORIZON_DAYS}
    scorer = predict.LoadedModel.from_parameters(parameters, FEATURE_NAMES)

    # ---- the test set, once; the ledger is written before the verdict is read
    main_set = test_set_name(config, "main")
    ledger = record_test_evaluation(prior_ledger, data_hash=data_hash, test_set=main_set)
    main_eval = evaluate(test, scorer, config)
    test_machines = sorted({s["machine_id"] for s in test})
    misspecification = {}
    for variant in config.variants:
        variant_fleet = synthetic.generate_fleet(config.n_machines, config.days, config.seed, variant,
                                                 machine_ids=test_machines)
        variant_samples, variant_excluded = make_samples(variant_fleet.histories, config, min_day=config.test_start)
        ledger = record_test_evaluation(ledger, data_hash=data_hash, test_set=test_set_name(config, variant))
        misspecification[variant] = dict(evaluate(variant_samples, scorer, config, intervals=False),
                                         excluded_in_breakdown=variant_excluded)
    adoption = adoption_decision(main_eval, ledger, data_hash, main_set)

    y_train, y_val = [r["label"] for r in train], [r["label"] for r in val]
    training = {
        "selection": selection,
        "l2": fitted["l2"], "class_weight": fitted["class_weight"], "fitted_on": fitted["fitted_on"],
        "fitted_rows": fitted["fitted_rows"], "fitted_rows_max_day": fitted["fitted_rows_max_day"],
        "fitted_positives": fitted["fitted_positives"], "base_rate": fitted["base_rate"],
        "converged": fitted["converged"], "n_iter": fitted["n_iter"],
        "calibration_rejected": fitted["calibration_rejected"],
        "machine_weeks": len(samples), "excluded_in_breakdown": excluded,
        "train_rows": len(train), "train_machines": len({r["machine_id"] for r in train}),
        "train_prevalence": sum(y_train) / len(y_train),
        "val_rows": len(val), "val_machines": len({r["machine_id"] for r in val}),
        "val_prevalence": sum(y_val) / len(y_val),
        "test_rows": len(test), "test_machines": len(test_machines),
    }
    split = {"entity": "machine", "time": "as-of day index (10:00 on that day)",
             "as_of_days": {"first": as_of_days(config)[0], "last": as_of_days(config)[-1], "step": config.step_days},
             "test": f"{config.test_frac:g} of machines, day >= {config.test_start}",
             "validation": (f"{config.val_share_of_rest:g} of the other machines, {config.val_start} <= day and "
                            f"day + {config.horizon} < {config.test_start}"),
             "train": f"the remaining machines, day + {config.horizon} < {config.val_start}",
             "train_end": config.train_end, "horizon": config.horizon, "gap": config.gap,
             "refit": "training + validation rows with day <= train_end (unweighted model)"}
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "model_type": predict.MODEL_TYPE,
        "model_name": predict.MODEL_NAME,
        "version": predict.VERSION,
        "created_at": created_at,
        "training_data_source": training_data_source(config),
        "consent_basis": CONSENT_BASIS,
        "features": list(FEATURE_NAMES),
        "parameters": parameters,
        "metrics": {"test": main_eval, "misspecification": misspecification},
        "baseline_metrics": {"scorer": RULE_SCORER, "test": main_eval["rule"],
                             "misspecification": {v: {"roc_auc": m["rule_roc_auc"], "pr_auc": m["rule_pr_auc"],
                                                      "precision_at_k": m["rule_precision_at_k"]}
                                                  for v, m in misspecification.items()}},
        "adoption": adoption,
        "seed": config.seed,
        "generator": synthetic.GENERATOR_ID,
        "generator_sha256": data_hash,
        "eval_ledger": ledger,
        "label": LABEL,
        "split": split,
        "training": training,
    }
    validate_artifact(artifact, require_sha256=False)
    sha = payload_hash(artifact)
    artifact["sha256"] = sha
    eval_doc = {
        "report": "amp_ai.failure_risk evaluation",
        "artifact_file": ARTIFACT_FILE,
        "artifact_sha256": sha,
        "model_name": predict.MODEL_NAME,
        "version": predict.VERSION,
        "created_at": created_at,
        "seed": config.seed,
        "generator": synthetic.GENERATOR_ID,
        "generator_sha256": data_hash,
        "training_data_source": training_data_source(config),
        "consent_basis": CONSENT_BASIS,
        "caveat": predict.CAVEAT,
        "config": config.describe(),
        "label": LABEL,
        "split": split,
        "training": training,
        "metrics": artifact["metrics"],
        "baseline_metrics": artifact["baseline_metrics"],
        "adoption": adoption,
        "eval_ledger": ledger,
        "ledger_before": copy.deepcopy(prior_ledger) if prior_ledger else {},
        "notes": [
            "Every number here is measured on synthetic machines from amp_ai/failure_risk/synthetic.py; "
            "none is evidence of accuracy on a real plant.",
            "Intervals resample machines. Machines of one plant share demand and disturbance days, so "
            "machine-level resampling can understate uncertainty that is shared at plant level.",
            "The misspecification variants reuse the fitted model and the test machines and dates; they are "
            "reported, never gated.",
            "Design changes made while building, all before the full configuration's test set was first "
            "evaluated, and prompted by --small builds (seed 20260918, a different fleet): the L2 grid gained "
            "1000 after a --small build chose 100, the plan grid's edge; the class-weighted path rejects a "
            "Platt calibrator whose slope is not positive after --small data produced a = -0.248.",
        ],
        "build_seconds": time.perf_counter() - started,
        "python": platform.python_version(),
    }
    return BuildResult(artifact=artifact, eval_doc=eval_doc, sha256=sha, split=(train, val, test))


# --------------------------------------------------------------------------- files
def _strict_json(data: bytes):
    def refuse(name):
        raise ValueError(f"non-finite constant {name}")
    return json.loads(data.decode("utf-8"), parse_constant=refuse)


def read_prior_ledger(eval_path) -> dict:
    """The ledger from an existing eval JSON, {} if there is none. A malformed one is refused, never reset."""
    if not os.path.exists(eval_path):
        return {}
    with open(eval_path, "rb") as fh:
        doc = _strict_json(fh.read())
    ledger = doc.get("eval_ledger") if isinstance(doc, dict) else None
    if not isinstance(ledger, dict):
        raise ValueError(f"{eval_path} has no eval_ledger; refusing to start a fresh ledger over it")
    run_count(ledger, "0" * 64, "probe")          # validates the shape; raises ValueError if malformed
    return ledger


def write_outputs(result, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, ARTIFACT_FILE)
    eval_path = os.path.join(out_dir, EVAL_FILE)
    sha = save_artifact(artifact_path, result.artifact)
    if sha != result.sha256:
        raise RuntimeError("saved artifact hash differs from the built payload hash")
    canonical_json(result.eval_doc)               # refuses NaN / non-JSON values before writing
    text = json.dumps(result.eval_doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    tmp = eval_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(text.encode("ascii"))
    os.replace(tmp, eval_path)
    return artifact_path, eval_path


def _same_dir(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m amp_ai.failure_risk.build", description=__doc__.split("\n")[0])
    parser.add_argument("--small", action="store_true", help="120 machines, a different seed, 200 resamples")
    parser.add_argument("--out", default=ARTIFACTS_DIR, help="output directory (default: the shipped artifacts)")
    parser.add_argument("--created-at", help="ISO-8601 timestamp to record (default: now, UTC)")
    args = parser.parse_args(argv)
    config = SMALL if args.small else FULL
    if args.small and _same_dir(args.out, ARTIFACTS_DIR):
        print("refusing: --small never writes into the shipped artifacts directory; pass --out", file=sys.stderr)
        return 2
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        _check_created_at(created_at)
        prior = read_prior_ledger(os.path.join(args.out, EVAL_FILE))
    except (TypeError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    result = build(config, created_at=created_at, prior_ledger=prior)
    artifact_path, eval_path = write_outputs(result, args.out)
    test = result.artifact["metrics"]["test"]
    adoption = result.artifact["adoption"]

    def show(block, key):
        v = block[key]
        return f"{_fmt(v['estimate'])} [{_fmt(v['lo'])}, {_fmt(v['hi'])}]"

    print(f"built {config.name}: {result.eval_doc['build_seconds']:.1f} s")
    print(f"  test rows {test['rows']} ({test['machines']} machines x {test['dates']} dates), "
          f"positives {test['positives']}, prevalence {test['prevalence']:.4f}")
    for key in ("roc_auc", "pr_auc", "precision_at_k"):
        print(f"  {key:<15} model {show(test['model'], key)}   rule {show(test['rule'], key)}")
    paired = test["paired_model_minus_rule"]["pr_auc"]
    print(f"  paired PR-AUC model - rule {_fmt(paired['estimate'])} [{_fmt(paired['lo'])}, {_fmt(paired['hi'])}]")
    print(f"  brier {show(test['model'], 'brier')}  base-rate brier {_fmt(test['base_rate_brier'])}  "
          f"ece {show(test['model'], 'ece')}")
    print(f"  adopted: {adoption['adopted']}")
    for reason in adoption["reasons"]:
        print(f"    - {reason}")
    print(f"  wrote {artifact_path}")
    print(f"  wrote {eval_path}")
    print(f"  pin in amp_ai/failure_risk/predict.py: ARTIFACT_SHA256 = \"{result.sha256}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
