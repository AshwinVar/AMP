"""AMP-native AI core: model artifacts, explanations, the test-set ledger, contracts.

WHY THIS EXISTS
---------------
A shipped AMP model is a JSON file. It must be impossible to change what that
file says - its weights, its metrics, or the ``adoption`` verdict that decides
whether a screen shows it - without a code change a reviewer can see. A SHA-256
stored inside the same file does not achieve that on its own: whoever edits the
file can recompute it. So ``load_artifact`` checks the embedded hash AND a hash
PINNED IN CODE by the caller, and this suite proves the second check is what
catches an edit whose embedded hash was recomputed.

It also pins the ways a JSON loader can be fooled into accepting a value the
canonical hash never saw: ``NaN``/``Infinity`` constants (``json.load`` accepts
them by default), ``1e999`` (parses to infinity without touching
``parse_constant``), duplicate keys (the last one silently wins), and a file big
enough to exhaust memory. Loading never executes anything: a file of Python
source is refused as invalid JSON and its side effect never happens.

The ledger section pins the guard against tuning on a test set: only the FIRST
evaluation of a test set for a given data hash can adopt a model.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_core_artifact.py
"""
import dataclasses
import json
import os
import shutil
import tempfile

from amp_ai.core import artifact as A
from amp_ai.core import contracts as C
from amp_ai.core.explain import linear_contributions
from amp_ai.core.ledger import first_run_for, record_test_evaluation
from amp_ai.core.logistic import BinaryLogisticRegression

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:  # noqa: BLE001
        return e
    return None


def make_artifact():
    return {
        "schema_version": 1,
        "model_type": "binary_logistic_regression",
        "model_name": "unit_test_model",
        "version": "1.0.0",
        "created_at": "2026-09-17T00:00:00+00:00",
        "training_data_source": "synthetic:unit-test seed=1",
        "consent_basis": "none required: no customer data used",
        "features": ["a", "b", "c"],
        "parameters": {"coef": [1.25, -0.5, 0.125], "intercept": -0.75},
        "metrics": {"roc_auc": 0.81},
        "baseline_metrics": {"roc_auc": 0.74},
        "adoption": {"adopted": False, "reasons": ["unit test"]},
        "seed": 1,
        "generator": "amp_ai.unit_test.synthetic@v1",
        "generator_sha256": "ab" * 32,
        "eval_ledger": {},
    }


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_text(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def load(path, sha, model_type="binary_logistic_regression"):
    return A.load_artifact(path, expected_model_type=model_type, expected_sha256=sha)


def integrity_error(path, sha, model_type="binary_logistic_regression"):
    return raises(A.ArtifactIntegrityError, load, path, sha, model_type)


def reason(err):
    return getattr(err, "reason", "") if err is not None else ""


def section_canonical_json():
    print("\n[canonical_json / payload_hash]")
    check("key order does not change the bytes",
          A.canonical_json({"b": 1, "a": [1.5, None, True]}) == A.canonical_json({"a": [1.5, None, True], "b": 1}))
    check("compact separators, shortest float repr", A.canonical_json({"x": 0.1}) == b'{"x":0.1}')
    check("non-ASCII escaped", A.canonical_json({"k": chr(0x2014)}) == b'{"k":"\\u2014"}')
    check("NaN refused", raises(ValueError, A.canonical_json, {"x": float("nan")}) is not None)
    check("Infinity refused", raises(ValueError, A.canonical_json, [float("inf")]) is not None)
    check("non-string key refused (int keys would silently become strings)",
          raises(TypeError, A.canonical_json, {1: "x"}) is not None)
    check("unsupported type refused", raises(TypeError, A.canonical_json, {"x": {1, 2}}) is not None)
    art = make_artifact()
    check("payload_hash ignores the sha256 field itself",
          A.payload_hash(art) == A.payload_hash(dict(art, sha256="f" * 64)))
    changed = make_artifact()
    changed["adoption"]["adopted"] = True
    check("payload_hash covers adoption (not just parameters)", A.payload_hash(art) != A.payload_hash(changed))
    check("REQUIRED_KEYS includes the provenance fields",
          {"training_data_source", "consent_basis", "generator_sha256", "eval_ledger", "sha256"} <= set(A.REQUIRED_KEYS))


def section_round_trip(tmp):
    print("\n[save_artifact / load_artifact: round trip]")
    art = make_artifact()
    path = os.path.join(tmp, "model.json")
    digest = A.save_artifact(path, art)
    check("save returns a 64-hex sha256", isinstance(digest, str) and len(digest) == 64)
    check("save does not mutate the caller's dict", "sha256" not in art)
    loaded = load(path, digest)
    check("load returns exactly what was saved plus its hash", loaded == dict(art, sha256=digest))
    check("the hash is the payload hash", digest == A.payload_hash(art))

    # git (core.autocrlf) or a human re-indenting the file changes BYTES, not the
    # payload. The pin must survive that, or every Windows checkout breaks it.
    pretty = json.dumps(loaded, indent=2, sort_keys=False).replace("\n", "\r\n") + "\r\n"
    reformatted = os.path.join(tmp, "model_crlf.json")
    write_text(reformatted, pretty)
    check("re-indented CRLF copy still loads against the same pin",
          raises(A.ArtifactIntegrityError, load, reformatted, digest) is None)

    check("save refuses NaN in parameters",
          raises(ValueError, A.save_artifact, os.path.join(tmp, "nan.json"),
                 dict(make_artifact(), parameters={"coef": [float("nan")]})) is not None)
    missing = make_artifact()
    del missing["consent_basis"]
    check("save refuses an artifact without consent_basis",
          raises(ValueError, A.save_artifact, os.path.join(tmp, "missing.json"), missing) is not None)
    blank = dict(make_artifact(), training_data_source="  ")
    check("save refuses a blank training_data_source",
          raises(ValueError, A.save_artifact, os.path.join(tmp, "blank.json"), blank) is not None)
    return path, digest


def section_tamper(tmp, path, digest):
    print("\n[load_artifact: tampering]")
    original = read_text(path)

    one_digit = os.path.join(tmp, "one_digit.json")
    check("fixture contains the parameter being edited", original.count("1.25") == 1)
    write_text(one_digit, original.replace("1.25", "1.26"))
    err = integrity_error(one_digit, digest)
    check("one changed parameter digit is refused", err is not None)
    check("... because the contents no longer match the embedded hash", "embedded" in reason(err), reason(err))

    # The attack an embedded hash alone cannot stop: edit metrics/adoption AND
    # recompute the embedded sha256 so the file is internally consistent.
    data = json.loads(original)
    data["metrics"]["roc_auc"] = 0.99
    data["adoption"]["adopted"] = True
    data["sha256"] = A.payload_hash(data)
    forged = os.path.join(tmp, "forged.json")
    with open(forged, "wb") as fh:
        fh.write(A.canonical_json(data))
    err = integrity_error(forged, digest)
    check("edited adoption with a RECOMPUTED embedded hash is refused", err is not None)
    check("... by the hash pinned in code", "pinned" in reason(err), reason(err))
    check("(the forged file is internally consistent: it loads against its own hash)",
          raises(A.ArtifactIntegrityError, load, forged, data["sha256"]) is None)

    missing = json.loads(original)
    del missing["consent_basis"]
    missing["sha256"] = A.payload_hash(missing)
    p = os.path.join(tmp, "missing_key.json")
    write_text(p, json.dumps(missing))
    err = integrity_error(p, missing["sha256"])
    check("a missing required key is refused", err is not None and "consent_basis" in reason(err), reason(err))

    bad_schema = json.loads(original)
    bad_schema["schema_version"] = 2
    bad_schema["sha256"] = A.payload_hash(bad_schema)
    p = os.path.join(tmp, "schema.json")
    write_text(p, json.dumps(bad_schema))
    check("an unknown schema_version is refused", integrity_error(p, bad_schema["sha256"]) is not None)

    no_verdict = json.loads(original)
    no_verdict["adoption"] = {"reasons": []}
    no_verdict["sha256"] = A.payload_hash(no_verdict)
    p = os.path.join(tmp, "no_verdict.json")
    write_text(p, json.dumps(no_verdict))
    check("adoption without a boolean 'adopted' is refused", integrity_error(p, no_verdict["sha256"]) is not None)

    err = integrity_error(path, digest, model_type="multinomial_logistic_regression")
    check("the wrong model_type is refused", err is not None and "model_type" in reason(err), reason(err))

    for token in ("NaN", "Infinity", "-Infinity"):
        p = os.path.join(tmp, f"const_{token}.json")
        check(f"fixture has the metric to replace ({token})", original.count("0.81") == 1)
        write_text(p, original.replace("0.81", token))
        err = integrity_error(p, digest)
        # "constant" is the parse-stage wording; the canonical-hash stage would say
        # "non-finite float". Checking the wording proves the parse guard fired.
        check(f"{token} in the file is refused at parse time",
              err is not None and "non-finite constant" in reason(err), reason(err))
    p = os.path.join(tmp, "overflow.json")
    write_text(p, original.replace("0.81", "1e999"))
    err = integrity_error(p, digest)
    check("1e999 (parses to inf without parse_constant) is refused at parse time",
          err is not None and "non-finite number" in reason(err), reason(err))

    p = os.path.join(tmp, "duplicate.json")
    check("fixture has the metrics object", original.count('"metrics":{') == 1)
    write_text(p, original.replace('"metrics":{', '"metrics":{"roc_auc":0.5,'))
    err = integrity_error(p, digest)
    check("duplicate keys are refused", err is not None and "duplicate" in reason(err), reason(err))

    p = os.path.join(tmp, "huge.json")
    with open(p, "wb") as fh:
        fh.write(b" " * A.MAX_ARTIFACT_BYTES + b"{}")
    err = integrity_error(p, digest)
    check("a file over MAX_ARTIFACT_BYTES is refused", err is not None and "MAX_ARTIFACT_BYTES" in reason(err), reason(err))

    p = os.path.join(tmp, "list.json")
    write_text(p, "[1, 2, 3]")
    check("a JSON array is refused", integrity_error(p, digest) is not None)

    check("a missing file is an integrity error (callers report model_unavailable)",
          integrity_error(os.path.join(tmp, "nope.json"), digest) is not None)
    check("a malformed pin is a programming error (ValueError)",
          raises(ValueError, load, path, "not-a-sha") is not None)


def section_never_executes(tmp):
    print("\n[load_artifact never executes content]")
    marker = os.path.join(tmp, "PWNED.txt")
    source = os.path.join(tmp, "source.json")
    write_text(source, "__import__('pathlib').Path(%r).write_text('pwned')\n" % marker)
    check("a file of Python source is refused", integrity_error(source, "0" * 64) is not None)
    check("... and its side effect never happened", not os.path.exists(marker))

    art = make_artifact()
    art["parameters"]["note"] = "__import__('pathlib').Path(%r).write_text('pwned')" % marker
    p = os.path.join(tmp, "code_in_string.json")
    digest = A.save_artifact(p, art)
    loaded = load(p, digest)
    check("code inside a string field stays an inert string",
          isinstance(loaded["parameters"]["note"], str) and not os.path.exists(marker))


def section_explain():
    print("\n[linear_contributions]")
    weights, z, names = [2.0, -1.0, 0.5], [1.0, 3.0, -4.0], ["a", "b", "c"]
    out = linear_contributions(weights, z, names, top=3)
    check("contribution = w_j * z_j, ranked by magnitude, ties by name",
          [(d["name"], d["contribution"]) for d in out] == [("b", -3.0), ("a", 2.0), ("c", -2.0)], str(out))
    check("direction labels", [d["direction"] for d in out] == ["decreases", "increases", "decreases"])
    check("value is the standardized input", [d["value"] for d in out] == [3.0, 1.0, -4.0])
    check("top truncates", len(linear_contributions(weights, z, names, top=1)) == 1)
    check("zero contribution has direction 'none'",
          linear_contributions([0.0], [5.0], ["x"], top=None)[0]["direction"] == "none")
    check("length mismatch refused", raises(ValueError, linear_contributions, [1.0], [1.0, 2.0], ["a"]) is not None)

    model = BinaryLogisticRegression.from_dict({
        "model": "binary_logistic_regression", "coef": weights, "intercept": -0.25, "l2": 1.0,
        "class_weight": None, "class_weights_used": [1.0, 1.0], "max_iter": 50, "tol": 1e-8,
        "n_iter": 3, "converged": True, "n_features": 3})
    every = linear_contributions(model.coef, z, names, top=None)
    logit = model.decision_function([z])[0]
    check("all contributions + intercept == the model's logit",
          abs(sum(d["contribution"] for d in every) + model.intercept - logit) < 1e-12)


def section_ledger():
    print("\n[ledger: a test set adopts at most once]")
    empty = {}
    h = "cd" * 32
    first = record_test_evaluation(empty, data_hash=h, test_set="test")
    check("recording does not mutate the input ledger", empty == {})
    check("first evaluation of (hash, test) is the first run", first_run_for(first, h, "test"))
    second = record_test_evaluation(first, data_hash=h, test_set="test")
    check("a second evaluation of the same test set is NOT the first run", not first_run_for(second, h, "test"))
    check("the earlier ledger object is unchanged", first_run_for(first, h, "test"))
    other = record_test_evaluation(second, data_hash=h, test_set="misspecification:weak_symptoms")
    check("a different test set is counted separately", first_run_for(other, h, "misspecification:weak_symptoms"))
    fresh = record_test_evaluation(second, data_hash="ef" * 32, test_set="test")
    check("a new data hash (fresh generator seed) starts a new count", first_run_for(fresh, "ef" * 32, "test"))
    check("nothing recorded -> not a first run (nothing to adopt)", not first_run_for({}, h, "test"))
    check("the ledger survives a JSON round trip",
          not first_run_for(json.loads(json.dumps(second)), h, "test"))
    check("a tampered count is refused",
          raises(ValueError, first_run_for, {"ledger_version": 1, "test_runs": {h: {"test": 0}}}, h, "test") is not None)
    check("an empty data hash is refused",
          raises(ValueError, record_test_evaluation, {}, data_hash="", test_set="test") is not None)


def section_contracts():
    print("\n[contracts]")
    refused = C.ConsentDecision(granted=False, capability=C.CAPABILITY_TELEMETRY_BASELINE,
                                reason="No Admin has enabled learning from telemetry.",
                                granted_by=None, granted_at=None)
    check("ConsentDecision is frozen",
          raises(dataclasses.FrozenInstanceError, setattr, refused, "granted", True) is not None)
    check("a decision without a reason is refused (the code must say why)",
          raises(ValueError, C.ConsentDecision, granted=False, capability="telemetry_baseline",
                 reason="", granted_by=None, granted_at=None) is not None)
    check("granted must be a real bool",
          raises(TypeError, C.ConsentDecision, granted=1, capability="telemetry_baseline",
                 reason="x", granted_by=None, granted_at=None) is not None)
    exc = C.ConsentRequired(refused)
    check("ConsentRequired carries its decision and reason", exc.decision is refused and str(exc) == refused.reason)
    granted = dataclasses.replace(refused, granted=True, reason="Granted by admin@example.test")
    check("ConsentRequired refuses a GRANTED decision", raises(ValueError, C.ConsentRequired, granted) is not None)
    check("the telemetry capability is a learning capability",
          C.LEARNING_CAPABILITIES == (C.CAPABILITY_TELEMETRY_BASELINE,) and C.CAPABILITY_TELEMETRY_BASELINE == "telemetry_baseline")

    class AlwaysRefuse:
        def check(self, db, tenant, capability):
            return refused

    check("an object with check() satisfies the ConsentGate protocol", isinstance(AlwaysRefuse(), C.ConsentGate))
    check("an object without check() does not", not isinstance(object(), C.ConsentGate))

    rd = C.RouteDecision(route="oee", confidence=0.93, top=[("oee", 0.93), ("downtime", 0.04)],
                         model_version="copilot_intent@1")
    check("RouteDecision is frozen", raises(dataclasses.FrozenInstanceError, setattr, rd, "route", "x") is not None)
    check("RouteDecision allows route=None", C.RouteDecision(None, 0.2, [], "v1").route is None)
    check("confidence outside [0,1] refused", raises(ValueError, C.RouteDecision, "oee", 1.5, [], "v1") is not None)


def main():
    print("=" * 74)
    print("AMP-native AI core: artifacts, explanations, ledger, contracts")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="amp_ai_artifact_")
    try:
        section_canonical_json()
        path, digest = section_round_trip(tmp)
        section_tamper(tmp, path, digest)
        section_never_executes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    section_explain()
    section_ledger()
    section_contracts()
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


def test_amp_ai_core_artifact():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
