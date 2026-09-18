"""Score machine histories with the pinned, hash-verified failure-risk artifact. Learns nothing.

WHAT IT ANSWERS
---------------
"How likely is this machine to START a new breakdown in the next 7 days?", for
each history it is handed, next to the existing rule-based score for the same
machine. The histories come from the AMP data layer
(``db_history.load_histories``, tenant-scoped and bounded); this module never
queries, never chooses a tenant and never sees a role. It turns records into
numbers and nothing else.

TRUST
-----
* The artifact is read on EVERY call and verified against ``ARTIFACT_SHA256``,
  pinned below. Changing the model means changing that line, in review. A file
  that fails any check (edited, corrupt, missing, wrong model type, a feature
  list this code does not extract) gives ``{"status": "model_unavailable"}``
  with the reason - never a guess and never an exception to the caller. There
  is no cache, so a replaced file is noticed on the next request.
* Scoring is a pure function of the artifact and the history: missing inputs
  are filled with the TRAINING medians stored in the artifact, standardised
  with the TRAINING statistics, and nothing is fitted, updated or remembered.
  A tenant's records are read to compute features and are never learned from,
  so nothing learned from one tenant can reach another.

WHAT IS RETURNED
----------------
``{"status": "ok", "model_name", "model_version", "artifact_sha256", "adopted",
"caveat", "training_data_source", "as_of", "horizon_days", "lookback_days",
"machines": [...]}`` with, per machine: ``machine_id``, ``name``,
``probability`` (None when not scored), ``band`` (low / elevated / high,
relative to the synthetic training base rate), ``top_contributions`` (the three
features that moved the logit most, exact for a linear model), ``rule_score``
and ``rule_level`` (the existing scorer, same as-of), ``model_version``,
``adopted``, ``excluded_reason`` and ``data_basis`` (records read, features that
had to be imputed, ``learned_from: False``).

A machine already in Breakdown at ``as_of`` is not scored: it cannot START a
new breakdown, which is what the model was trained to predict.

``adopted`` comes from the artifact's adoption gate. Even when it is true the
model does not replace the rule on any existing screen; see build.py.
"""
import os

from ..core.artifact import ArtifactIntegrityError, load_artifact
from ..core.explain import linear_contributions
from ..core.logistic import BinaryLogisticRegression, PlattCalibrator
from ..core.standardize import Standardizer
from . import baseline_rule
from .features import FEATURE_NAMES, extract
from .history import HORIZON_DAYS, LOOKBACK_DAYS, in_breakdown_at, truncate

__all__ = [
    "MODEL_TYPE", "MODEL_NAME", "VERSION", "ARTIFACT_PATH", "ARTIFACT_SHA256", "CAVEAT", "BANDS",
    "LoadedModel", "load_model", "predict",
]

MODEL_TYPE = "failure_risk_logistic"
MODEL_NAME = "failure_risk"
VERSION = "v1"
ARTIFACT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "artifacts", "failure_risk_v1.json")
ARTIFACT_SHA256 = "ad30970f3ac1fca60c955b352939f42152c5e9a0b03f42dfb01047565f90f821"
CAVEAT = "Evaluated on synthetic machines only; not evidence of accuracy on real plants."
BANDS = ("low", "elevated", "high")
TOP_CONTRIBUTIONS = 3


class LoadedModel:
    """The scoring half of an artifact's ``parameters``: standardiser, logistic model, optional calibrator, bands."""

    __slots__ = ("names", "standardizer", "logistic", "calibrator", "elevated_at", "high_at")

    @classmethod
    def from_parameters(cls, parameters, feature_names) -> "LoadedModel":
        names = list(feature_names)
        if tuple(names) != FEATURE_NAMES:
            raise ValueError("the artifact's feature list is not features.FEATURE_NAMES; rebuild the model")
        standardizer = Standardizer.from_dict(parameters["standardizer"])
        if standardizer.names != names:
            raise ValueError("the standardiser's features differ from the artifact's feature list")
        logistic = BinaryLogisticRegression.from_dict(parameters["logistic"])
        if len(logistic.coef) != len(names):
            raise ValueError("the model's coefficient count differs from the feature list")
        calibrator = parameters["calibrator"]
        calibrator = None if calibrator is None else PlattCalibrator.from_dict(calibrator)
        if calibrator is not None and not calibrator.a > 0.0:
            raise ValueError("the calibrator is not increasing (a <= 0): it would reverse every ranking")
        bands = parameters["bands"]
        elevated_at, high_at = float(bands["elevated_at"]), float(bands["high_at"])
        if not 0.0 < elevated_at < high_at < 1.0:
            raise ValueError("band thresholds must satisfy 0 < elevated_at < high_at < 1")
        model = cls()
        model.names = names
        model.standardizer = standardizer
        model.logistic = logistic
        model.calibrator = calibrator
        model.elevated_at = elevated_at
        model.high_at = high_at
        return model

    @property
    def intercept(self) -> float:
        return self.logistic.intercept

    def score(self, feature_row) -> dict:
        """{"logit", "probability", "z"} for one feature dict, using stored statistics only."""
        z = self.standardizer.transform([feature_row])[0]
        logit = self.logistic.decision_function([z])[0]
        if self.calibrator is None:
            probability = self.logistic.predict_proba([z])[0]
        else:
            probability = self.calibrator.transform([logit])[0]
        return {"logit": logit, "probability": probability, "z": z}

    def contributions(self, z, top=TOP_CONTRIBUTIONS) -> list:
        return linear_contributions(self.logistic.coef, z, self.names, top=top)

    def band(self, probability) -> str:
        if probability >= self.high_at:
            return "high"
        if probability >= self.elevated_at:
            return "elevated"
        return "low"


def load_model(artifact_path=None):
    """(artifact dict, LoadedModel), verified against the pinned hash. Raises ArtifactIntegrityError."""
    path = ARTIFACT_PATH if artifact_path is None else artifact_path
    artifact = load_artifact(path, expected_model_type=MODEL_TYPE, expected_sha256=ARTIFACT_SHA256)
    try:
        model = LoadedModel.from_parameters(artifact["parameters"], artifact["features"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError(f"artifact parameters are unusable: {exc}") from None
    return artifact, model


def _data_basis(view, row):
    return {
        "lookback_days": LOOKBACK_DAYS,
        "records": {"events": len(view.events), "downtime": len(view.downtime),
                    "production": len(view.production), "inspections": len(view.inspections),
                    "maintenance": len(view.maintenance)},
        "imputed_features": [] if row is None else [name for name in FEATURE_NAMES if row[name] is None],
        "learned_from": False,
    }


def predict(histories, as_of, *, artifact_path=None) -> dict:
    """Score each history at ``as_of``. See the module docstring for the shape and the guarantees."""
    try:
        artifact, model = load_model(artifact_path)
    except ArtifactIntegrityError as exc:
        return {"status": "model_unavailable", "reason": exc.reason, "caveat": CAVEAT}
    version = f"{artifact['model_name']}@{artifact['version']}"
    adopted = artifact["adoption"]["adopted"]
    machines = []
    for history in histories:
        view = history if history.window_end == as_of else truncate(history, as_of)
        rule = baseline_rule.rule_row(view, as_of)
        entry = {
            "machine_id": history.machine_id,
            "name": history.name,
            "probability": None,
            "band": None,
            "top_contributions": [],
            "rule_score": float(rule["risk_score"]),
            "rule_level": rule["risk_level"],
            "model_version": version,
            "adopted": adopted,
            "excluded_reason": None,
        }
        if in_breakdown_at(view, as_of):
            entry["excluded_reason"] = "already_in_breakdown"
            entry["data_basis"] = _data_basis(view, None)
        else:
            row = extract(view, as_of)
            scored = model.score(row)
            entry["probability"] = scored["probability"]
            entry["band"] = model.band(scored["probability"])
            entry["top_contributions"] = [dict(c, raw_value=row[c["name"]], imputed=row[c["name"]] is None)
                                          for c in model.contributions(scored["z"])]
            entry["data_basis"] = _data_basis(view, row)
        machines.append(entry)
    return {
        "status": "ok",
        "model_name": artifact["model_name"],
        "model_version": version,
        "artifact_sha256": artifact["sha256"],
        "adopted": adopted,
        "caveat": CAVEAT,
        "training_data_source": artifact["training_data_source"],
        "as_of": as_of.isoformat(),
        "horizon_days": HORIZON_DAYS,
        "lookback_days": LOOKBACK_DAYS,
        "machines": machines,
    }
