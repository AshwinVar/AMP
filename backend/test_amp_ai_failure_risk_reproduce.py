"""The shipped failure-risk numbers are reproducible: rebuild from scratch and get them again.

WHY
---
Every accuracy figure AMP quotes for the native failure-risk model comes from
``amp_ai/artifacts/failure_risk_v1.eval.json``. A number nobody can regenerate
is a claim, not a measurement. This suite reruns the FULL build (400 machines x
400 days, seed 20260917, the committed created_at and the ledger as it stood
before the committed run) in-process and compares it with the committed files:

  1  TIME        the full build finishes within 90 s; if it ever does not, the
                 fleet is shrunk and the model rebuilt - the check is never
                 skipped
  2  METRICS     every metric, interval, count and calibration row equals the
                 committed value within 1e-6 (floats may differ in the last
                 bits across platforms' maths libraries; the pinned hash, not
                 this suite, guards the artifact's bytes)
  3  MODEL       the fitted parameters (standardiser, coefficients, bands) equal
                 the committed ones within 1e-6
  4  VERDICT     the adoption verdict, reasons, ledger, features and provenance
                 are identical

Reproducing into memory is verification, not a new evaluation: nothing is
written and the ledger is not advanced. A rebuild that is COMMITTED goes through
the CLI, which reads the committed ledger and records the new run.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_reproduce.py
"""
import json
import math
import os
import time

from amp_ai.core import artifact as A
from amp_ai.failure_risk import build as B
from amp_ai.failure_risk import predict as PR

TOLERANCE = 1e-6
BUDGET_SECONDS = 90.0
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def differences(a, b, path="$", out=None, limit=10):
    """Paths where a and b differ: floats beyond TOLERANCE, anything else unequal."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str):
        if a != b or type(a) is not type(b):
            out.append(f"{path}: {a!r} != {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            if a != b:
                out.append(f"{path}: {a} != {b}")
        elif not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=TOLERANCE):
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


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: reproduce the shipped model and its numbers")
    print("=" * 74)
    committed = A.load_artifact(PR.ARTIFACT_PATH, expected_model_type=PR.MODEL_TYPE,
                                expected_sha256=PR.ARTIFACT_SHA256)
    with open(os.path.join(os.path.dirname(PR.ARTIFACT_PATH), B.EVAL_FILE), encoding="utf-8") as fh:
        committed_eval = json.load(fh)

    print("\n1. Full rebuild")
    started = time.perf_counter()
    rebuilt = B.build(B.FULL, created_at=committed["created_at"], prior_ledger=committed_eval["ledger_before"])
    elapsed = time.perf_counter() - started
    print(f"     full build: {elapsed:.1f} s (committed build recorded {committed_eval['build_seconds']:.1f} s)")
    check(f"the full build finishes within {BUDGET_SECONDS:.0f} s", elapsed <= BUDGET_SECONDS, f"{elapsed:.1f} s")

    art = rebuilt.artifact
    print("\n2. Metrics")
    for key in ("metrics", "baseline_metrics"):
        diff = differences(art[key], committed[key])
        check(f"{key} equal the committed values within {TOLERANCE}", not diff, "; ".join(diff))
    diff = differences(rebuilt.eval_doc["training"], committed_eval["training"])
    check("model selection and training summary reproduce", not diff, "; ".join(diff))

    print("\n3. Model")
    diff = differences(art["parameters"], committed["parameters"])
    check(f"parameters equal the committed model within {TOLERANCE}", not diff, "; ".join(diff))

    print("\n4. Verdict and provenance")
    check("the adoption verdict and its reasons are identical",
          art["adoption"]["adopted"] == committed["adoption"]["adopted"]
          and art["adoption"]["reasons"] == committed["adoption"]["reasons"],
          f"{art['adoption']['reasons']} vs {committed['adoption']['reasons']}")
    diff = differences(art["adoption"], committed["adoption"])
    check(f"the gate's criteria values reproduce within {TOLERANCE}", not diff, "; ".join(diff))
    for key in ("eval_ledger", "features", "training_data_source", "consent_basis", "generator",
                "generator_sha256", "seed", "model_type", "model_name", "version", "created_at"):
        check(f"{key} is identical", art[key] == committed[key], f"{art[key]!r} vs {committed[key]!r}")
    same_bytes = rebuilt.sha256 == committed["sha256"]
    print(f"     payload hash {'identical' if same_bytes else 'differs in float bits'}: {rebuilt.sha256}")

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


def test_amp_ai_failure_risk_reproduce():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
