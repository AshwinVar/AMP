"""Score the last hour of one machine's telemetry against its own recent baseline.

THE ORDER IS THE AUTHORIZATION CHAIN
------------------------------------
USER -> AUTHENTICATION -> RBAC happen in the route before this is called. Then:

1. TENANT: the machine must belong to ``tenant`` (explicit filter). If not,
   ``MachineNotFound`` - BEFORE the consent gate is consulted, so another
   tenant's machine id reveals nothing about anyone's consent settings.
2. CONSENT: fitting a baseline from a tenant's telemetry is learning from that
   tenant's data, so ``gate.check(db, tenant, "telemetry_baseline")`` must grant
   it. A refusal (or a grant for any other capability) raises
   ``ConsentRequired`` carrying the gate's reason, and no telemetry is read.
3. EVALUATION: the committed evaluation card must verify against the hash
   pinned below, and describe the method this code runs. Otherwise the answer is
   ``model_unavailable``; the code never guesses.
4. DATA -> MODEL: bounded, tenant-filtered reads (``db_telemetry``), buckets
   (``series``), per-request baseline and score (``baseline``).

The model never chooses a tenant, a query or a role. Nothing is persisted or
cached: the baseline exists for the duration of the call, so revoking consent
takes effect on the next request and there is no tenant-derived state to leak.

``gate`` is required and keyword-only. The HTTP route must pass the
database-backed consent gate; nothing here accepts readings supplied by a
caller, so the scored data is always the tenant's own stored telemetry.

ADOPTED OR EXPERIMENTAL
-----------------------
``evaluation.adopted`` comes from the committed, hash-pinned evaluation. When it
is False the score is ``experimental`` and must never be presented as an alert.
Either way it was evaluated on synthetic machines only.
"""
from pathlib import Path

import models

from ..core.artifact import ArtifactIntegrityError, load_artifact
from ..core.contracts import CAPABILITY_TELEMETRY_BASELINE, ConsentDecision, ConsentGate, ConsentRequired
from . import baseline as B
from . import db_telemetry as DT
from . import series as SR

__all__ = ["MODEL_TYPE", "MODEL_VERSION", "EVAL_ARTIFACT_PATH", "EVAL_SHA256", "CAVEAT", "MachineNotFound",
           "evaluation_card", "score_machine"]

MODEL_TYPE = "telemetry_anomaly"
MODEL_VERSION = "telemetry_anomaly@v1"
EVAL_ARTIFACT_PATH = str(Path(__file__).resolve().parent.parent / "artifacts" / "telemetry_anomaly_v1.eval.json")
# The SHA-256 of the committed evaluation's canonical payload. Replacing the
# evaluation (a new verdict, new metrics) means changing this line in review.
EVAL_SHA256 = "905d7a448090bb62519e16e9940e4e47bff0021508683e47376356a8db1a9fad"
CAVEAT = "Evaluated on synthetic machines only; not evidence of accuracy on real plants."


class MachineNotFound(LookupError):
    """No machine with this id belongs to the tenant."""


def evaluation_card(path=None, expected_sha256=None) -> dict:
    """The committed evaluation's verdict, verified against the pinned hash. Never raises for a bad file."""
    path = EVAL_ARTIFACT_PATH if path is None else path
    try:
        artifact = load_artifact(path, expected_model_type=MODEL_TYPE,
                                 expected_sha256=expected_sha256 or EVAL_SHA256)
    except ArtifactIntegrityError as exc:
        return {"available": False, "reason": exc.reason}
    if artifact["parameters"] != B.method_parameters():
        return {"available": False,
                "reason": "the evaluated method parameters differ from the method this code runs"}
    adoption = artifact["adoption"]
    return {
        "available": True,
        "adopted": adoption["adopted"],
        "experimental": not adoption["adopted"],
        "reasons": list(adoption.get("reasons", [])),
        "version": artifact["version"],
        "sha256": artifact["sha256"],
        "caveat": CAVEAT,
    }


def _window(start_used, window):
    return {"start": start_used.isoformat(), "end": window.end.isoformat(), "requested_start": window.start.isoformat()}


def score_machine(db, tenant, machine_id, *, gate, now=None) -> dict:
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("tenant must be a non-empty string")
    if not isinstance(gate, ConsentGate):
        raise TypeError("gate must implement ConsentGate.check(db, tenant, capability)")

    owned = (db.query(models.Machine.id)
             .filter(models.Machine.id == machine_id, models.Machine.tenant_code == tenant)
             .first())
    if owned is None:
        raise MachineNotFound(f"machine {machine_id!r} not found")

    decision = gate.check(db, tenant, CAPABILITY_TELEMETRY_BASELINE)
    if not isinstance(decision, ConsentDecision):
        raise TypeError("the consent gate must return a ConsentDecision")
    if decision.capability != CAPABILITY_TELEMETRY_BASELINE:
        decision = ConsentDecision(granted=False, capability=CAPABILITY_TELEMETRY_BASELINE,
                                   reason="the consent gate answered for a different capability",
                                   granted_by=None, granted_at=None)
    if not decision.granted:
        raise ConsentRequired(decision)

    card = evaluation_card()
    if not card["available"]:
        return {"status": "model_unavailable", "score": None, "machine_id": machine_id,
                "reason": card["reason"], "model_version": MODEL_VERSION}

    data = DT.load(db, tenant, machine_id, now=now)
    baseline_buckets = SR.build_buckets(data.baseline_readings, data.events, data.initial_running,
                                        first_k=SR.SCORE_BUCKETS, last_k=SR.TOTAL_BUCKETS - 1)
    score_buckets = SR.build_buckets(data.score_readings, data.events, data.initial_running,
                                     first_k=0, last_k=SR.SCORE_BUCKETS - 1)
    result = B.assess(baseline_buckets, score_buckets,
                      integer_signals=SR.integer_valued_signals(data.baseline_readings))
    result.update({
        "machine_id": machine_id,
        "score_window": _window(data.score_start_used, data.score_window),
        "baseline_window": _window(data.baseline_start_used, data.baseline_window),
        "truncated": data.truncated,
        "simulated_source": data.simulated_source,
        "model_version": MODEL_VERSION,
        "evaluation": {k: card[k] for k in ("adopted", "experimental", "reasons", "version", "caveat")},
    })
    return result
