"""Post-hoc diagnostics of the SHIPPED failure-risk evaluation (v1): what the rule baseline had to work with,
and whether one raw input scores as well as the model.

    cd backend
    DATABASE_URL="sqlite:///./ci.db" python -m amp_ai.failure_risk.diagnose [--out FILE]

WHY THIS EXISTS
---------------
build.py adopted v1 because it beat the rule scorer on held-out synthetic machine-weeks. An adversarial
review (2026-09-17) showed that says less than it seems. This script is the runnable source of every number
the model card, ADR-0020 and the founder handbook quote about it:

  RULE COMPONENTS  the share of test machine-weeks on which each of the rule scorer's components fires. The
                   component list is read from predictive_engine's own source, not restated here.
  ONE RAW INPUT    the single feature whose direction and missing-value median come from TRAINING rows and
                   which has the highest PR-AUC on VALIDATION rows, scored on the test rows beside the model
                   and the rule, with paired 95% cluster-bootstrap intervals. This is the trivial baseline a
                   future gate should include.
  REVIEWER'S PICK  reject_rate_7d, which the reviewer chose while scanning all 18 features ON THE TEST SET.
                   Reported because it was quoted, labelled as chosen on test data; never a gate.
  SCOPE            the weekdays and hours of the as-of snapshots the evaluation used.

WHAT THIS IS NOT
----------------
* Not a new evaluation or verdict. v1's adoption stands as recorded; nothing is fitted beyond one sign and one
  median per feature. It is a post-hoc LOOK at the committed test set, made after adoption, and the file says
  so (``post_hoc: true``). build.py's ledger is an audit trail of committed builds and does not count it.
* Deterministic for the committed model, generator and seed. test_amp_ai_failure_risk_diagnostics.py re-runs
  it and compares with the committed file.

The committed output is hash-pinned (``DIAGNOSTICS_SHA256``), like a model artifact, because the model card
reads it. After re-running into the shipped location, pin the printed hash.
"""
import argparse
import ast
import hmac
import inspect
import json
import os
import statistics
import sys
import textwrap
import time
from collections import Counter

from ..core import metrics as M
from ..core.artifact import (MAX_ARTIFACT_BYTES, ArtifactIntegrityError, _DuplicateKey, _finite_float,
                             _no_duplicates, _NonFiniteNumber, _reject_constant, canonical_json, payload_hash)
from . import baseline_rule, build, predict, synthetic
from .features import FEATURE_NAMES

__all__ = ["REPORT", "DIAGNOSTICS_FILE", "DIAGNOSTICS_PATH", "DIAGNOSTICS_SHA256", "REVIEWER_PICK",
           "rule_components", "diagnose", "load_diagnostics", "write_diagnostics", "main"]

REPORT = "amp_ai.failure_risk post-hoc diagnostics"
DIAGNOSTICS_FILE = "failure_risk_v1.diagnostics.json"
DIAGNOSTICS_PATH = os.path.join(build.ARTIFACTS_DIR, DIAGNOSTICS_FILE)
# SHA-256 of the committed diagnostics' canonical payload. The card shows these numbers, so changing them is a
# reviewed code change, exactly like a model.
DIAGNOSTICS_SHA256 = "c06b9e9f52e181b35891b0848b38317deb3f9f1a2a71d9bc2ac5c85f08fb1904"
REVIEWER_PICK = "reject_rate_7d"
_FALLBACK_REASON = "no major risk indicators detected"
_RULE_COUNTERS = ("downtime_minutes", "downtime_events", "breakdown_events", "utilization", "reject_rate")


# --------------------------------------------------------------------------- the rule's components
def rule_components() -> list:
    """Every reason predictive_engine.calculate_predictive_risk can give, in source order, read from its source.

    Raises RuntimeError if fewer than ten are found: a scan that finds nothing must not report all-clear.
    """
    source = textwrap.dedent(inspect.getsource(baseline_rule.calculate_predictive_risk))
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "reasons"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.append((node.lineno, node.args[0].value))
    reasons = [text for _, text in sorted(found) if text != _FALLBACK_REASON]
    if len(reasons) < 10:
        raise RuntimeError(f"found only {len(reasons)} rule components in predictive_engine; the scan is broken")
    return reasons


def _rule_section(rows):
    components = rule_components()
    n = len(rows)
    fired = Counter(reason for row in rows for reason in set(row["reasons"]))
    unknown = sorted(set(fired) - set(components) - {_FALLBACK_REASON})
    if unknown:
        raise RuntimeError(f"the rule gave reasons its source does not contain: {unknown}")
    table = [{"reason": r, "fires_on_rows": fired.get(r, 0), "fires_on_share": fired.get(r, 0) / n}
             for r in components]
    scores = Counter(float(row["risk_score"]) for row in rows)
    modal_score, modal_rows = min(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "components": table,
        "always": [t["reason"] for t in table if t["fires_on_rows"] == n],
        "never": [t["reason"] for t in table if t["fires_on_rows"] == 0],
        "sometimes": [t["reason"] for t in table if 0 < t["fires_on_rows"] < n],
        "rows_with_no_component": fired.get(_FALLBACK_REASON, 0),
        "counters": {key: {"min": min(row[key] for row in rows), "max": max(row[key] for row in rows)}
                     for key in _RULE_COUNTERS},
        "score_distribution": {"distinct_scores": len(scores), "modal_score": modal_score, "modal_rows": modal_rows,
                               "modal_share": modal_rows / n},
        "work_orders": ("not part of a machine history: baseline_rule passes work_orders=[], so 'high active "
                        "work-order load' cannot fire in this evaluation"),
    }


# --------------------------------------------------------------------------- one raw input
def _feature_stats(rows, name):
    """(sign, median) from ``rows``: the direction a higher value points, and the value that fills a gap."""
    present = [r["features"][name] for r in rows if r["features"][name] is not None]
    if not present:
        return None, None
    median = float(statistics.median(present))
    values = [median if r["features"][name] is None else r["features"][name] for r in rows]
    roc = M.roc_auc([r["label"] for r in rows], values)
    return (1.0 if roc is None or roc >= 0.5 else -1.0), median


def _feature_scores(rows, name, sign, median):
    return [sign * (median if r["features"][name] is None else r["features"][name]) for r in rows]


def _compare(test, scores, model_p, rule, config):
    y = [s["label"] for s in test]
    machines = [s["machine_id"] for s in test]
    dates = [s["day"] for s in test]
    frac, n_boot, seed = config.precision_k_frac, config.n_boot, config.seed
    labelled = list(zip(y, dates))

    def pak_paired(pairs, s):
        return M.precision_at_k_grouped([t for t, _ in pairs], s, [d for _, d in pairs], frac)

    def paired(fn, a, b, first=y):
        r = M.paired_bootstrap_diff(fn, first, a, b, machines, n_boot, seed=seed)
        return {"estimate": r["estimate"], "lo": r["lo"], "hi": r["hi"], "n_boot": r["n_boot"],
                "n_valid": r["n_valid"]}

    return {
        "pr_auc": M.pr_auc(y, scores), "roc_auc": M.roc_auc(y, scores),
        "precision_at_k": M.precision_at_k_grouped(y, scores, dates, frac),
        "feature_minus_rule": {"pr_auc": paired(M.pr_auc, scores, rule)},
        "model_minus_feature": {"pr_auc": paired(M.pr_auc, model_p, scores),
                                "roc_auc": paired(M.roc_auc, model_p, scores),
                                "precision_at_k": paired(pak_paired, model_p, scores, first=labelled)},
    }


# --------------------------------------------------------------------------- run
def diagnose(config=build.FULL) -> dict:
    """The diagnostics document for the committed model on ``config``'s data (no sha256 yet)."""
    artifact, model = predict.load_model()
    data_hash = build.generator_sha256()
    if data_hash != artifact["generator_sha256"] or config.seed != artifact["seed"]:
        raise RuntimeError("the generator or seed differs from the committed model's; these diagnostics would "
                           "describe different data")
    fleet = synthetic.generate_fleet(config.n_machines, config.days, config.seed, "main")
    samples, _excluded = build.make_samples(fleet.histories, config)
    train, val, test = build.split_samples(samples, config)
    if len(test) != artifact["metrics"]["test"]["rows"]:
        raise RuntimeError("the regenerated test set is not the committed evaluation's test set")

    rule_rows = [baseline_rule.rule_row(s["history"], s["as_of"]) for s in test]
    rule = [float(r["risk_score"]) for r in rule_rows]
    model_p = [model.score(s["features"])["probability"] for s in test]

    y_val = [s["label"] for s in val]
    table = []
    for name in FEATURE_NAMES:
        sign, median = _feature_stats(train, name)
        val_pr = None if sign is None else M.pr_auc(y_val, _feature_scores(val, name, sign, median))
        table.append({"feature": name, "sign": sign, "train_median": median, "val_pr_auc": val_pr})
    ranked = [t for t in table if t["val_pr_auc"] is not None]
    selected = min(ranked, key=lambda t: (-t["val_pr_auc"], t["feature"]))
    pick = next(t for t in table if t["feature"] == REVIEWER_PICK)

    def evaluated(entry, chosen_on):
        scores = _feature_scores(test, entry["feature"], entry["sign"], entry["train_median"])
        return dict(_compare(test, scores, model_p, rule, config), feature=entry["feature"], sign=entry["sign"],
                    train_median=entry["train_median"], val_pr_auc=entry["val_pr_auc"], chosen_on=chosen_on)

    as_ofs = [synthetic.as_of_for_day(d) for d in build.as_of_days(config)]
    y = [s["label"] for s in test]
    return {
        "report": REPORT,
        "post_hoc": True,
        "made_after": ("failure_risk v1 was adopted by its build; these diagnostics change no verdict and "
                       "gate nothing"),
        "model_name": artifact["model_name"], "version": artifact["version"],
        "artifact_sha256": artifact["sha256"], "generator": artifact["generator"],
        "generator_sha256": data_hash, "seed": config.seed,
        "config": {"name": config.name, "n_machines": config.n_machines, "days": config.days,
                   "n_boot": config.n_boot, "test_start": config.test_start,
                   "precision_k_frac": config.precision_k_frac},
        "test": {"rows": len(test), "positives": sum(y), "prevalence": sum(y) / len(y),
                 "model_pr_auc": M.pr_auc(y, model_p), "rule_pr_auc": M.pr_auc(y, rule)},
        "rule": _rule_section(rule_rows),
        "single_feature": {
            "method": ("direction (sign of training ROC-AUC - 0.5) and missing-value median from TRAINING rows; "
                       "the feature with the highest VALIDATION PR-AUC is selected; scored once on the test rows"),
            "validation": table,
            "selected_on_validation": evaluated(selected, "validation"),
            "reviewer_pick": evaluated(pick, "the TEST set, by scanning all 18 features (post hoc; not a gate)"),
        },
        "evaluation_scope": {
            "snapshots": len(as_ofs), "step_days": config.step_days,
            "as_of_weekdays": sorted({a.strftime("%A") for a in as_ofs}),
            "as_of_times": sorted({a.strftime("%H:%M") for a in as_ofs}),
            "note": ("every training, validation and test row is a snapshot at this weekday and time; the "
                     "failure-risk endpoint scores at whatever moment it is called"),
        },
        "notes": [
            "Measured on synthetic machines from amp_ai/failure_risk/synthetic.py; not evidence about real plants.",
            "Intervals: 95% percentile cluster bootstrap by machine, the build's resample count and seed.",
        ],
    }


# --------------------------------------------------------------------------- files
def write_diagnostics(doc, path) -> str:
    doc = dict(doc)
    doc.pop("sha256", None)
    canonical_json(doc)                           # refuses NaN / non-JSON values before writing
    doc["sha256"] = payload_hash(doc)
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(text.encode("ascii"))
    os.replace(tmp, path)
    return doc["sha256"]


def load_diagnostics(path=None, *, expected_sha256=None) -> dict:
    """The committed diagnostics, verified: strict JSON, embedded hash, pinned hash, and the model they describe.

    Raises ArtifactIntegrityError on any doubt, like load_artifact.
    """
    path = DIAGNOSTICS_PATH if path is None else path
    expected = DIAGNOSTICS_SHA256 if expected_sha256 is None else expected_sha256
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot read diagnostics: {exc.__class__.__name__}") from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactIntegrityError("diagnostics file is too large")
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float,
                         object_pairs_hook=_no_duplicates)
    except (_NonFiniteNumber, _DuplicateKey) as exc:
        raise ArtifactIntegrityError(str(exc)) from None
    except (ValueError, RecursionError):
        raise ArtifactIntegrityError("diagnostics file is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("report") != REPORT or not isinstance(doc.get("sha256"), str):
        raise ArtifactIntegrityError("not a failure-risk diagnostics document")
    try:
        recomputed = payload_hash(doc)
    except (ValueError, TypeError) as exc:
        raise ArtifactIntegrityError(f"diagnostics cannot be hashed: {exc}") from None
    if not hmac.compare_digest(recomputed, doc["sha256"]):
        raise ArtifactIntegrityError("diagnostics contents do not match their embedded sha256")
    if not hmac.compare_digest(doc["sha256"], expected):
        raise ArtifactIntegrityError("diagnostics sha256 is not the hash pinned in code")
    if doc.get("artifact_sha256") != predict.ARTIFACT_SHA256:
        raise ArtifactIntegrityError("these diagnostics describe a different model than the one pinned")
    return doc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m amp_ai.failure_risk.diagnose", description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=DIAGNOSTICS_PATH, help="output file (default: the shipped diagnostics)")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    doc = diagnose()
    sha = write_diagnostics(doc, args.out)
    rule, single = doc["rule"], doc["single_feature"]
    print(f"diagnosed {doc['model_name']}@{doc['version']} on {doc['test']['rows']} test rows "
          f"in {time.perf_counter() - started:.1f} s")
    print(f"  rule components always firing: {rule['always']}")
    print(f"  rule components never firing:  {rule['never']}")
    for key in ("selected_on_validation", "reviewer_pick"):
        s = single[key]
        d = s["model_minus_feature"]["pr_auc"]
        print(f"  {key}: {s['feature']} PR-AUC {s['pr_auc']:.4f}; model - feature {d['estimate']:+.4f} "
              f"[{d['lo']:+.4f}, {d['hi']:+.4f}]")
    print(f"  wrote {args.out}")
    print(f"  pin in amp_ai/failure_risk/diagnose.py: DIAGNOSTICS_SHA256 = \"{sha}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
