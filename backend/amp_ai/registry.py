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
and its reasons, the evaluation ledger and the synthetic-only caveat. Never
``parameters``: the weights are not a customer-facing fact, and a card that
carried them would be a way to exfiltrate a model through an API.

An artifact that fails verification gives ``available: False`` with the reason,
and no numbers at all: metrics read from a file that failed its hash check are
not metrics.

The card is rebuilt on every request (no cache), so a replaced file is noticed
immediately. The copilot's 0.9 MB artifact costs about 50 ms to verify.
"""
from .copilot_intent import classifier
from .core.artifact import ArtifactIntegrityError, load_artifact
from .failure_risk import predict
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
    corpus = _get(a, "metrics", "corpus_test") or {}
    rows = [
        _row("Routing accuracy", "held-out questions not written by the model's author (64)",
             _get(ext, "H", "accuracy"), _get(ext, "K", "accuracy"), "keyword router",
             difference=_get(ext, "h_minus_k_accuracy_bootstrap"), unit="percent"),
        _row("Macro-F1", "held-out questions not written by the model's author (64)",
             _get(ext, "H", "macro_f1"), _get(ext, "K", "macro_f1"), "keyword router"),
        _row("Routing accuracy", "AMP-authored test question families (favours the model)",
             _get(corpus, "H", "accuracy"), _get(corpus, "K", "accuracy"), "keyword router",
             difference=_get(corpus, "h_minus_k_accuracy_bootstrap"), unit="percent"),
    ]
    return [r for r in rows if r]


def _misspecification_failure_risk(a):
    model = _get(a, "metrics", "misspecification")
    rule = _get(a, "baseline_metrics", "misspecification")
    return {"model": model, "rule": rule} if model else None


class _Entry:
    __slots__ = ("title", "purpose", "baseline", "default_behaviour", "module", "path_attr", "pin_attr",
                 "type_attr", "verify", "headline", "misspecification", "extras")

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
        misspecification=_misspecification_failure_risk, extras=("label", "split")),
    "telemetry_anomaly": _Entry(
        title="Telemetry anomaly check",
        purpose=("Scores the last hour of one machine's telemetry against a baseline fitted, on request, from "
                 "that machine's own previous 14 days. Needs the company's learning consent."),
        baseline="A mean/std z-score and static range checks, on the same windows.",
        default_behaviour=("Not adopted: scores are experimental, are never shown as alerts, and exist only "
                           "while the request is answered."),
        module=service, path_attr="EVAL_ARTIFACT_PATH", pin_attr="EVAL_SHA256", type_attr="MODEL_TYPE",
        verify=_verify_telemetry_anomaly, headline=_headline_telemetry_anomaly,
        misspecification=lambda a: a.get("misspecification"), extras=("gate", "ablations", "development_notes")),
    "copilot_intent": _Entry(
        title="AMP-native copilot intent model",
        purpose=("Proposes which of the copilot's fixed answers a question needs. It can only name an "
                 "allowlisted pillar; AMP fetches the data with the asker's own tenant and permissions."),
        baseline="The copilot's keyword router, on the same questions.",
        default_behaviour=("Not adopted: the keyword router stays in charge. The model is switched on only by "
                           "a future build that passes its gate on a fresh question set."),
        module=classifier, path_attr="ARTIFACT_PATH", pin_attr="ARTIFACT_SHA256", type_attr="MODEL_TYPE",
        verify=_verify_copilot_intent, headline=_headline_copilot_intent,
        misspecification=lambda a: None, extras=()),
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
                    "reasons": [], "headline": [], "metrics": None, "baseline_metrics": None})
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
    })
    for key in entry.extras:
        if key in artifact:
            out[key] = artifact[key]
    return out


def cards() -> list:
    return [card(name) for name in names()]
