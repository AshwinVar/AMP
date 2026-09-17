"""The failure-risk disclosures are measured, reproducible and shown (ADR-0020, review round 1).

WHY
---
v1 "beat the rule" on synthetic data. The review found the rule barely works on that data and one raw input
ties the model. Those facts are now quoted on the model card, in ADR-0020 and in the handbook, so they are held
to the same standard as the model's own numbers:

  1  PINNED      the committed diagnostics load only against the hash pinned in diagnose.py and name the
                 pinned model; an edited number (even re-hashed) is refused
  2  REPRODUCE   re-running amp_ai.failure_risk.diagnose on the committed model, generator and seed gives the
                 committed document (floats within 1e-9)
  3  FOUND       the rule's component list is read from predictive_engine's source and every one of them is
                 accounted for; a scan that found nothing would report all-clear
  4  SHOWN       the failure_risk model card states the facts the diagnostics support (always/never-firing
                 components, the tie with one raw input, the Tuesday 10:00 scope, the calibration and
                 misspecification weaknesses), and says so plainly when the diagnostics cannot be verified

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_diagnostics.py
"""
import json
import math
import os
import shutil
import tempfile

from amp_ai import registry
from amp_ai.core.artifact import ArtifactIntegrityError, payload_hash
from amp_ai.failure_risk import diagnose as D
from amp_ai.failure_risk import predict

TOLERANCE = 1e-9
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def differences(a, b, path="$", out=None, limit=10):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str):
        if a != b or type(a) is not type(b):
            out.append(f"{path}: {a!r} != {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=TOLERANCE):
            out.append(f"{path}: {a!r} vs {b!r}")
    elif isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            out.append(f"{path}: keys differ {sorted(set(a) ^ set(b))}")
        for key in sorted(set(a) & set(b)):
            differences(a[key], b[key], f"{path}.{key}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            differences(x, y, f"{path}[{i}]", out, limit)
    else:
        out.append(f"{path}: {type(a).__name__} vs {type(b).__name__}")
    return out


def refused(fn):
    try:
        fn()
    except ArtifactIntegrityError as exc:
        return exc.reason
    return None


def section_pinned():
    print("\n1. The committed diagnostics are pinned")
    doc = D.load_diagnostics()
    check("the committed file loads against DIAGNOSTICS_SHA256", doc["sha256"] == D.DIAGNOSTICS_SHA256)
    check("...describes the pinned model", doc["artifact_sha256"] == predict.ARTIFACT_SHA256)
    check("...and says it is a post-hoc look that changes no verdict",
          doc["post_hoc"] is True and "no verdict" in doc["made_after"])
    folder = tempfile.mkdtemp(prefix="amp_ai_diag_")
    try:
        edited = json.loads(json.dumps(doc))
        edited["single_feature"]["selected_on_validation"]["model_minus_feature"]["pr_auc"]["lo"] = 0.01
        path = os.path.join(folder, "edited.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(edited, fh)
        check("an edited number with the old embedded hash is refused",
              refused(lambda: D.load_diagnostics(path)) is not None)
        edited["sha256"] = payload_hash(edited)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(edited, fh)
        reason = refused(lambda: D.load_diagnostics(path))
        check("...and so is one whose embedded hash was recomputed (the pin in code does not match)",
              reason is not None and "pinned" in reason, str(reason))
        other = json.loads(json.dumps(doc))
        other["artifact_sha256"] = "e" * 64
        other["sha256"] = payload_hash(other)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(other, fh)
        reason = refused(lambda: D.load_diagnostics(path, expected_sha256=other["sha256"]))
        check("diagnostics of a different model are refused even when their own hash is the one expected",
              reason is not None and "different model" in reason, str(reason))
        check("a missing file is refused, not treated as all-clear",
              refused(lambda: D.load_diagnostics(os.path.join(folder, "absent.json"))) is not None)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return doc


def section_reproduce(committed):
    print("\n2. Re-running the diagnostics reproduces the committed numbers")
    rebuilt = D.diagnose()
    committed = {k: v for k, v in committed.items() if k != "sha256"}
    diff = differences(rebuilt, committed)
    check(f"diagnose() equals the committed document within {TOLERANCE}", not diff, "; ".join(diff))
    return rebuilt


def section_found(doc):
    print("\n3. The rule's components come from predictive_engine and are all accounted for")
    components = D.rule_components()
    check("eleven components are found in predictive_engine's source", len(components) == 11, str(components))
    check("...including the two the review named", "high accumulated downtime" in components
          and "frequent downtime events" in components)
    rule = doc["rule"]
    check("every component is in exactly one of always / never / sometimes",
          sorted(rule["always"] + rule["never"] + rule["sometimes"]) == sorted(components))
    n = doc["test"]["rows"]
    check("'always' means every test row", all(c["fires_on_rows"] == n for c in rule["components"]
                                               if c["reason"] in rule["always"]))


def section_shown(doc):
    print("\n4. The model card says what the diagnostics support")
    card = registry.card("failure_risk")
    text = " ".join(card.get("limitations") or [])
    check("the card has a limitations list", isinstance(card.get("limitations"), list) and card["limitations"])
    for reason in doc["rule"]["always"] + doc["rule"]["never"]:
        check(f"...naming the rule component '{reason}'", reason in text)
    selected = doc["single_feature"]["selected_on_validation"]
    d = selected["model_minus_feature"]["pr_auc"]
    check("...naming the one raw input that ties the model, and that its interval includes zero",
          selected["feature"] in text and d["lo"] < 0 < d["hi"] and "includes zero" in text, text[:400])
    check("...and the reviewer's post-hoc pick, labelled as picked on the test set",
          D.REVIEWER_PICK in text and "test set" in text)
    scope = doc["evaluation_scope"]
    check("...the as-of scope (weekday and time)", all(w in text for w in scope["as_of_weekdays"])
          and all(t in text for t in scope["as_of_times"]))
    check("...the sparse, over-predicting calibration above 20%", "20%" in text and "calibration" in text)
    check("...and the stress test whose Brier score is not below the base rate", "shock_driven" in text)
    rows = [r for r in card.get("headline", []) if "one input alone" in r.get("baseline_name", "")]
    check("a headline row compares the model with that one input on the same rows",
          len(rows) == 1 and math.isclose(rows[0]["baseline"], selected["pr_auc"], abs_tol=1e-12)
          and rows[0]["difference_lo"] == d["lo"] and rows[0]["difference_hi"] == d["hi"], str(rows)[:300])
    stress = card.get("misspecification_rows") or []
    check("the card carries the stress-test rows in the headline shape",
          stress and all(isinstance(r.get("model"), (int, float)) and isinstance(r.get("baseline"), (int, float))
                         and r.get("dataset", "").startswith("stress test") for r in stress), str(stress)[:300])

    original = D.DIAGNOSTICS_SHA256
    D.DIAGNOSTICS_SHA256 = "f" * 64
    try:
        broken = registry.card("failure_risk")
    finally:
        D.DIAGNOSTICS_SHA256 = original
    text = " ".join(broken.get("limitations") or [])
    check("when the diagnostics fail verification the card SAYS so instead of dropping the caveats",
          "could not be verified" in text and not any("one input alone" in r.get("baseline_name", "")
                                                       for r in broken.get("headline", [])), text[:300])


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: post-hoc diagnostics are pinned, reproducible and shown")
    print("=" * 74)
    doc = section_pinned()
    section_reproduce(doc)
    section_found(doc)
    section_shown(doc)
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


def test_amp_ai_failure_risk_diagnostics():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
