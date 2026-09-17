"""AMP-native model artifacts: JSON only, hash-verified against a pin in code.

WHAT AN ARTIFACT IS
-------------------
A JSON object with every key in ``REQUIRED_KEYS``: what the model is
(``schema_version``, ``model_type``, ``model_name``, ``version``,
``created_at``), where its training data came from and on what consent basis
(``training_data_source``, ``consent_basis``, ``generator``,
``generator_sha256``, ``seed``), the model itself (``features``,
``parameters``), how it was judged (``metrics``, ``baseline_metrics``,
``adoption``, ``eval_ledger``) and ``sha256``.

WHY THE HASH IS PINNED IN CODE
------------------------------
``sha256`` is the SHA-256 of the canonical JSON of everything EXCEPT the
``sha256`` field: parameters, metrics, adoption verdict and provenance alike.
Stored alone it only detects accidents - anyone editing ``adoption.adopted``
can recompute it. So ``load_artifact`` also requires the hash to equal
``expected_sha256``, a constant the CALLER pins in its source (for example
``ARTIFACT_SHA256`` in the capability's predict module). Replacing a model then
means changing a line of code that shows up in review, and a test ties the
committed file to that pin.

The hash covers the canonical PAYLOAD, not the file's bytes, so git's CRLF
conversion or re-indenting the file does not break the pin; changing any value
does.

WHAT LOADING WILL NOT DO
------------------------
Never pickle, eval, exec or import anything. The file is read as bytes (at most
``MAX_ARTIFACT_BYTES``), decoded as UTF-8 and parsed by ``json`` with:
``NaN``/``Infinity`` refused (``json`` accepts them by default), numbers that
overflow to infinity (``1e999``) refused, and duplicate keys refused (``json``
silently keeps the last one). Any failure raises ``ArtifactIntegrityError``
with a ``reason``; callers turn that into "model unavailable", never a guess.
"""
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime

__all__ = [
    "SCHEMA_VERSION", "REQUIRED_KEYS", "MAX_ARTIFACT_BYTES", "ArtifactIntegrityError",
    "canonical_json", "payload_hash", "validate_artifact", "save_artifact", "load_artifact",
]

SCHEMA_VERSION = 1
REQUIRED_KEYS = ("schema_version", "model_type", "model_name", "version", "created_at",
                 "training_data_source", "consent_basis", "features", "parameters", "metrics",
                 "baseline_metrics", "adoption", "seed", "generator", "generator_sha256",
                 "eval_ledger", "sha256")
MAX_ARTIFACT_BYTES = 8_000_000

_HEX64 = re.compile(r"[0-9a-f]{64}")
_NON_EMPTY_STRINGS = ("model_type", "model_name", "version", "created_at", "training_data_source",
                      "consent_basis", "generator")
_DICTS = ("parameters", "metrics", "baseline_metrics", "adoption", "eval_ledger")


class ArtifactIntegrityError(Exception):
    """The artifact cannot be trusted. ``reason`` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _NonFiniteNumber(ValueError):
    pass


class _DuplicateKey(ValueError):
    pass


# --------------------------------------------------------------------------- canonical form
def _check_json_value(obj, path):
    kind = type(obj)
    if obj is None or kind is bool or kind is str or kind is int:
        return
    if kind is float:
        if not math.isfinite(obj):
            raise ValueError(f"{path}: non-finite float {obj!r} is not allowed in an artifact")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if type(key) is not str:
                raise TypeError(f"{path}: object keys must be str, got {type(key).__name__} {key!r}")
            _check_json_value(value, f"{path}.{key}")
        return
    if isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            _check_json_value(value, f"{path}[{i}]")
        return
    raise TypeError(f"{path}: {kind.__name__} is not a JSON type")


def canonical_json(obj) -> bytes:
    """sort_keys, compact separators, ASCII, shortest-repr floats; refuses NaN/Inf and non-JSON types."""
    _check_json_value(obj, "$")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def payload_hash(artifact) -> str:
    """SHA-256 hex of the canonical JSON of every field except ``sha256``."""
    if not isinstance(artifact, dict):
        raise TypeError("artifact must be a dict")
    payload = {k: v for k, v in artifact.items() if k != "sha256"}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


# --------------------------------------------------------------------------- structure
def validate_artifact(artifact, *, require_sha256=True) -> None:
    """Raise ValueError/TypeError if the artifact is structurally unacceptable."""
    if not isinstance(artifact, dict):
        raise TypeError("artifact must be a JSON object")
    required = REQUIRED_KEYS if require_sha256 else tuple(k for k in REQUIRED_KEYS if k != "sha256")
    missing = [k for k in required if k not in artifact]
    if missing:
        raise ValueError(f"missing required keys: {', '.join(missing)}")
    version = artifact["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")
    for key in _NON_EMPTY_STRINGS:
        value = artifact[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    try:
        datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"created_at is not an ISO-8601 timestamp: {artifact['created_at']!r}") from None
    for key in _DICTS:
        if not isinstance(artifact[key], dict):
            raise ValueError(f"{key} must be an object")
    if type(artifact["adoption"].get("adopted")) is not bool:
        raise ValueError("adoption.adopted must be true or false")
    features = artifact["features"]
    if not isinstance(features, (list, dict)) or not features:
        raise ValueError("features must be a non-empty list or object")
    if type(artifact["seed"]) is not int:
        raise ValueError("seed must be an integer")
    generator_sha = artifact["generator_sha256"]
    if not isinstance(generator_sha, str) or not _HEX64.fullmatch(generator_sha):
        raise ValueError("generator_sha256 must be 64 lowercase hex characters")
    if require_sha256:
        sha = artifact["sha256"]
        if not isinstance(sha, str) or not _HEX64.fullmatch(sha):
            raise ValueError("sha256 must be 64 lowercase hex characters")


# --------------------------------------------------------------------------- save / load
def save_artifact(path, artifact) -> str:
    """Validate, hash and write the artifact as canonical JSON. Returns the sha256 to pin.

    Any ``sha256`` already in ``artifact`` is ignored and replaced in the file;
    the caller's dict is not modified.
    """
    validate_artifact(artifact, require_sha256=False)
    body = {k: v for k, v in artifact.items() if k != "sha256"}
    digest = hashlib.sha256(canonical_json(body)).hexdigest()
    data = canonical_json(dict(body, sha256=digest)) + b"\n"
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact is {len(data)} bytes, over MAX_ARTIFACT_BYTES ({MAX_ARTIFACT_BYTES})")
    path = os.fspath(path)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return digest


def _reject_constant(name):
    raise _NonFiniteNumber(f"non-finite constant {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise _NonFiniteNumber(f"non-finite number {text} is not allowed")
    return value


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def load_artifact(path, *, expected_model_type: str, expected_sha256: str) -> dict:
    """Read, parse, validate and verify an artifact; raise ArtifactIntegrityError on any doubt."""
    if not isinstance(expected_sha256, str) or not _HEX64.fullmatch(expected_sha256):
        raise ValueError("expected_sha256 must be the 64 lowercase hex characters pinned in code")
    if not isinstance(expected_model_type, str) or not expected_model_type:
        raise ValueError("expected_model_type must be a non-empty string")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot read artifact: {exc.__class__.__name__}") from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactIntegrityError(f"artifact exceeds MAX_ARTIFACT_BYTES ({MAX_ARTIFACT_BYTES})")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactIntegrityError("artifact is not UTF-8 text") from None
    try:
        data = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                          object_pairs_hook=_no_duplicates)
    except (_NonFiniteNumber, _DuplicateKey) as exc:
        raise ArtifactIntegrityError(str(exc)) from None
    except (ValueError, RecursionError):
        raise ArtifactIntegrityError("artifact is not valid JSON") from None
    try:
        validate_artifact(data, require_sha256=True)
    except (ValueError, TypeError) as exc:
        raise ArtifactIntegrityError(f"malformed artifact: {exc}") from None
    if data["model_type"] != expected_model_type:
        raise ArtifactIntegrityError(
            f"model_type is {data['model_type']!r}, expected {expected_model_type!r}")
    try:
        recomputed = payload_hash(data)
    except (ValueError, TypeError) as exc:
        raise ArtifactIntegrityError(f"artifact cannot be hashed: {exc}") from None
    if not hmac.compare_digest(recomputed, data["sha256"]):
        raise ArtifactIntegrityError("contents do not match the embedded sha256 (edited or corrupted)")
    if not hmac.compare_digest(data["sha256"], expected_sha256):
        raise ArtifactIntegrityError(
            f"artifact sha256 {data['sha256'][:12]}... is not the hash pinned in code "
            f"({expected_sha256[:12]}...); a changed model needs a reviewed code change")
    return data
