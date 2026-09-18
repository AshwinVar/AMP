"""Train, evaluate and gate the AMP-native copilot intent model; write the artifact and its eval JSON.

    cd backend
    python -m amp_ai.copilot_intent.build                 # full build -> amp_ai/artifacts/
    python -m amp_ai.copilot_intent.build --small --out DIR --created-at 2026-01-01T00:00:00+00:00

WHAT IT DOES, IN ORDER
----------------------
1. Expands the AMP-authored corpus (``corpus.build_corpus``) and refuses to go on
   if any sentence copies a question from the repo's evaluation sets
   (``corpus.leakage_report``; counts only, no question is printed).
2. Splits it by FAMILY, 70/15/15 per label (``corpus.split_corpus``).
3. Featurises with ``classifier.featurize`` (the runtime's own function) and fits
   ``MultinomialLogisticRegression`` for every entry of ``GRID``, early-stopped on
   validation loss. The entry with the best validation macro-F1 is the model;
   there is no refit, so validation stays unseen by the weights.
4. Chooses the threshold ``tau`` on VALIDATION only (``select_tau``).
5. Evaluates three systems on the SAME questions:
      K  the keyword router: ``ai.assistant.answer(...)["matched"]``, run through
         each harness's OWN seeded in-memory database and call
         (test_ai_evaluation.ask, test_ai_routing_holdout[2].route);
      M  the model's argmax label;
      H  what production would do: the router's machine-name and find answers
         stand; otherwise the model's route when it is confident (>= tau, not
         ``__none__``); otherwise K.
   Datasets: (i) the corpus test families; and - full build only - the repo's
   questions: section 1 (literal), 1b (reported only: it guided the router's
   design), 1c, and the odd-index held-out halves of both holdout files. The
   POOL is holdout1_odd + holdout2_odd + section1c.
6. Records every test-set evaluation in the ledger (read from the previous eval
   JSON in the output directory, so re-running INTO THE SAME DIRECTORY counts;
   a build into another directory starts from that directory's ledger and is
   not seen - the ledger is an audit trail of committed runs, not a lock),
   applies the adoption gate (``evaluate_gate``) and writes both files.

HOW TO READ THE CORPUS TEST NUMBERS
-----------------------------------
The corpus was written by AMP with the router's keyword table in view (see the
disclosure in corpus.py), and its ``__none__`` sentences can never be "right" for
K or H, which always answer with some pillar. The corpus test split is therefore
favourable to the model and is reported as a difference, never as an accuracy
claim. The external pool is the honest comparison, and it is small (64
questions): a real gain can still fail the significance condition, which is
reported plainly and never tuned around. The remedy is a fresh question set.

EVALUATION HISTORY OF THE COMMITTED ARTIFACT (v1)
-------------------------------------------------
The full build was run ONCE (ledger count 1 for every test set), after the
hyper-parameter grid, epochs and corpus cap had been fixed from train/validation
probes only. Before that: ``--small`` builds (a smaller corpus with its own hash
but the same test FAMILIES) were run to check the pipeline without displaying
their metrics, and a timing probe ran the keyword
router over 300 corpus-test sentences, printing only its predicted-label counts
(no accuracy, no model). After it, two gate conditions were merged (one was
implied by the other) and a redundant classifier check was removed; neither
changes any prediction or verdict, which the build suite re-verifies from the
stored metrics.

WHAT IT NEVER DOES
------------------
No tenant data, no network, no pickle. The only database is the harnesses'
in-memory SQLite, used to run the keyword router. Nothing printed or written
contains an evaluation question.
"""
import argparse
import ast
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

from ..core import ledger as LG
from ..core.artifact import SCHEMA_VERSION, canonical_json, save_artifact
from ..core.metrics import accuracy, confusion_matrix, macro_f1, mcnemar_exact, paired_bootstrap_diff
from ..core.multinomial import MultinomialLogisticRegression
from . import classifier as C
from . import corpus as K
from .labels import LABELS, NONE_LABEL, ROUTE_LABELS

__all__ = [
    "SEED", "ARTIFACT_FILE", "EVAL_FILE", "DEFAULT_OUT", "CONSENT_BASIS", "TAU_TARGET_ACCURACY", "TAU_STEPS",
    "GRID", "MAX_EPOCHS", "PATIENCE", "SMALL", "BOOTSTRAP_N", "WEIGHT_DECIMALS", "EXTERNAL_SETS",
    "EXTERNAL_DATASETS", "CORPUS_TEST_SETS", "EXTERNAL_TEST_SETS", "ROUTER_ONLY_LABELS", "METRIC_LABELS",
    "MIN_POOL_GAIN_POINTS", "MCNEMAR_ALPHA", "BuildError", "load_external_question_sets", "external_datasets",
    "external_sha256", "select_tau", "hybrid_label", "pool_gain_ok", "evaluate_gate", "score_block", "build",
    "main",
]

SEED = 20260917
ARTIFACT_FILE = os.path.basename(C.ARTIFACT_PATH)
EVAL_FILE = os.path.basename(C.EVAL_PATH)
DEFAULT_OUT = os.path.dirname(C.ARTIFACT_PATH)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSENT_BASIS = "none: AMP-authored synthetic corpus; tenant questions are never logged or learned from"

TAU_TARGET_ACCURACY = 0.9
TAU_STEPS = 100

# Hyper-parameters are chosen on validation macro-F1. Learning rate backs off by
# halving inside the solver, so it sets speed; L2 sets how far weights may grow.
# Sized to the 90 s build budget: a train/validation-only timing run measured
# about 0.37 s per epoch at 2493 training sentences, and validation macro-F1 was
# still rising slowly at 70 epochs (no early stop).
GRID = ({"l2": 1e-5, "lr": 32.0}, {"l2": 1e-4, "lr": 32.0})
MAX_EPOCHS = 100
PATIENCE = 5
# --small: a quick, deterministic smoke build. It never evaluates the external sets, so it is never adopted.
SMALL = {"cap": 6, "max_epochs": 12, "grid": ({"l2": 1e-4, "lr": 32.0},)}
BOOTSTRAP_N = 1000
# Shipped weights are rounded to this many decimal places BEFORE threshold selection and evaluation.
WEIGHT_DECIMALS = 6

EXTERNAL_SETS = ("section1", "section1b", "section1c", "holdout1", "holdout2")
EXTERNAL_DATASETS = ("section1_literal", "section1b", "section1c", "holdout1_odd", "holdout2_odd", "pool")
CORPUS_TEST_SETS = ("corpus_test",)
EXTERNAL_TEST_SETS = ("external_pool", "section1_literal", "section1b")
# Answers the router gives BEFORE the model is consulted; the model can never produce them.
ROUTER_ONLY_LABELS = ("machine_detail", "find")
METRIC_LABELS = LABELS + ROUTER_ONLY_LABELS
MIN_POOL_GAIN_POINTS = 5
MCNEMAR_ALPHA = 0.05

# name -> (file, enclosing function or None for module level, variable)
_SOURCES = {
    "section1": ("test_ai_evaluation.py", "main", "ROUTING_CASES"),
    "section1b": ("test_ai_evaluation.py", "main", "PARAPHRASES"),
    "section1c": ("test_ai_evaluation.py", "main", "UNSEEN"),
    "holdout1": ("test_ai_routing_holdout.py", None, "QUESTIONS"),
    "holdout2": ("test_ai_routing_holdout2.py", None, "QUESTIONS"),
}
# which harness's seeded database and call answers each set with the keyword router
_HARNESS_FOR_SET = {"section1": "evaluation", "section1b": "evaluation", "section1c": "evaluation",
                    "holdout1": "holdout1", "holdout2": "holdout2"}
# the corpus test split names no machine; it is routed on the second holdout harness's plant
_CORPUS_HARNESS = "holdout2"


class BuildError(RuntimeError):
    """The build cannot produce a trustworthy artifact (missing sets, leakage, a malformed ledger...)."""


# =========================================================================== external question sets
def _assignments(scope_nodes, variable):
    found = []
    for node in scope_nodes:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
            found.append(node)
    return found


def load_external_question_sets(backend_dir=BACKEND_DIR) -> dict:
    """{set name: [(question, expected pillar), ...]} parsed from the harness files' source.

    Parsed with ``ast`` and ``ast.literal_eval`` (literals only; nothing is imported or
    executed). Every set must be found exactly once, be non-empty, and name only
    allowlisted pillars - a loader that finds nothing would make every later check vacuous.
    """
    out = {}
    trees = {}
    for name in EXTERNAL_SETS:
        filename, function, variable = _SOURCES[name]
        path = os.path.join(backend_dir, filename)
        if path not in trees:
            try:
                with open(path, encoding="utf-8") as fh:
                    trees[path] = ast.parse(fh.read(), filename=filename)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                raise BuildError(f"cannot read evaluation file {filename}: {exc.__class__.__name__}") from None
        tree = trees[path]
        if function is None:
            scope = tree.body
        else:
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
            if len(functions) != 1:
                raise BuildError(f"{filename}: expected one function {function}(), found {len(functions)}")
            scope = list(ast.walk(functions[0]))
        nodes = _assignments(scope, variable)
        if len(nodes) != 1:
            raise BuildError(f"{filename}: expected one assignment to {variable}, found {len(nodes)}")
        try:
            rows = ast.literal_eval(nodes[0].value)
        except (ValueError, SyntaxError):
            raise BuildError(f"{filename}: {variable} is not a literal list") from None
        if not isinstance(rows, list) or not rows:
            raise BuildError(f"{filename}: {variable} must be a non-empty list")
        clean = []
        for row in rows:
            if (not isinstance(row, (tuple, list)) or len(row) != 2 or not isinstance(row[0], str)
                    or not row[0].strip() or row[1] not in ROUTE_LABELS):
                raise BuildError(f"{filename}: {variable} has a row that is not (question, allowlisted pillar)")
            clean.append((row[0], row[1]))
        out[name] = clean
    return out


def external_datasets(sets) -> dict:
    """The evaluation datasets, in EXTERNAL_DATASETS order, built from the loaded sets."""
    def odd(rows):
        return [r for i, r in enumerate(rows) if i % 2 == 1]

    data = {
        "section1_literal": list(sets["section1"]),
        "section1b": list(sets["section1b"]),
        "section1c": list(sets["section1c"]),
        "holdout1_odd": odd(sets["holdout1"]),
        "holdout2_odd": odd(sets["holdout2"]),
    }
    data["pool"] = data["holdout1_odd"] + data["holdout2_odd"] + data["section1c"]
    return {name: data[name] for name in EXTERNAL_DATASETS}


def external_sha256(sets) -> str:
    """The ledger key for the external sets: their content, independent of the corpus.

    Keyed by the QUESTIONS, not the corpus, so rewriting the corpus does not buy a
    fresh external test: only new questions do.
    """
    body = {"external_sets_version": 1, "sets": {name: [list(r) for r in sets[name]] for name in EXTERNAL_SETS}}
    return hashlib.sha256(canonical_json(body)).hexdigest()


# =========================================================================== threshold, hybrid, gate
def select_tau(y, probabilities) -> dict:
    """The smallest tau in {0, 0.01, ..., 1} at which the argmax predictions with confidence >= tau
    are at least TAU_TARGET_ACCURACY accurate. If none qualifies, tau = 1.0 and ``qualified`` is
    False: the classifier then never proposes a route.

    ``probabilities`` are per-example {label: p} dicts (empty = no features: no prediction).
    """
    y = list(y)
    probabilities = list(probabilities)
    if len(y) != len(probabilities) or not y:
        raise ValueError("select_tau needs one probability dict per label, and at least one")
    predictions = []
    for p in probabilities:
        predictions.append(C.top_label(p) if p else (NONE_LABEL, 0.0))
    n = len(y)

    def at(tau):
        covered = [i for i in range(n) if predictions[i][1] >= tau]
        if not covered:
            return 0, None
        right = sum(1 for i in covered if predictions[i][0] == y[i])
        return len(covered), right / len(covered)

    for step in range(TAU_STEPS + 1):
        tau = step / TAU_STEPS
        count, acc = at(tau)
        if acc is not None and acc >= TAU_TARGET_ACCURACY:
            return {"tau": tau, "coverage": count / n, "accuracy_above": acc, "qualified": True,
                    "target_accuracy": TAU_TARGET_ACCURACY, "n": n}
    count, acc = at(1.0)
    return {"tau": 1.0, "coverage": count / n, "accuracy_above": acc, "qualified": False,
            "target_accuracy": TAU_TARGET_ACCURACY, "n": n}


def hybrid_label(keyword_label, decision) -> str:
    """What production answers with: machine-name and find answers stand (they run before the
    model); otherwise a proposed route replaces the keyword route; no proposal leaves it."""
    if keyword_label in ROUTER_ONLY_LABELS:
        return keyword_label
    return decision.route if decision.route is not None else keyword_label


def pool_gain_ok(block) -> bool:
    """H - K >= MIN_POOL_GAIN_POINTS accuracy points, in integer arithmetic (65/100 vs 60/100 passes)."""
    return (block["H"]["correct"] - block["K"]["correct"]) * 100 >= MIN_POOL_GAIN_POINTS * block["n"]


def _ledger_reasons(ledger, data_hash, test_sets):
    reasons = []
    for test_set in test_sets:
        try:
            count = LG.run_count(ledger, data_hash, test_set)
        except (ValueError, TypeError) as exc:
            reasons.append(f"test set {test_set}: the ledger is unusable ({exc}); treated as reused")
            continue
        if count != 1:
            reasons.append(f"test set {test_set} has {count} recorded evaluations for this data "
                           "(adoption needs exactly 1): reused or unrecorded")
    return reasons


def evaluate_gate(metrics, ledger) -> dict:
    """{"adopted": bool, "reasons": [...]} from the stored metrics and ledger alone.

    Adopted only if ALL hold:
      1. H beats K on the corpus test split;
      2. H beats K on the external pool by >= 5 accuracy points, with McNemar p < 0.05;
      3. H answers all of section 1's literal cases;
      4. H macro-F1 >= K macro-F1 on the pool;
      5. each gated test set was evaluated exactly once for its data hash.
    """
    reasons = []
    ct = metrics.get("corpus_test") if isinstance(metrics, dict) else None
    if not isinstance(ct, dict) or "K" not in ct:
        reasons.append("the corpus test evaluation is missing")
    else:
        if not ct["H"]["correct"] > ct["K"]["correct"]:
            reasons.append(f"H does not beat the keyword router on the corpus test split "
                           f"({ct['H']['correct']}/{ct['n']} vs {ct['K']['correct']}/{ct['n']})")
        reasons.extend(_ledger_reasons(ledger, ct.get("corpus_sha256", ""), CORPUS_TEST_SETS))
    ext = metrics.get("external") if isinstance(metrics, dict) else None
    if not isinstance(ext, dict) or "skipped" in ext or "pool" not in ext:
        reasons.append("the external evaluation was not run, so the model cannot be compared with the router "
                       "on the repo's questions")
    else:
        pool = ext["pool"]
        n = pool["n"]
        # ">= 5 points" implies "H beats K" (n >= 1), so this one condition states both; a separate
        # "H > K" check could never change the verdict (a mutation test showed it was dead code).
        if not pool_gain_ok(pool):
            gain = 100.0 * (pool["H"]["correct"] - pool["K"]["correct"]) / n
            reasons.append(f"the external pool gain is {gain:+.1f} points, under {MIN_POOL_GAIN_POINTS} points")
        p_value = pool["mcnemar_h_vs_k"]["p_value"]
        if not p_value < MCNEMAR_ALPHA:
            reasons.append(f"McNemar p = {p_value:.4g} on the external pool is not below {MCNEMAR_ALPHA}")
        literal = ext["section1_literal"]
        if literal["H"]["correct"] != literal["n"]:
            reasons.append(f"H answers {literal['H']['correct']}/{literal['n']} of section 1's literal cases "
                           "(all are required)")
        h_f1, k_f1 = pool["H"]["macro_f1"], pool["K"]["macro_f1"]
        if h_f1 is None or k_f1 is None or h_f1 < k_f1:
            reasons.append(f"H macro-F1 {h_f1} is below K macro-F1 {k_f1} on the external pool")
        reasons.extend(_ledger_reasons(ledger, ext.get("sha256", ""), EXTERNAL_TEST_SETS))
    return {"adopted": not reasons, "reasons": reasons}


# =========================================================================== scoring
def score_block(gold, keyword, model, hybrid, used, *, groups, seed) -> dict:
    """Aggregate numbers for K, M and H on one dataset. No question text."""
    n = len(gold)
    if not n or not (len(keyword) == len(model) == len(hybrid) == len(used) == n):
        raise ValueError("score_block needs equally long, non-empty prediction lists")
    block = {"n": n}
    for name, predictions in (("K", keyword), ("M", model), ("H", hybrid)):
        correct = sum(1 for a, b in zip(gold, predictions) if a == b)
        block[name] = {"correct": correct, "accuracy": correct / n,
                       "macro_f1": macro_f1(gold, predictions, METRIC_LABELS)}
    h_right = [a == b for a, b in zip(gold, hybrid)]
    k_right = [a == b for a, b in zip(gold, keyword)]
    block["mcnemar_h_vs_k"] = mcnemar_exact(h_right, k_right)
    block["h_minus_k_accuracy_bootstrap"] = paired_bootstrap_diff(
        accuracy, gold, hybrid, keyword, groups, n=BOOTSTRAP_N, seed=seed)
    used_rows = [i for i in range(n) if used[i]]
    block["model_route_used"] = {
        "count": len(used_rows), "coverage": len(used_rows) / n,
        "accuracy_when_used": (sum(1 for i in used_rows if hybrid[i] == gold[i]) / len(used_rows)
                               if used_rows else None),
        "changed_keyword_answer": sum(1 for i in used_rows if hybrid[i] != keyword[i]),
    }
    return block


def _keyword_routers(harnesses):
    """harness name -> question -> matched pillar, each via the harness's own seed() and call.

    Imported here, not at module level: only the evaluation needs the application.
    """
    routers = {}
    if "evaluation" in harnesses:
        import test_ai_evaluation as EV
        ev_session = EV.seed()
        routers["evaluation"] = lambda q: EV.ask(ev_session, EV.A, q).get("matched")
    if "holdout1" in harnesses:
        import test_ai_routing_holdout as H1
        h1_session = H1.seed()
        routers["holdout1"] = lambda q: H1.route(h1_session, q)
    if "holdout2" in harnesses:
        import test_ai_routing_holdout2 as H2
        h2_session = H2.seed()
        routers["holdout2"] = lambda q: H2.route(h2_session, q)
    return routers


def _systems(clf, questions, keyword):
    model, hybrid, used = [], [], []
    for q, k in zip(questions, keyword):
        p = clf.probabilities(q)
        model.append(C.top_label(p)[0] if p else NONE_LABEL)
        decision = clf.route(q)
        h = hybrid_label(k, decision)
        hybrid.append(h)
        used.append(k not in ROUTER_ONLY_LABELS and decision.route is not None)
    return model, hybrid, used


# =========================================================================== ledger persistence
def _prior_ledger(out_dir):
    """The ledger this build continues: from the previous eval JSON and artifact in ``out_dir``.

    Deleting one of the two files does not reset the count; if both exist they must agree.
    """
    found = []
    for filename in (EVAL_FILE, ARTIFACT_FILE):
        path = os.path.join(out_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                ledger = json.load(fh)["eval_ledger"]
            LG.run_count(ledger, "shape-check", "shape-check")   # raises ValueError on a malformed ledger
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BuildError(f"{filename} exists but its eval_ledger cannot be read ({exc.__class__.__name__}); "
                             "refusing to start a fresh ledger over it") from None
        found.append(ledger)
    if len(found) == 2 and found[0] != found[1]:
        raise BuildError("the eval JSON and the artifact in the output directory disagree about the ledger")
    return found[0] if found else {}


# =========================================================================== build
def _round(value):
    rounded = round(value, WEIGHT_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded   # no "-0.0" in the artifact


def _rounded(model_dict):
    """Weights and biases rounded to WEIGHT_DECIMALS places; rows that become all-zero are dropped
    (an absent row IS a zero row). Everything downstream - tau, every metric - uses this model."""
    out = dict(model_dict)
    out["bias"] = [_round(b) for b in model_dict["bias"]]
    weights = {}
    for key, row in model_dict["weights"].items():
        values = [_round(v) for v in row]
        if any(v != 0.0 for v in values):
            weights[key] = values
    out["weights"] = weights
    return out


def _fit(config, X_train, y_train, X_val, y_val, max_epochs):
    started = time.perf_counter()
    model = MultinomialLogisticRegression(LABELS, l2=config["l2"], lr=config["lr"], max_epochs=max_epochs,
                                          seed=SEED, patience=PATIENCE)
    model.fit(X_train, y_train, X_val=X_val, y_val=y_val)
    predictions = []
    for x in X_val:
        predictions.append(C.top_label(model.predict_proba(x))[0] if x else NONE_LABEL)
    result = {
        "l2": config["l2"], "lr": config["lr"], "max_epochs": max_epochs, "patience": PATIENCE,
        "epochs_run": model.epochs_run, "best_epoch": model.best_epoch, "stopped_early": model.stopped_early,
        "final_lr": model.final_lr, "val_loss": model.val_losses[model.best_epoch - 1],
        "val_accuracy": accuracy(y_val, predictions), "val_macro_f1": macro_f1(y_val, predictions, LABELS),
    }
    return model, result, time.perf_counter() - started


def build(*, out_dir=DEFAULT_OUT, small=False, created_at=None, backend_dir=BACKEND_DIR, log=print) -> dict:
    started = time.perf_counter()
    timings = {}
    created_at = created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))   # refuse a malformed timestamp early
    cap = SMALL["cap"] if small else K.MAX_PER_FAMILY
    max_epochs = SMALL["max_epochs"] if small else MAX_EPOCHS
    grid = SMALL["grid"] if small else GRID
    os.makedirs(out_dir, exist_ok=True)
    prior_ledger = _prior_ledger(out_dir)

    t = time.perf_counter()
    corpus = K.build_corpus(cap=cap)
    corpus_hash = K.corpus_sha256(corpus)
    train, val, test = K.split_corpus(corpus, seed=K.SPLIT_SEED)
    sets = load_external_question_sets(backend_dir)
    leakage = K.leakage_report([e.text for e in corpus], [q for rows in sets.values() for q, _ in rows])
    if leakage["exact"] or leakage["near"]:
        raise BuildError(f"corpus leakage: {leakage['exact']} exact and {leakage['near']} near copies of "
                         f"evaluation questions (corpus indices {leakage['flagged_indices'][:20]})")
    timings["corpus_and_leakage"] = time.perf_counter() - t
    log(f"corpus {len(corpus)} sentences ({len(train)}/{len(val)}/{len(test)}), leakage clean against "
        f"{leakage['questions_checked']} questions")

    t = time.perf_counter()
    X_train = [C.featurize(e.text) for e in train]
    X_val = [C.featurize(e.text) for e in val]
    y_train = [e.label for e in train]
    y_val = [e.label for e in val]
    timings["featurize"] = time.perf_counter() - t

    trials, grid_seconds = [], []
    best = None
    for index, config in enumerate(grid):
        model, result, seconds = _fit(config, X_train, y_train, X_val, y_val, max_epochs)
        trials.append(result)
        grid_seconds.append(seconds)
        log(f"grid {index}: l2={config['l2']} lr={config['lr']} val macro-F1 {result['val_macro_f1']:.4f} "
            f"acc {result['val_accuracy']:.4f} epochs {result['epochs_run']} ({seconds:.1f}s)")
        key = (result["val_macro_f1"], -result["val_loss"])
        if best is None or key > best[0]:
            best = (key, index, model)
    chosen = best[1]
    timings["grid"] = grid_seconds
    # Exactly what ships, and exactly what is evaluated from here on: the chosen model with its
    # weights rounded (smaller artifact), through the JSON round trip.
    model_dict = json.loads(canonical_json(_rounded(best[2].to_dict())))

    t = time.perf_counter()
    shipped = MultinomialLogisticRegression.from_dict(model_dict)
    val_probabilities = [shipped.predict_proba(x) if x else {} for x in X_val]
    tau_selection = select_tau(y_val, val_probabilities)
    timings["tau"] = time.perf_counter() - t
    log(f"tau {tau_selection['tau']} (validation coverage {tau_selection['coverage']:.3f}, "
        f"accuracy above {tau_selection['accuracy_above']}, qualified {tau_selection['qualified']})")

    parameters = {
        "model": model_dict, "weight_decimals": WEIGHT_DECIMALS,
        "tau": tau_selection["tau"], "tau_selection": tau_selection,
        "selection": {"criterion": "validation macro-F1, then lower validation loss, then grid order",
                      "grid": trials, "chosen_index": chosen},
    }
    provisional = {"model_type": C.MODEL_TYPE, "model_name": C.MODEL_NAME, "version": C.MODEL_VERSION,
                   "features": dict(C.FEATURE_SPEC), "parameters": parameters,
                   "adoption": {"adopted": False, "reasons": ["provisional"]}}
    clf = C.IntentClassifier(provisional)

    # ---------------------------------------------------------------- evaluation (test sets)
    harnesses = {_CORPUS_HARNESS} if small else {_CORPUS_HARNESS} | set(_HARNESS_FOR_SET.values())
    routers = _keyword_routers(harnesses)
    ledger = prior_ledger

    t = time.perf_counter()
    questions = [e.text for e in test]
    gold = [e.label for e in test]
    keyword = [routers[_CORPUS_HARNESS](q) for q in questions]
    model_labels, hybrid, used = _systems(clf, questions, keyword)
    corpus_block = score_block(gold, keyword, model_labels, hybrid, used, groups=[e.family for e in test],
                               seed=SEED)
    corpus_block["corpus_sha256"] = corpus_hash
    corpus_block["confusion_labels"] = list(METRIC_LABELS)
    corpus_block["confusion"] = {name: confusion_matrix(gold, pred, METRIC_LABELS)
                                 for name, pred in (("K", keyword), ("M", model_labels), ("H", hybrid))}
    ledger = LG.record_test_evaluation(ledger, data_hash=corpus_hash, test_set="corpus_test")
    timings["evaluate_corpus_test"] = time.perf_counter() - t

    t = time.perf_counter()
    if small:
        external = {"skipped": "small build: the repo's evaluation questions are not scored"}
    else:
        external_hash = external_sha256(sets)
        # Each set answered by its own harness, then sliced into datasets by the ONE pool definition.
        answered = {name: [(q, label, routers[_HARNESS_FOR_SET[name]](q)) for q, label in rows]
                    for name, rows in sets.items()}
        external = {"sha256": external_hash}
        for name, rows in external_datasets(answered).items():
            qs = [q for q, _label, _k in rows]
            kw = [k for _q, _label, k in rows]
            m_labels, h_labels, h_used = _systems(clf, qs, kw)
            external[name] = score_block([label for _q, label, _k in rows], kw, m_labels, h_labels, h_used,
                                         groups=None, seed=SEED)
        for test_set in EXTERNAL_TEST_SETS:
            ledger = LG.record_test_evaluation(ledger, data_hash=external_hash, test_set=test_set)
    timings["evaluate_external"] = time.perf_counter() - t

    metrics = {"corpus_test": corpus_block, "external": external}
    adoption = evaluate_gate(metrics, ledger)
    baseline_metrics = {
        "system": "ai.assistant keyword router (answer()['matched'])",
        "corpus_test": corpus_block["K"],
        "external_pool": external["pool"]["K"] if "pool" in external else None,
    }
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "model_type": C.MODEL_TYPE,
        "model_name": C.MODEL_NAME,
        "version": C.MODEL_VERSION,
        "build_mode": "small" if small else "full",
        "created_at": created_at,
        "training_data_source": (f"synthetic:{K.CORPUS_VERSION} (AMP-authored question families; train split "
                                 f"only) expansion_seed={K.EXPANSION_SEED} split_seed={K.SPLIT_SEED} "
                                 f"max_per_family={cap}"),
        "consent_basis": CONSENT_BASIS,
        "features": dict(C.FEATURE_SPEC),
        "parameters": parameters,
        "metrics": metrics,
        "baseline_metrics": baseline_metrics,
        "adoption": adoption,
        "seed": SEED,
        "generator": K.CORPUS_VERSION,
        "generator_sha256": corpus_hash,
        "eval_ledger": ledger,
    }
    artifact_path = os.path.join(out_dir, ARTIFACT_FILE)
    sha = save_artifact(artifact_path, artifact)
    timings["total"] = time.perf_counter() - started

    evaluation = {
        "eval_version": 1,
        "model_name": C.MODEL_NAME,
        "version": C.MODEL_VERSION,
        "build_mode": artifact["build_mode"],
        "artifact_file": ARTIFACT_FILE,
        "artifact_sha256": sha,
        "created_at": created_at,
        "corpus": {"version": K.CORPUS_VERSION, "sha256": corpus_hash, "examples": len(corpus),
                   "families": len({e.family for e in corpus}), "max_per_family": cap,
                   "split_examples": {"train": len(train), "val": len(val), "test": len(test)},
                   "split_families": {name: len({e.family for e in part})
                                      for name, part in (("train", train), ("val", val), ("test", test))}},
        "leakage": {k: leakage[k] for k in ("questions_checked", "corpus_sentences_checked", "exact", "near",
                                            "threshold")},
        "selection": parameters["selection"],
        "tau_selection": tau_selection,
        "metrics": metrics,
        "baseline_metrics": baseline_metrics,
        "adoption": adoption,
        "eval_ledger": ledger,
        "timings_seconds": timings,
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "notes": [
            "K = keyword router, M = model argmax, H = model when confident else K (what production would run).",
            "Corpus test numbers favour the model: AMP wrote the corpus with the keyword table in view, and "
            "__none__ sentences can never be right for K or H. Read them as a difference, not an accuracy claim.",
            "The external pool (odd halves of both holdout files plus section 1c) is the honest comparison and is "
            "small; section 1b is reported only because it guided the router's design.",
            "No evaluation question text is stored here; only aggregate counts.",
        ],
    }
    eval_path = os.path.join(out_dir, EVAL_FILE)
    tmp = eval_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(evaluation, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n")
    os.replace(tmp, eval_path)
    return {"artifact_path": artifact_path, "eval_path": eval_path, "sha256": sha, "evaluation": evaluation}


def _summary(evaluation):
    lines = []
    metrics = evaluation["metrics"]
    blocks = [("corpus_test", metrics["corpus_test"])]
    if "skipped" not in metrics["external"]:
        blocks += [(name, metrics["external"][name]) for name in EXTERNAL_DATASETS]
    for name, b in blocks:
        lines.append(f"  {name:<17} n={b['n']:<4} K {b['K']['correct']:>4} ({b['K']['accuracy']:.3f})  "
                     f"M {b['M']['correct']:>4} ({b['M']['accuracy']:.3f})  H {b['H']['correct']:>4} "
                     f"({b['H']['accuracy']:.3f})  McNemar H vs K p={b['mcnemar_h_vs_k']['p_value']:.4g}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--small", action="store_true", help="quick smoke build; never evaluates external sets")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory (default: amp_ai/artifacts)")
    parser.add_argument("--created-at", default=None, help="ISO-8601 timestamp to record (fix it for reproducibility)")
    args = parser.parse_args(argv)
    result = build(out_dir=args.out, small=args.small, created_at=args.created_at)
    ev = result["evaluation"]
    print(_summary(ev))
    print(f"adopted: {ev['adoption']['adopted']}")
    for reason in ev["adoption"]["reasons"]:
        print(f"  - {reason}")
    print(f"artifact sha256: {result['sha256']}")
    print(f"total build time: {ev['timings_seconds']['total']:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
