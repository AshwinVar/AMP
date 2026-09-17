"""Runtime for the AMP-native copilot intent model: question in, proposed pillar NAME out.

WHAT IT DOES
------------
``load()`` reads ``artifacts/copilot_intent_v1.json`` once, verifies it against
``ARTIFACT_SHA256`` (the hash PINNED below, so replacing the model is a reviewed
code change) and caches the resulting ``IntentClassifier``. ``route(question)``
returns a ``RouteDecision``:

  * ``route`` is a name from ``labels.ROUTE_LABELS``, or None. None means "no
    opinion - let the keyword router decide": the question has no features,
    the model's best label is ``__none__``, or its confidence is below the
    threshold ``tau`` chosen on the validation split (the smallest threshold at
    which validation accuracy of the confident predictions is >= 0.9).
  * ``confidence`` is the top label's softmax probability, ``top`` the three
    most probable labels, ``model_version`` "<model_name>@<version>".

WHAT IT CANNOT DO (constraint 6 of the AMP-native AI build)
----------------------------------------------------------
It takes a question string and nothing else: no database session, tenant,
user or role. It imports only the standard library and ``amp_ai.core`` (checked
by test_amp_ai_intent_isolation.py), so it cannot build a query or pick a
tenant. The tenant-scoped pillar that answers is run by ``ai.assistant`` with the
tenant the request already holds.

It learns nothing at runtime: no question is stored, no statistic is updated,
and the loaded artifact is never modified. Two tenants asking the same question
get the same decision regardless of order.

It is NOT the adoption decision. ``adopted`` mirrors the artifact's evaluated
gate; the integration layer uses the model only when that is true.

FROZEN FEATURE SPEC
-------------------
``FEATURE_SPEC`` must equal the artifact's ``features`` exactly, or the
classifier refuses to construct: a model trained on different features would
score garbage without any error. Input is cut to ``MAX_QUESTION_CHARS``
characters before normalisation, so a huge question costs bounded time.
"""
import math
import os
import threading

from ..core.artifact import load_artifact
from ..core.contracts import RouteDecision
from ..core.multinomial import MultinomialLogisticRegression
from ..core.text_features import hashed_features
from .labels import LABELS, to_route

__all__ = [
    "MODEL_TYPE", "MODEL_NAME", "MODEL_VERSION", "ARTIFACT_SHA256", "ARTIFACT_PATH", "EVAL_PATH",
    "MAX_QUESTION_CHARS", "FEATURE_SPEC", "TOP_K", "featurize", "top_label", "IntentClassifier", "load",
]

MODEL_TYPE = "copilot_intent_multinomial_logreg"
MODEL_NAME = "copilot_intent"
MODEL_VERSION = "v1"

# The SHA-256 of the committed artifact's canonical payload. Changing the model
# means changing this line. test_amp_ai_intent_build.py ties the file to it.
ARTIFACT_SHA256 = "69e825efacdf160529193b5709cf6a397fa5838c57071eb830dceb1c4235922b"

_ARTIFACT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")
ARTIFACT_PATH = os.path.join(_ARTIFACT_DIR, "copilot_intent_v1.json")
EVAL_PATH = os.path.join(_ARTIFACT_DIR, "copilot_intent_v1.eval.json")

MAX_QUESTION_CHARS = 1000
FEATURE_SPEC = {
    "dim": 8192,
    "word_ngrams": [1, 2],
    "char_ngrams": [4, 4],
    "hash": "blake2b-8byte signed: sign = lowest bit, index = (value >> 1) % dim",
    "normalize": "amp_ai.core.text_features.normalize_text (NFKC, lower, apostrophes deleted, '-' kept)",
    "l2_normalized": True,
    "max_question_chars": MAX_QUESTION_CHARS,
}
TOP_K = 3


def featurize(question) -> dict:
    """The model's input for a question: signed hashed n-grams per FEATURE_SPEC. Non-text has no features."""
    if not isinstance(question, str):
        return {}
    return hashed_features(question[:MAX_QUESTION_CHARS], dim=FEATURE_SPEC["dim"],
                           word_ngrams=tuple(FEATURE_SPEC["word_ngrams"]),
                           char_ngrams=tuple(FEATURE_SPEC["char_ngrams"]))


def top_label(probabilities) -> tuple:
    """(label, probability) of the most probable label; a tie goes to the label listed first.

    The one argmax rule: the build's threshold selection and ``route`` both use it.
    """
    best = None
    for label, p in probabilities.items():
        if best is None or p > best[1]:
            best = (label, p)
    if best is None:
        raise ValueError("no probabilities")
    return best


def _require(condition, message):
    if not condition:
        raise ValueError(message)


class IntentClassifier:
    """A verified artifact turned into a router proposer. Construct via ``load()`` in production."""

    def __init__(self, artifact):
        _require(isinstance(artifact, dict), "artifact must be a dict")
        _require(artifact.get("model_type") == MODEL_TYPE, f"model_type must be {MODEL_TYPE!r}")
        _require(artifact.get("features") == FEATURE_SPEC,
                 "the artifact's feature spec differs from FEATURE_SPEC; refusing to score with it")
        params = artifact.get("parameters")
        _require(isinstance(params, dict), "parameters must be an object")
        model_dict = params.get("model")
        _require(isinstance(model_dict, dict), "parameters.model must be an object")
        _require(model_dict.get("labels") == list(LABELS),
                 "the model's labels must be exactly labels.LABELS in order (the allowlist plus __none__)")
        tau = params.get("tau")
        _require(not isinstance(tau, bool) and isinstance(tau, (int, float)) and math.isfinite(tau)
                 and 0.0 <= tau <= 1.0, "tau must be a number in [0, 1]")
        selection = params.get("tau_selection")
        _require(isinstance(selection, dict) and type(selection.get("qualified")) is bool,
                 "tau_selection.qualified must be true or false")
        adoption = artifact.get("adoption")
        _require(isinstance(adoption, dict) and type(adoption.get("adopted")) is bool,
                 "adoption.adopted must be true or false")
        name, version = artifact.get("model_name"), artifact.get("version")
        _require(isinstance(name, str) and name and isinstance(version, str) and version,
                 "model_name and version must be non-empty strings")
        self.model = MultinomialLogisticRegression.from_dict(model_dict)
        self.tau = float(tau)
        self.qualified = selection["qualified"]
        self.adopted = adoption["adopted"]
        self.model_version = f"{name}@{version}"
        self.artifact = artifact

    def probabilities(self, question) -> dict:
        """Softmax probability per label (LABELS order). Empty when the question has no features."""
        x = featurize(question)
        if not x:
            return {}
        return self.model.predict_proba(x)

    def route(self, question) -> RouteDecision:
        p = self.probabilities(question)
        if not p:
            return RouteDecision(None, 0.0, [], self.model_version)
        label, confidence = top_label(p)
        order = {name: i for i, name in enumerate(LABELS)}
        ranked = sorted(p.items(), key=lambda item: (-item[1], order[item[0]]))[:TOP_K]
        # to_route is the one place "__none__ means no route" is decided: a confident __none__ gives None.
        route = to_route(label) if self.qualified and confidence >= self.tau else None
        return RouteDecision(route, confidence, [(name, prob) for name, prob in ranked], self.model_version)


_CACHE = {}
_LOCK = threading.Lock()


def load(path=None) -> IntentClassifier:
    """The verified classifier for ``path`` (default: the committed artifact), cached per path.

    Every path is verified against the PINNED ``ARTIFACT_SHA256``; a failure raises
    ``ArtifactIntegrityError`` and caches nothing, so callers report "model
    unavailable" instead of guessing.
    """
    key = os.path.abspath(ARTIFACT_PATH if path is None else os.fspath(path))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            artifact = load_artifact(key, expected_model_type=MODEL_TYPE, expected_sha256=ARTIFACT_SHA256)
            cached = IntentClassifier(artifact)
            _CACHE[key] = cached
        return cached
