"""The fixed registry of AMP-native models and their public model cards (ADR-0020).

A NAME IS LOOKED UP, NEVER USED AS A PATH
-----------------------------------------
``GET /ai/models/{name}`` passes ``name`` to ``card()``, which answers only for a
key of ``MODELS``. The file each entry reads comes from the capability's own
module (``predict.ARTIFACT_PATH``, ``service.EVAL_ARTIFACT_PATH``,
``classifier.ARTIFACT_PATH``), read at call time, and is verified against the
hash PINNED in that module. This file holds no path and no hash of its own, so
there is one pin per model.

A CARD IS METADATA
------------------
Built from a whitelist of the verified artifact's keys: provenance (training data
source, consent basis, generator and its hash, seed, created_at, sha256), the
held-out metrics and the baseline's metrics on the same data, the adoption verdict
and its reasons, the evaluation ledger and the synthetic-only caveat, the stress-test
(misspecification) rows in the headline shape, and ``limitations``: the known
weaknesses a reader must see before quoting a number (for failure risk, drawn
from the pinned post-hoc diagnostics in ``failure_risk.diagnose``). Never
``parameters``: the weights are not a customer-facing fact, and a card that
carried them would be a way to exfiltrate a model through an API.

An artifact that fails verification gives ``available: False`` with the reason,
and no numbers at all: metrics read from a file that failed its hash check are
not metrics.

The card is rebuilt on every request (no cache), so a replaced file is noticed
immediately. The copilot's 0.9 MB artifact costs about 50 ms to verify.
"""
from .copilot_intent import classifier, corpus
from .core.artifact import ArtifactIntegrityError, load_artifact
from .failure_risk import diagnose, predict
from .telemetry_anomaly import service

__all__ = ["CAVEAT", "MODELS", "names", "pinned_sha256", "card", "cards"]

CAVEAT = "Evaluated on synthetic machines only; not evidence of accuracy on real plants."

# Keys copied from a verified artifact into its card. "parameters" is absent on purpose.
_CARD_KEYS = ("model_type", "model_name", "version", "created_at", "sha256", "training_data_source",
              "consent_basis", "generator", "generator_sha256", "seed", "metrics", "baseline_metrics",
              "adoption", "eval_ledger")


def _get(d, *path):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def _num(x, part="estimate"):
    if isinstance(x, dict):
        x = x.get(part)
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _row(metric, dataset, model, baseline, baseline_name, *, difference=None, higher_is_better=True,
         unit="score"):
    """One headline comparison. `unit` is "percent" for a proportion (accuracy, recall, a rate) and
    "score" for a unitless score (AUC, F1), so the UI never guesses a format from the label."""
    if _num(model) is None or _num(baseline) is None:
        return None
    return {
        "metric": metric, "dataset": dataset, "unit": unit, "higher_is_better": higher_is_better,
        "model": _num(model), "model_lo": _num(model, "lo"), "model_hi": _num(model, "hi"),
        "baseline_name": baseline_name,
        "baseline": _num(baseline), "baseline_lo": _num(baseline, "lo"), "baseline_hi": _num(baseline, "hi"),
        "difference": _num(difference), "difference_lo": _num(difference, "lo"),
        "difference_hi": _num(difference, "hi"),
    }


# --- per-model facts ------------------------------------------------------------
def _verify_failure_risk():
    artifact, _model = predict.load_model()
    return artifact


def _verify_telemetry_anomaly():
    verdict = service.evaluation_card()
    if not verdict["available"]:
        raise ArtifactIntegrityError(verdict["reason"])
    return load_artifact(service.EVAL_ARTIFACT_PATH, expected_model_type=service.MODEL_TYPE,
                         expected_sha256=service.EVAL_SHA256)


def _verify_copilot_intent():
    artifact = load_artifact(classifier.ARTIFACT_PATH, expected_model_type=classifier.MODEL_TYPE,
                             expected_sha256=classifier.ARTIFACT_SHA256)
    try:
        classifier.IntentClassifier(artifact)
    except (ValueError, TypeError) as exc:
        raise ArtifactIntegrityError(f"artifact is not usable by the classifier: {exc}") from None
    return artifact


def _headline_failure_risk(a):
    t = _get(a, "metrics", "test") or {}
    ds = "held-out synthetic test machines"
    rows = [
        _row("PR-AUC", ds, _get(t, "model", "pr_auc"), _get(t, "rule", "pr_auc"), "rule-based risk score",
             difference=_get(t, "paired_model_minus_rule", "pr_auc")),
        _row("ROC-AUC", ds, _get(t, "model", "roc_auc"), _get(t, "rule", "roc_auc"), "rule-based risk score",
             difference=_get(t, "paired_model_minus_rule", "roc_auc")),
        _row("Precision in the top 10%", ds, _get(t, "model", "precision_at_k"), _get(t, "rule", "precision_at_k"),
             "rule-based risk score", difference=_get(t, "paired_model_minus_rule", "precision_at_k"),
             unit="percent"),
    ]
    doc, _reason = _failure_risk_diagnostics()
    if doc is not None:
        # The trivial baseline the review asked for, from the pinned post-hoc diagnostics: the one raw input
        # chosen on validation rows, on the SAME test rows. Without it the rows above read as a stronger result
        # than the evaluation supports.
        sel = _get(doc, "single_feature", "selected_on_validation") or {}
        rows.append(_row("PR-AUC", ds, _get(t, "model", "pr_auc"), sel.get("pr_auc"),
                         f"one input alone ({sel.get('feature')}, chosen on validation)",
                         difference=_get(sel, "model_minus_feature", "pr_auc")))
    return [r for r in rows if r]


# --- known limitations: what a reader must know before quoting the rows above ---------------------
def _failure_risk_diagnostics():
    """(verified post-hoc diagnostics, None) or (None, the reason they could not be verified)."""
    try:
        return diagnose.load_diagnostics(), None
    except ArtifactIntegrityError as exc:
        return None, exc.reason


def failure_risk_evaluation_scope() -> dict:
    """When the failure-risk evaluation's snapshots were taken, for GET /ai/native/failure-risk to show.

    From the pinned diagnostics; if they cannot be verified, says so rather than inventing a scope.
    """
    doc, reason = _failure_risk_diagnostics()
    if doc is None:
        return {"as_of_weekdays": None, "as_of_times": None,
                "note": f"The evaluation's snapshot times could not be verified: {reason}"}
    scope = doc["evaluation_scope"]
    return {"as_of_weekdays": list(scope["as_of_weekdays"]), "as_of_times": list(scope["as_of_times"]),
            "note": (f"Evaluated only on snapshots taken on "
                     f"{' and '.join(w + 's' for w in scope['as_of_weekdays'])} at {', '.join(scope['as_of_times'])}; "
                     "this endpoint scores at whatever moment it is called, which the evaluation may not cover.")}


def _quoted(items):
    return ", ".join(f"'{item}'" for item in items) if items else "none"


def _signed(d):
    return f"{d['estimate']:+.3f} (95% interval {d['lo']:+.3f} to {d['hi']:+.3f})"


def _includes_zero(d):
    return d["lo"] is not None and d["hi"] is not None and d["lo"] <= 0.0 <= d["hi"]


def _limitations_failure_risk(a):
    out = []
    test = _get(a, "metrics", "test") or {}
    model_pr = _num(_get(test, "model", "pr_auc"))
    doc, reason = _failure_risk_diagnostics()
    if doc is None:
        out.append("The post-hoc diagnostics of this evaluation (how little the rule baseline has to work with on "
                   "this synthetic data, and whether one raw input scores as well as the model) could not be "
                   f"verified, so their numbers are not shown: {reason}")
    else:
        rule, dist = doc["rule"], doc["rule"]["score_distribution"]
        out.append(
            f"The rule it beat has little to work with on this synthetic data: {len(rule['always'])} of its "
            f"{len(rule['components'])} components fire on every test machine-week ({_quoted(rule['always'])}) and "
            f"{len(rule['never'])} never fire ({_quoted(rule['never'])}), so it separates machines on "
            f"{len(rule['sometimes'])} yes/no signals ({_quoted(rule['sometimes'])}); "
            f"{dist['modal_share'] * 100:.0f}% of test machine-weeks share its most common score "
            f"({dist['modal_score']:g}). Beating it here is a low bar.")
        sel = doc["single_feature"]["selected_on_validation"]
        d = sel["model_minus_feature"]["pr_auc"]
        tie = _includes_zero(d)
        out.append(
            f"One raw input does about as well: {sel['feature']} alone (its direction set on training rows and "
            f"chosen on validation rows) scores PR-AUC {sel['pr_auc']:.3f} on the same test rows against the "
            f"model's {model_pr:.3f}. Model minus that input: {_signed(d)}, an interval that "
            f"{'includes zero, so the model is not shown to be better than one input' if tie else 'excludes zero'}.")
        pick = doc["single_feature"]["reviewer_pick"]
        dp = pick["model_minus_feature"]["pr_auc"]
        out.append(
            f"{pick['feature']} alone, picked by a reviewer after scanning all 18 inputs on the test set (so it is "
            f"not a fair baseline, only a warning), scores PR-AUC {pick['pr_auc']:.3f}; model minus it: "
            f"{_signed(dp)}.")
        scope = doc["evaluation_scope"]
        out.append(
            f"Evaluated only on snapshots taken on {' and '.join(w + 's' for w in scope['as_of_weekdays'])} at "
            f"{', '.join(scope['as_of_times'])} ({scope['snapshots']} weekly dates). The failure-risk view scores "
            "at whatever moment it is opened, including off-shift, which this evaluation never covered; there the "
            "rule score beside it can pick up components that never fired here.")
    table = test.get("calibration_table") or []
    total = sum(r["n"] for r in table)
    high = [r for r in table if r["lo"] >= 0.2 - 1e-12 and r["n"]]
    if total and high:
        parts = "; ".join(f"{r['lo'] * 100:.0f}-{r['hi'] * 100:.0f}%: {r['n']} row{'s' if r['n'] != 1 else ''}, predicted "
                          f"{r['mean_predicted']:.2f}, observed {r['observed_rate']:.2f}" for r in high)
        lowest = table[0]
        ece = _num(_get(test, "model", "ece"))
        out.append(
            f"Probabilities of 20% or more rest on {sum(r['n'] for r in high)} of {total} test rows, too few to "
            f"establish calibration there ({parts}). The overall calibration error"
            f"{f' (ECE {ece:.3f})' if ece is not None else ''} is dominated by the lowest bin, which holds "
            f"{lowest['n']} of {total} rows.")
    stress = _get(a, "metrics", "misspecification") or {}
    for variant, m in sorted(stress.items()):
        brier, base = _num(m.get("model_brier")), _num(m.get("base_rate_brier"))
        if brier is not None and base is not None and brier >= base:
            out.append(f"On the '{variant}' stress test its Brier score {brier:.4f} is not below the base-rate "
                       f"Brier {base:.4f}: there its probabilities are no better than always predicting the "
                       "prevalence.")
    rocs = [(m.get("model_roc_auc"), v) for v, m in stress.items() if _num(m.get("model_roc_auc")) is not None]
    if rocs:
        roc, variant = min(rocs)
        out.append(f"Its lowest ROC-AUC across the stress tests is {roc:.3f}, on the '{variant}' machines "
                   "(0.5 is chance).")
    return out


def _limitations_telemetry_anomaly(a):
    out = []
    metrics = a.get("metrics") or {}
    method = metrics.get("method")
    alarm = _num(_get(a, "gate", "alarm_score"))
    rate = metrics.get("clean_false_alarm_rate")
    if alarm is not None and isinstance(rate, dict) and _num(rate) is not None:
        stressed = ", ".join(
            f"'{v}' {_num(_get(m, 'per_method', method, 'clean_false_alarm_rate')) * 100:.2f}%"
            for v, m in sorted((a.get("misspecification") or {}).items())
            if _num(_get(m, "per_method", method, "clean_false_alarm_rate")) is not None)
        out.append(
            f"At its alarm level (score {alarm:g} or more, nominally {(1 - alarm) * 100:.0f}% of normal hours) the "
            f"measured false-alarm rate on clean held-out hours is {_num(rate) * 100:.2f}% (95% interval "
            f"{_num(rate, 'lo') * 100:.2f}% to {_num(rate, 'hi') * 100:.2f}%), above the nominal rate"
            f"{f'; on the stress tests: {stressed}' if stressed else ''}.")
    out.append("The score is a rank of the last hour against this machine's own recent hours (\"rarer than N% of "
               "them\"), not a probability that anything is wrong.")
    out.append("Evaluated only on one-hour windows that start with the machine running. The check scores any hour, "
               "including idle, changeover and night hours, which the evaluation never covered.")
    states = _get(a, "parameters", "states") or []
    described = str(_get(a, "features", "state") or "")
    if "Transition" in states and "Transition" not in described:
        out.append("The evaluation file's feature description lists machine states Running / NotRunning / Unknown. "
                   "The method it evaluated (its parameters) also has a Transition state, for five-minute buckets "
                   "whose state changes inside them; the description is corrected on the next evaluation build.")
    out.append("It fits a baseline from this machine's own telemetry on every request, with the company's consent; "
               "only its evaluation used synthetic data.")
    return out


def _limitations_copilot_intent(a):
    n = _num(_get(a, "metrics", "external", "pool", "n"))
    size = f"{n:g} " if n is not None else ""
    return [
        f"The external pool's {size}questions were written in this repository's evaluation files, and the training "
        f"corpus was decontaminated against them only down to a character 4-gram Jaccard similarity of "
        f"{corpus.LEAKAGE_JACCARD:g}: near paraphrases can remain in training, so a gain measured on the pool is, "
        "if anything, optimistic. How close the pool sits to the corpus is not measured by a committed script.",
        "The confidence it reports is the model's raw output, not a calibrated probability.",
    ]


def _misspecification_rows_failure_risk(a):
    rows = []
    for variant, m in sorted((_get(a, "metrics", "misspecification") or {}).items()):
        ds = f"stress test: {variant}"
        rows += [
            _row("PR-AUC", ds, m.get("model_pr_auc"), m.get("rule_pr_auc"), "rule-based risk score",
                 difference=m.get("paired_pr_auc_model_minus_rule")),
            _row("ROC-AUC", ds, m.get("model_roc_auc"), m.get("rule_roc_auc"), "rule-based risk score"),
            _row("Brier score", ds, m.get("model_brier"), m.get("base_rate_brier"),
                 "base-rate Brier (always predict the prevalence)", higher_is_better=False),
        ]
    return [r for r in rows if r]


def _misspecification_rows_telemetry_anomaly(a):
    method = _get(a, "metrics", "method")
    rows = []
    for variant, m in sorted((a.get("misspecification") or {}).items()):
        ds = f"stress test: {variant}"
        model, mean_std = _get(m, "per_method", method) or {}, _get(m, "per_method", "mean_std") or {}
        rows += [
            _row("PR-AUC", ds, model.get("pr_auc"), mean_std.get("pr_auc"), "mean/std z-score",
                 difference=_get(m, "paired_pr_auc_vs", "mean_std")),
            _row("False alarms on clean hours", ds, model.get("clean_false_alarm_rate"),
                 mean_std.get("clean_false_alarm_rate"), "mean/std z-score", higher_is_better=False, unit="percent"),
        ]
    return [r for r in rows if r]


def _headline_telemetry_anomaly(a):
    m, b = a.get("metrics") or {}, a.get("baseline_metrics") or {}
    ds = "held-out synthetic telemetry windows"
    rows = [
        _row("PR-AUC", ds, m.get("pr_auc"), _get(b, "mean_std", "pr_auc"), "mean/std z-score",
             difference=_get(m, "paired_pr_auc_vs", "mean_std")),
        _row("PR-AUC", ds, m.get("pr_auc"), _get(b, "static_range", "pr_auc"), "static range check",
             difference=_get(m, "paired_pr_auc_vs", "static_range")),
        _row("ROC-AUC", ds, m.get("roc_auc"), _get(b, "mean_std", "roc_auc"), "mean/std z-score",
             difference=_get(m, "paired_roc_auc_vs", "mean_std")),
        _row("Recall at alarm (score >= 0.99)", ds, m.get("recall_at_alarm"), _get(b, "mean_std", "recall_at_alarm"),
             "mean/std z-score", unit="percent"),
        _row("False alarms on clean hours", ds, m.get("clean_false_alarm_rate"),
             _get(b, "mean_std", "clean_false_alarm_rate"), "mean/std z-score", higher_is_better=False,
             unit="percent"),
    ]
    return [r for r in rows if r]


def _headline_copilot_intent(a):
    ext = _get(a, "metrics", "external", "pool") or {}
    corpus_test = _get(a, "metrics", "corpus_test") or {}
    n = _num(ext.get("n"))
    pool = (f"repo evaluation questions{f' ({n:g})' if n is not None else ''}; training corpus decontaminated "
            f"against them at Jaccard {corpus.LEAKAGE_JACCARD:g}")
    rows = [
        _row("Routing accuracy", pool,
             _get(ext, "H", "accuracy"), _get(ext, "K", "accuracy"), "keyword router",
             difference=_get(ext, "h_minus_k_accuracy_bootstrap"), unit="percent"),
        _row("Macro-F1", pool,
             _get(ext, "H", "macro_f1"), _get(ext, "K", "macro_f1"), "keyword router"),
        _row("Routing accuracy", "AMP-authored test question families (favours the model)",
             _get(corpus_test, "H", "accuracy"), _get(corpus_test, "K", "accuracy"), "keyword router",
             difference=_get(corpus_test, "h_minus_k_accuracy_bootstrap"), unit="percent"),
    ]
    return [r for r in rows if r]


def _misspecification_failure_risk(a):
    model = _get(a, "metrics", "misspecification")
    rule = _get(a, "baseline_metrics", "misspecification")
    return {"model": model, "rule": rule} if model else None


class _Entry:
    __slots__ = ("title", "purpose", "baseline", "default_behaviour", "module", "path_attr", "pin_attr",
                 "type_attr", "verify", "headline", "misspecification", "misspecification_rows", "limitations",
                 "extras")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


MODELS = {
    "failure_risk": _Entry(
        title="Machine failure risk (next 7 days)",
        purpose="Estimates how likely each machine is to START a new breakdown in the next 7 days.",
        baseline="AMP's rule-based risk scorer (predictive_engine), on the same machine-weeks.",
        default_behaviour=("Shown only on the AMP-native failure-risk view, with the rule score beside it. It "
                           "replaces nothing on Machine Health, the briefing or the agents."),
        module=predict, path_attr="ARTIFACT_PATH", pin_attr="ARTIFACT_SHA256", type_attr="MODEL_TYPE",
        verify=_verify_failure_risk, headline=_headline_failure_risk,
        misspecification=_misspecification_failure_risk,
        misspecification_rows=_misspecification_rows_failure_risk, limitations=_limitations_failure_risk,
        extras=("label", "split")),
    "telemetry_anomaly": _Entry(
        title="Telemetry anomaly check",
        purpose=("Scores the last hour of one machine's telemetry against a baseline fitted, on request, from "
                 "that machine's own previous 14 days. Needs the company's learning consent."),
        baseline="A mean/std z-score and static range checks, on the same windows.",
        default_behaviour=("Not adopted: scores are experimental, are never shown as alerts, and exist only "
                           "while the request is answered."),
        module=service, path_attr="EVAL_ARTIFACT_PATH", pin_attr="EVAL_SHA256", type_attr="MODEL_TYPE",
        verify=_verify_telemetry_anomaly, headline=_headline_telemetry_anomaly,
        misspecification=lambda a: a.get("misspecification"),
        misspecification_rows=_misspecification_rows_telemetry_anomaly, limitations=_limitations_telemetry_anomaly,
        extras=("gate", "ablations", "development_notes")),
    "copilot_intent": _Entry(
        title="AMP-native copilot intent model",
        purpose=("Proposes which of the copilot's fixed answers a question needs. It can only name an "
                 "allowlisted pillar; AMP fetches the data with the asker's own tenant and permissions."),
        baseline="The copilot's keyword router, on the same questions.",
        default_behaviour=("Not adopted: the keyword router stays in charge. The model is switched on only by "
                           "a future build that passes its gate on a fresh question set."),
        module=classifier, path_attr="ARTIFACT_PATH", pin_attr="ARTIFACT_SHA256", type_attr="MODEL_TYPE",
        verify=_verify_copilot_intent, headline=_headline_copilot_intent,
        misspecification=lambda a: None, misspecification_rows=lambda a: [],
        limitations=_limitations_copilot_intent, extras=()),
}


def names() -> list:
    return sorted(MODELS)


def pinned_sha256(name):
    entry = MODELS.get(name) if isinstance(name, str) else None
    return getattr(entry.module, entry.pin_attr) if entry is not None else None


def card(name):
    """The public card for a registered model, or None for any other name."""
    if not isinstance(name, str) or name not in MODELS:
        return None
    entry = MODELS[name]
    out = {
        "name": name, "title": entry.title, "purpose": entry.purpose, "baseline": entry.baseline,
        "default_behaviour": entry.default_behaviour, "model_type": getattr(entry.module, entry.type_attr),
        "sha256_pinned": getattr(entry.module, entry.pin_attr), "caveat": CAVEAT,
    }
    try:
        artifact = entry.verify()
    except ArtifactIntegrityError as exc:
        out.update({"available": False, "status": "unavailable", "adopted": None, "reason": exc.reason,
                    "reasons": [], "headline": [], "metrics": None, "baseline_metrics": None,
                    "limitations": [], "misspecification_rows": []})
        return out
    for key in _CARD_KEYS:
        out[key] = artifact.get(key)
    adopted = _get(artifact, "adoption", "adopted") is True
    out.update({
        "available": True,
        "adopted": adopted,
        "status": "adopted" if adopted else "experimental",
        "reason": None,
        "reasons": list(_get(artifact, "adoption", "reasons") or []),
        "headline": entry.headline(artifact),
        "misspecification": entry.misspecification(artifact),
        "misspecification_rows": entry.misspecification_rows(artifact),
        "limitations": entry.limitations(artifact),
    })
    for key in entry.extras:
        if key in artifact:
            out[key] = artifact[key]
    return out


def cards() -> list:
    return [card(name) for name in names()]
