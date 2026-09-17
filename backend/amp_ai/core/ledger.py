"""Evaluation ledger: a test set can adopt a model at most once.

THE FAILURE IT PREVENTS
-----------------------
Build, look at the test score, tweak, rebuild, look again. Each look is harmless
alone; after a dozen, the "held-out" score has been tuned on and no longer
predicts anything. Nothing about the final artifact reveals it.

So every evaluation on a test set is RECORDED against the hash of the data it
was drawn from (the synthetic generator's source hash, or the corpus hash), and
an adoption gate may only pass on the FIRST recorded evaluation for that
(data_hash, test_set). Evaluating again - for any reason - marks the result
``test_set_reused`` and the gate fails until the test set is genuinely fresh
(a new generator seed or version, or a new question set), which changes the
hash and starts a new count.

The ledger lives inside each eval JSON and artifact (``eval_ledger``), so it is
committed, reviewed and covered by the artifact's pinned hash.

WHAT IT CANNOT DO
-----------------
It is an AUDIT TRAIL of committed evaluations, not an enforcement. It counts
only runs that read a committed ledger and were themselves committed. Building
into another folder, calling a build function in-process with an empty prior
ledger, or reading the generated data directly re-scores a test set without
leaving a trace, and the reproduce suites do exactly that on every CI run, by
design. "The first recorded evaluation" therefore means the first COMMITTED one
(ADR-0020 section 3).

SHAPE
-----
    {"ledger_version": 1, "test_runs": {data_hash: {test_set: count}}}

``{}`` is an empty ledger. Functions never mutate their input.
"""
import copy

__all__ = ["LEDGER_VERSION", "record_test_evaluation", "first_run_for", "run_count"]

LEDGER_VERSION = 1


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _validated(ledger) -> dict:
    if not isinstance(ledger, dict):
        raise ValueError("ledger must be a dict")
    if not ledger:
        return {"ledger_version": LEDGER_VERSION, "test_runs": {}}
    if ledger.get("ledger_version") != LEDGER_VERSION or set(ledger) != {"ledger_version", "test_runs"}:
        raise ValueError("unrecognised ledger shape or version")
    runs = ledger["test_runs"]
    if not isinstance(runs, dict):
        raise ValueError("ledger.test_runs must be an object")
    for data_hash, per_set in runs.items():
        _nonempty(data_hash, "data_hash")
        if not isinstance(per_set, dict) or not per_set:
            raise ValueError(f"ledger entry for {data_hash[:12]} is malformed")
        for test_set, count in per_set.items():
            _nonempty(test_set, "test_set")
            if type(count) is not int or count < 1:
                raise ValueError(f"ledger count for ({data_hash[:12]}, {test_set}) must be an int >= 1")
    return copy.deepcopy(ledger)


def record_test_evaluation(ledger: dict, *, data_hash: str, test_set: str) -> dict:
    """A NEW ledger with one more evaluation of ``test_set`` drawn from ``data_hash``."""
    _nonempty(data_hash, "data_hash")
    _nonempty(test_set, "test_set")
    updated = _validated(ledger)
    per_set = updated["test_runs"].setdefault(data_hash, {})
    per_set[test_set] = per_set.get(test_set, 0) + 1
    return updated


def run_count(ledger, data_hash, test_set) -> int:
    _nonempty(data_hash, "data_hash")
    _nonempty(test_set, "test_set")
    return _validated(ledger)["test_runs"].get(data_hash, {}).get(test_set, 0)


def first_run_for(ledger, data_hash, test_set) -> bool:
    """True iff exactly ONE evaluation of (data_hash, test_set) is recorded.

    Call it AFTER recording the evaluation being judged. Zero recorded runs is
    False: there is no evaluation to adopt on.
    """
    return run_count(ledger, data_hash, test_set) == 1
