"""Native copilot intent model: the build is reproducible, the committed artifact is the pinned one, and its adoption verdict follows from its own numbers.

WHY THIS EXISTS
---------------
Constraint 5 of the AMP-native AI build: every accuracy figure comes from a
runnable, seeded evaluation, compared with the existing baseline (the keyword
router in ai/assistant.py) on the SAME questions, and a model that does not beat
the baseline is not made the default. This suite holds the committed files to
that:

  * the committed artifact loads against the hash PINNED in classifier.py, and
    the eval JSON describes exactly that artifact;
  * `adoption` in both files equals the gate recomputed here from the stored
    metrics and ledger - nobody can flip `adopted` without also changing the
    numbers (and the pin);
  * the decision threshold tau, recomputed on the validation split with the
    committed weights, equals the stored one;
  * the corpus the artifact claims to be trained on is the corpus in the repo;
  * the eval JSON contains aggregate numbers only - no evaluation question text;
  * a `--small` build with a fixed created_at is bit-for-bit reproducible, and a
    second evaluation of the same test set is recorded in the ledger and fails
    the gate (the guard against tuning on a test set).

The gate, the threshold rule and the hybrid (model-else-keywords) rule are also
unit-tested on hand-built inputs, one broken condition at a time.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_intent_build.py
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from amp_ai.core import ledger as LG
from amp_ai.core.artifact import load_artifact, payload_hash
from amp_ai.core.contracts import RouteDecision
from amp_ai.core.text_features import normalize_text
from amp_ai.copilot_intent import build as B
from amp_ai.copilot_intent import classifier as C
from amp_ai.copilot_intent import corpus as K
from amp_ai.copilot_intent import labels as L

BACKEND = os.path.dirname(os.path.abspath(__file__))
failures = []
CREATED_AT = "2026-01-01T00:00:00+00:00"


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


# --------------------------------------------------------------------------- unit: tau
def section_tau():
    print("\n[select_tau: the smallest threshold whose covered accuracy is >= 0.9]")
    y = ["oee", "oee", "cost", "cost"]
    probas = [{"oee": 0.9, "cost": 0.1}, {"cost": 0.8, "oee": 0.2}, {"cost": 0.7, "oee": 0.3},
              {"cost": 0.6, "oee": 0.4}]
    t = B.select_tau(y, probas)
    check("worked example: tau = 0.81 (0.80 would admit the confident mistake)", t["tau"] == 0.81, str(t))
    check("worked example: coverage 1/4 and covered accuracy 1.0",
          t["coverage"] == 0.25 and t["accuracy_above"] == 1.0, str(t))
    check("worked example: qualified", t["qualified"] is True)
    all_right = B.select_tau(["oee", "cost"], [{"oee": 0.6, "cost": 0.4}, {"cost": 0.55, "oee": 0.45}])
    check("all predictions right: tau = 0.0, full coverage", all_right["tau"] == 0.0 and all_right["coverage"] == 1.0,
          str(all_right))
    all_wrong = B.select_tau(["oee", "cost"], [{"cost": 0.9, "oee": 0.1}, {"oee": 0.95, "cost": 0.05}])
    check("nothing reaches 0.9: tau = 1.0 and not qualified (model never used)",
          all_wrong["tau"] == 1.0 and all_wrong["qualified"] is False, str(all_wrong))
    check("the target accuracy is 0.9", B.TAU_TARGET_ACCURACY == 0.9)


# --------------------------------------------------------------------------- unit: hybrid
def section_hybrid():
    print("\n[hybrid: the model is consulted only where production would consult it]")
    confident = RouteDecision("oee", 0.97, [("oee", 0.97)], "v")
    abstain = RouteDecision(None, 0.4, [("oee", 0.4)], "v")
    check("machine-name answers are kept (they run before the model)",
          B.hybrid_label("machine_detail", confident) == "machine_detail")
    check("find answers are kept (they run before the model)", B.hybrid_label("find", confident) == "find")
    check("a confident model route replaces the keyword route", B.hybrid_label("cost", confident) == "oee")
    check("an abstaining model (below tau or __none__) leaves the keyword route",
          B.hybrid_label("cost", abstain) == "cost")


# --------------------------------------------------------------------------- unit: gate
def system(correct, n, f1):
    return {"accuracy": correct / n, "macro_f1": f1, "correct": correct}


def passing_metrics():
    def block(k_correct, h_correct, k_f1, h_f1, n, p):
        return {"n": n, "K": system(k_correct, n, k_f1), "M": system(h_correct, n, h_f1),
                "H": system(h_correct, n, h_f1), "mcnemar_h_vs_k": {"b": 10, "c": 1, "p_value": p}}
    return {
        "corpus_test": dict(block(300, 540, 0.45, 0.88, 600, 1e-9), corpus_sha256="a" * 64),
        "external": {
            "sha256": "b" * 64,
            "pool": block(38, 48, 0.55, 0.70, 64, 0.01),
            "section1_literal": block(9, 9, 1.0, 1.0, 9, 1.0),
            "section1b": block(14, 14, 0.8, 0.8, 17, 1.0),
            "holdout1_odd": block(15, 19, 0.6, 0.7, 26, 0.5),
            "holdout2_odd": block(15, 19, 0.6, 0.7, 26, 0.5),
            "section1c": block(8, 10, 0.6, 0.8, 12, 0.5),
        },
    }


def set_correct(block, who, correct):
    block[who].update(system(correct, block["n"], block[who]["macro_f1"]))


def first_ledger():
    ledger = {}
    for test_set in B.CORPUS_TEST_SETS:
        ledger = LG.record_test_evaluation(ledger, data_hash="a" * 64, test_set=test_set)
    for test_set in B.EXTERNAL_TEST_SETS:
        ledger = LG.record_test_evaluation(ledger, data_hash="b" * 64, test_set=test_set)
    return ledger


def section_gate():
    print("\n[adoption gate: every condition is load-bearing]")
    base = passing_metrics()
    ok = B.evaluate_gate(base, first_ledger())
    check("all conditions met: adopted", ok["adopted"] is True and ok["reasons"] == [], str(ok))

    def failing(label, mutate, ledger=None, needle=""):
        m = copy.deepcopy(base)
        mutate(m)
        g = B.evaluate_gate(m, ledger if ledger is not None else first_ledger())
        check(f"not adopted when {label}", g["adopted"] is False and any(needle in r for r in g["reasons"]), str(g))

    failing("H does not beat K on the corpus test split",
            lambda m: set_correct(m["corpus_test"], "H", 300), needle="corpus test")
    failing("H does not beat K on the external pool",
            lambda m: set_correct(m["external"]["pool"], "H", 38), needle="pool")
    failing("the pool gain is under 5 points (3/64 = 4.7)",
            lambda m: set_correct(m["external"]["pool"], "H", 41), needle="5 points")
    passes = copy.deepcopy(base)
    set_correct(passes["external"]["pool"], "K", 44)
    set_correct(passes["external"]["pool"], "H", 48)
    check("a pool gain of exactly 4/64 (6.25 points) passes the 5-point condition",
          B.evaluate_gate(passes, first_ledger())["adopted"] is True, str(B.evaluate_gate(passes, first_ledger())))
    edge = {"n": 100, "K": system(60, 100, 0.5), "H": system(65, 100, 0.5)}
    check("exactly +5 points (65/100 vs 60/100) counts as >= 5 despite float rounding",
          B.pool_gain_ok(edge) is True)
    failing("McNemar p >= 0.05 on the pool",
            lambda m: m["external"]["pool"]["mcnemar_h_vs_k"].update(p_value=0.05), needle="McNemar")
    failing("H misses one of section 1's literal cases",
            lambda m: set_correct(m["external"]["section1_literal"], "H", 8), needle="literal")
    failing("H macro-F1 is below K on the pool",
            lambda m: m["external"]["pool"]["H"].update(macro_f1=0.54), needle="macro-F1")
    failing("the external evaluation was skipped",
            lambda m: m.update(external={"skipped": "small"}), needle="external")
    reused = LG.record_test_evaluation(first_ledger(), data_hash="b" * 64, test_set="external_pool")
    failing("the external pool was evaluated before (ledger count 2)", lambda m: None, ledger=reused,
            needle="reused")
    reused_corpus = LG.record_test_evaluation(first_ledger(), data_hash="a" * 64, test_set="corpus_test")
    failing("the corpus test split was evaluated before", lambda m: None, ledger=reused_corpus, needle="reused")
    failing("the ledger has no record of this evaluation", lambda m: None, ledger={}, needle="reused")


# --------------------------------------------------------------------------- committed files
def section_committed():
    print("\n[committed artifact and eval JSON]")
    check("ARTIFACT_SHA256 is pinned as 64 hex characters",
          isinstance(C.ARTIFACT_SHA256, str) and len(C.ARTIFACT_SHA256) == 64)
    art = load_artifact(C.ARTIFACT_PATH, expected_model_type=C.MODEL_TYPE, expected_sha256=C.ARTIFACT_SHA256)
    check("the committed artifact loads against the pinned hash", art["sha256"] == C.ARTIFACT_SHA256)
    clf = C.load()
    check("classifier.load() returns the pinned artifact", clf.artifact["sha256"] == C.ARTIFACT_SHA256)
    with open(C.EVAL_PATH, encoding="utf-8") as fh:
        ev = json.load(fh)
    check("eval JSON names the committed artifact", ev["artifact_sha256"] == C.ARTIFACT_SHA256)
    check("eval JSON metrics == artifact metrics", ev["metrics"] == art["metrics"])
    check("eval JSON ledger == artifact ledger", ev["eval_ledger"] == art["eval_ledger"])
    recomputed = B.evaluate_gate(ev["metrics"], ev["eval_ledger"])
    check("adoption == the gate recomputed from the stored metrics and ledger",
          ev["adoption"] == recomputed == art["adoption"], f"stored={ev['adoption']} recomputed={recomputed}")
    check("classifier.adopted mirrors the artifact", clf.adopted is art["adoption"]["adopted"])
    check("baseline_metrics are the keyword router's numbers",
          art["baseline_metrics"]["external_pool"] == art["metrics"]["external"]["pool"]["K"]
          and art["baseline_metrics"]["corpus_test"] == art["metrics"]["corpus_test"]["K"])
    check("the consent basis is stated", art["consent_basis"] ==
          "none: AMP-authored synthetic corpus; tenant questions are never logged or learned from")
    check("the training data source names the corpus", "amp_ai.copilot_intent.corpus" in art["training_data_source"])
    check("the seed is the build seed", art["seed"] == B.SEED)

    corpus = K.build_corpus()
    check("generator_sha256 is the corpus in the repo", art["generator_sha256"] == K.corpus_sha256(corpus))
    check("the corpus_test ledger key is that corpus hash",
          art["metrics"]["corpus_test"]["corpus_sha256"] == art["generator_sha256"])

    model = art["parameters"]["model"]
    values = [v for row in model["weights"].values() for v in row] + list(model["bias"])
    check("shipped weights are rounded to WEIGHT_DECIMALS places (what was evaluated is what ships)",
          art["parameters"]["weight_decimals"] == B.WEIGHT_DECIMALS
          and all(round(v, B.WEIGHT_DECIMALS) == v for v in values) and len(values) > 1000, str(len(values)))
    check("the model's labels are LABELS in order", model["labels"] == list(L.LABELS))

    print("\n[tau recomputed on the validation split with the committed weights]")
    _train, val, _test = K.split_corpus(corpus, seed=K.SPLIT_SEED)
    probas = [clf.probabilities(e.text) for e in val]
    tau = B.select_tau([e.label for e in val], probas)
    check("tau matches", tau["tau"] == art["parameters"]["tau"], f"recomputed={tau} stored={art['parameters']['tau']}")
    check("stored tau selection matches", tau == art["parameters"]["tau_selection"])

    print("\n[the eval JSON reports aggregates only]")
    sets = B.load_external_question_sets(BACKEND)
    blob = normalize_text(json.dumps(ev))
    leaked = sum(1 for rows in sets.values() for q, _ in rows if len(normalize_text(q)) > 12
                 and normalize_text(q) in blob)
    check("no evaluation question text appears in the eval JSON", leaked == 0, f"{leaked} questions found")
    ext = art["metrics"]["external"]
    if "skipped" not in ext:
        sizes = {name: len(rows) for name, rows in B.external_datasets(sets).items()}
        check("dataset sizes match the loaded evaluation sets",
              all(ext[name]["n"] == n for name, n in sizes.items()), f"{sizes}")
        check("the pool is the two odd-index holdout halves plus section 1c",
              ext["pool"]["n"] == ext["holdout1_odd"]["n"] + ext["holdout2_odd"]["n"] + ext["section1c"]["n"])
    for name, block in [("corpus_test", art["metrics"]["corpus_test"])] + [
            (k, v) for k, v in ext.items() if isinstance(v, dict) and "K" in v]:
        consistent = all(abs(block[s]["accuracy"] - block[s]["correct"] / block["n"]) < 1e-12 for s in ("K", "M", "H"))
        check(f"{name}: accuracy == correct / n for K, M and H", consistent)
    check("the build recorded its time budget", "timings_seconds" in ev and ev["timings_seconds"]["total"] > 0)


# --------------------------------------------------------------------------- small build
def run_small(out_dir):
    env = dict(os.environ, PYTHONHASHSEED="random", PYTHONIOENCODING="utf-8")
    started = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "amp_ai.copilot_intent.build", "--small", "--out", out_dir,
                        "--created-at", CREATED_AT], cwd=BACKEND, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r, time.perf_counter() - started


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def section_small_build():
    print("\n[--small build: reproducible, ledger-guarded, never touches the committed files]")
    committed = {p: digest(p) for p in (C.ARTIFACT_PATH, C.EVAL_PATH)}
    d1 = tempfile.mkdtemp(prefix="intent_small_a_")
    d2 = tempfile.mkdtemp(prefix="intent_small_b_")
    try:
        r1, t1 = run_small(d1)
        check("small build #1 exits 0", r1.returncode == 0, (r1.stdout + r1.stderr)[-800:])
        r2, t2 = run_small(d2)
        check("small build #2 exits 0", r2.returncode == 0, (r2.stdout + r2.stderr)[-800:])
        check("each small build takes under 60 s", t1 < 60 and t2 < 60, f"{t1:.1f}s {t2:.1f}s")
        a1 = os.path.join(d1, B.ARTIFACT_FILE)
        a2 = os.path.join(d2, B.ARTIFACT_FILE)
        if not (os.path.exists(a1) and os.path.exists(a2)):
            check("small builds wrote their artifacts", False)
            return
        with open(a1, encoding="utf-8") as fh:
            art1 = json.load(fh)
        with open(a2, encoding="utf-8") as fh:
            art2 = json.load(fh)
        check("same seed + same created_at => identical artifact hash (different PYTHONHASHSEED)",
              art1["sha256"] == art2["sha256"] == payload_hash(art1), f"{art1['sha256'][:12]} {art2['sha256'][:12]}")
        check("the small artifact loads against its own hash",
              load_artifact(a1, expected_model_type=C.MODEL_TYPE, expected_sha256=art1["sha256"])["seed"] == B.SEED)
        check("the small artifact is not the committed one", art1["sha256"] != C.ARTIFACT_SHA256)
        check("a small build is never adopted (external sets are not evaluated)",
              art1["adoption"]["adopted"] is False and "skipped" in art1["metrics"]["external"])
        small_values = [v for row in art1["parameters"]["model"]["weights"].values() for v in row]
        check("a fresh build rounds the weights it ships (and evaluates)",
              len(small_values) > 100 and all(round(v, B.WEIGHT_DECIMALS) == v for v in small_values)
              and any(v != round(v, 3) for v in small_values), str(len(small_values)))
        check("a small build ledger records one corpus_test run",
              LG.run_count(art1["eval_ledger"], art1["generator_sha256"], "corpus_test") == 1)
        # In-process this time, so the build itself is measured by the coverage job (subprocesses are not).
        err3 = raises(Exception, B.build, out_dir=d1, small=True, created_at=CREATED_AT, log=lambda *_: None)
        check("small build #3 (same output dir, in-process) completes", err3 is None, repr(err3))
        with open(a1, encoding="utf-8") as fh:
            art3 = json.load(fh)
        check("re-evaluating the same test set is recorded (count 2)",
              LG.run_count(art3["eval_ledger"], art3["generator_sha256"], "corpus_test") == 2)
        check("...and the gate says the test set was reused",
              any("reused" in r for r in art3["adoption"]["reasons"]), str(art3["adoption"]))
        check("...and the artifact hash changed with the ledger", art3["sha256"] != art1["sha256"])
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)
    check("the committed artifact and eval JSON were not modified by small builds",
          all(digest(p) == h for p, h in committed.items()))


def write_harness_files(root, *, planted=None, section1_label="oee", drop=None):
    """Minimal stand-ins for the three evaluation files, with made-up questions (never the real ones)."""
    def rows(prefix, n, label="oee"):
        return [(f"{prefix} made up question number {i} about zebra kiosks", label) for i in range(n)]
    s1 = rows("s1", 9, section1_label)
    if planted:
        s1[0] = (planted, "oee")
    parts = {
        "ROUTING_CASES": s1, "PARAPHRASES": rows("s1b", 5), "UNSEEN": rows("s1c", 4),
    }
    body = "def main():\n" + "".join(
        f"    {name} = {value!r}\n" for name, value in parts.items() if name != drop) + "    return 0\n"
    with open(os.path.join(root, "test_ai_evaluation.py"), "w", encoding="utf-8") as fh:
        fh.write(body)
    for filename, prefix in (("test_ai_routing_holdout.py", "h1"), ("test_ai_routing_holdout2.py", "h2")):
        with open(os.path.join(root, filename), "w", encoding="utf-8") as fh:
            fh.write(f"QUESTIONS = {rows(prefix, 6)!r}\n")


def section_loader_and_refusals():
    print("\n[the evaluation-set loader finds every set or refuses]")
    root = tempfile.mkdtemp(prefix="intent_sets_")
    try:
        write_harness_files(root)
        sets = B.load_external_question_sets(root)
        check("stand-in files load: five sets with the expected sizes",
              {k: len(v) for k, v in sets.items()} ==
              {"section1": 9, "section1b": 5, "section1c": 4, "holdout1": 6, "holdout2": 6}, str(sets.keys()))
        data = B.external_datasets(sets)
        check("odd-index halves and the pool are built from them",
              len(data["holdout1_odd"]) == 3 and data["holdout1_odd"][0] == sets["holdout1"][1]
              and data["pool"] == data["holdout1_odd"] + data["holdout2_odd"] + data["section1c"])
        check("the external hash depends on the questions", B.external_sha256(sets) != B.external_sha256(
            dict(sets, section1c=sets["section1c"][:-1])))
        write_harness_files(root, drop="UNSEEN")
        check("a missing set is refused (BuildError)",
              raises(B.BuildError, B.load_external_question_sets, root) is not None)
        write_harness_files(root, section1_label="drop_tables")
        check("a label outside the allowlist is refused",
              raises(B.BuildError, B.load_external_question_sets, root) is not None)
        write_harness_files(root)
        with open(os.path.join(root, "test_ai_routing_holdout.py"), "a", encoding="utf-8") as fh:
            fh.write("QUESTIONS = QUESTIONS + []\n")
        check("a set assigned twice is refused (ambiguous)",
              raises(B.BuildError, B.load_external_question_sets, root) is not None)
        write_harness_files(root)
        with open(os.path.join(root, "test_ai_routing_holdout2.py"), "w", encoding="utf-8") as fh:
            fh.write("QUESTIONS = make_questions()\n")
        check("a set that is not a literal is refused (nothing is executed)",
              raises(B.BuildError, B.load_external_question_sets, root) is not None)

        print("\n[the build refuses a corpus that copies an evaluation question]")
        write_harness_files(root, planted=K.build_corpus()[123].text)
        out = os.path.join(root, "out")
        err = raises(B.BuildError, B.build, out_dir=out, small=True, created_at=CREATED_AT, backend_dir=root,
                     log=lambda *_: None)
        check("build() raises BuildError on leakage", err is not None and "leakage" in str(err), str(err))
        check("...and writes nothing", not os.path.exists(os.path.join(out, B.ARTIFACT_FILE)))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n[the ledger continues across builds and cannot be silently reset]")
    out = tempfile.mkdtemp(prefix="intent_ledger_")
    try:
        ledger = LG.record_test_evaluation({}, data_hash="c" * 64, test_set="corpus_test")
        for filename in (B.EVAL_FILE, B.ARTIFACT_FILE):
            with open(os.path.join(out, filename), "w", encoding="utf-8") as fh:
                json.dump({"eval_ledger": ledger}, fh)
        check("both files agree: the ledger continues", B._prior_ledger(out) == ledger)
        os.remove(os.path.join(out, B.EVAL_FILE))
        check("eval JSON deleted: the ledger continues from the artifact", B._prior_ledger(out) == ledger)
        with open(os.path.join(out, B.EVAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"eval_ledger": {}}, fh)
        check("the two files disagree: refused",
              raises(B.BuildError, B._prior_ledger, out) is not None)
        with open(os.path.join(out, B.EVAL_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        check("an unreadable eval JSON is refused, not treated as an empty ledger",
              raises(B.BuildError, B._prior_ledger, out) is not None)
        # Both files carry the SAME malformed ledger, so only the shape check (not "disagree") can refuse it.
        for filename in (B.EVAL_FILE, B.ARTIFACT_FILE):
            with open(os.path.join(out, filename), "w", encoding="utf-8") as fh:
                json.dump({"eval_ledger": {"ledger_version": 99, "test_runs": {}}}, fh)
        err = raises(B.BuildError, B._prior_ledger, out)
        check("a malformed ledger is refused (both files agree on it)",
              err is not None and "disagree" not in str(err), str(err))
        empty = tempfile.mkdtemp(prefix="intent_ledger_empty_")
        check("no previous files: an empty ledger", B._prior_ledger(empty) == {})
        shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def section_artifact_tamper():
    print("\n[a tampered artifact is refused even with its embedded hash recomputed]")
    from amp_ai.core.artifact import ArtifactIntegrityError, save_artifact

    d = tempfile.mkdtemp(prefix="intent_tamper_")
    try:
        art = copy.deepcopy(C.load().artifact)
        art["parameters"]["tau"] = 0.0
        art["adoption"] = {"adopted": True, "reasons": []}
        path = os.path.join(d, "tampered.json")
        save_artifact(path, art)   # recomputes the embedded sha256, so only the pin can catch this
        err = raises(ArtifactIntegrityError, C.load, path)
        check("classifier.load(tampered) raises ArtifactIntegrityError (pinned hash)", err is not None)
        check("the default load still works afterwards", C.load().artifact["sha256"] == C.ARTIFACT_SHA256)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 74)
    print("AMP-native copilot intent: build, artifact and adoption gate")
    print("=" * 74)
    section_tau()
    section_hybrid()
    section_gate()
    section_loader_and_refusals()
    section_committed()
    section_artifact_tamper()
    section_small_build()
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


def test_amp_ai_intent_build():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
