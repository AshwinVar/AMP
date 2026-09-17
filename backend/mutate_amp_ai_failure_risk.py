"""Mutation harness for the AMP-native failure-risk model (amp_ai/failure_risk).

Each mutation below is a small, plausible edit that quietly breaks a guard the
failure-risk model relies on: a term of the adoption gate, the test-set ledger,
the refit that must stop at train_end, the label window that must end inside
the generated data, the exclusion of machines already down, the calibrator that
must be increasing, the pinned feature list, the explicit tenant filter in the
database loader, the half-open window, None-not-0, the generator's independence
from the rule scorer, and the purity of the modules that must never reach a
query. For each one the harness applies the edit, runs the suite that is
supposed to notice, and restores the file byte for byte. A mutation that leaves
its suite green SURVIVED: that guard is untested.

RUN IT ALONE, ideally on a copy of backend/. Like every mutate_* harness it
EDITS THE WORKING TREE and restores it afterwards; anything reading those files
meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_amp_ai_failure_risk.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

BUILD = "test_amp_ai_failure_risk_build.py"
PURITY = "test_amp_ai_failure_risk_purity.py"
FEATURES = "test_amp_ai_failure_risk_features.py"
DB = "test_amp_ai_failure_risk_db_parity.py"
RULE = "test_amp_ai_failure_risk_rule_parity.py"
GENERATOR = "test_amp_ai_failure_risk_generator.py"

B = "amp_ai/failure_risk/build.py"
P = "amp_ai/failure_risk/predict.py"

# (label, file, old, new, suite that must go red)
MUTATIONS = [
    # --- the adoption gate --------------------------------------------------
    ("gate: paired PR-AUC lower bound need not exceed 0", B,
     '"passed": lo is not None and lo > 0.0,', '"passed": lo is not None,', BUILD),
    ("gate: ROC-AUC criterion always passes", B,
     '"passed": model_roc is not None and rule_roc is not None and model_roc >= rule_roc,', '"passed": True,', BUILD),
    ("gate: precision@k criterion always passes", B,
     '"passed": model_pak is not None and rule_pak is not None and model_pak >= rule_pak,', '"passed": True,',
     BUILD),
    ("gate: precision@k tie with the rule fails (> instead of >=)", B,
     "and model_pak >= rule_pak,", "and model_pak > rule_pak,", BUILD),
    ("gate: Brier equal to the base rate passes (<= instead of <)", B,
     "and brier < base,", "and brier <= base,", BUILD),
    ("gate: ECE threshold loosened to 0.10", B, "ECE_MAX = 0.05", "ECE_MAX = 0.10", BUILD),
    ("gate: an undefined ECE passes", B,
     '"passed": ece is not None and ece <= ECE_MAX,', '"passed": ece is None or ece <= ECE_MAX,', BUILD),
    ("gate: a reused test set can still adopt", B,
     '"passed": first_run_for(ledger, data_hash, test_set),', '"passed": True,', BUILD),
    ("gate: adopted regardless of the criteria", B,
     '"adopted": all(c["passed"] for c in criteria),', '"adopted": True,', BUILD),

    # --- data, split, ledger ------------------------------------------------
    ("refit trains on every validation row, past train_end", B,
     'rows = SP.refit_rows(train, val, "day", train_end=config.train_end)', "rows = list(train) + list(val)", BUILD),
    ("last as-of date's label window runs past the generated data", B,
     "last = config.days - config.horizon - 1", "last = config.days - config.horizon", BUILD),
    ("machines already in Breakdown are trained and scored on", B,
     "            if in_breakdown_at(h, as_of):", "            if False:", BUILD),
    ("the test evaluation is not recorded in the ledger", B,
     "ledger = record_test_evaluation(prior_ledger, data_hash=data_hash, test_set=main_set)",
     "ledger = prior_ledger", BUILD),
    ("the CLI starts a fresh ledger over an existing eval JSON", B,
     "prior = read_prior_ledger(os.path.join(args.out, EVAL_FILE))", "prior = {}", BUILD),
    ("--small may overwrite the shipped artifacts", B,
     "if args.small and _same_dir(args.out, ARTIFACTS_DIR):", "if False:", BUILD),
    ("a decreasing Platt calibrator is shipped", B,
     "problem = None if platt.a > 0.0 else", "problem = None if True else", BUILD),
    ("generator hash depends on the checkout's line endings", B,
     'hashlib.sha256(source.replace(b"\\r\\n", b"\\n")).hexdigest()', "hashlib.sha256(source).hexdigest()", BUILD),

    # --- serving ------------------------------------------------------------
    ("predict accepts a decreasing calibrator", P,
     "if calibrator is not None and not calibrator.a > 0.0:", "if False:", BUILD),
    ("predict ignores the stored calibrator", P, "if self.calibrator is None:", "if True:", BUILD),
    ("predict scores a machine already in Breakdown", P, "        if in_breakdown_at(view, as_of):",
     "        if False:", BUILD),
    ("predict accepts features this code does not extract", P,
     "if tuple(names) != FEATURE_NAMES:", "if False:", BUILD),
    ("predict raises on a tampered artifact instead of model_unavailable", P,
     "except ArtifactIntegrityError as exc:", "except ZeroDivisionError as exc:", BUILD),
    ("predict reaches for the database loader", P,
     "from . import baseline_rule", "from . import baseline_rule, db_history", PURITY),
    ("build reaches for the database loader", B,
     "from . import baseline_rule, predict, synthetic", "from . import baseline_rule, db_history, predict, synthetic",
     PURITY),

    # --- the history, features, loader and generator underneath ------------
    ("db loader: downtime loses its explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(DL.tenant_code == tenant, DL.created_at >= start, DL.created_at < end)",
     ".filter(DL.created_at >= start, DL.created_at < end)", DB),
    ("db loader: machines lose their explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(M.tenant_code == tenant).order_by(M.id).all())", ".order_by(M.id).all())", DB),
    ("db loader: in-window events lose their explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(E.tenant_code == tenant, E.created_at >= start, E.created_at < end)",
     ".filter(E.created_at >= start, E.created_at < end)", DB),
    ("db loader: the state subquery loses its explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(E.tenant_code == tenant, E.created_at >= state_start, E.created_at < start)\n"
     "                .group_by(E.machine_id).subquery())",
     ".filter(E.created_at >= state_start, E.created_at < start)\n"
     "                .group_by(E.machine_id).subquery())", DB),
    ("db loader: the OUTER state query loses its explicit tenant filter (same-instant foreign event)",
     "amp_ai/failure_risk/db_history.py",
     "E.created_at == latest.c.last_at))\n"
     "              .filter(E.tenant_code == tenant, E.created_at >= state_start, E.created_at < start)",
     "E.created_at == latest.c.last_at))\n"
     "              .filter(E.created_at >= state_start, E.created_at < start)", DB),
    ("db loader: production loses its explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(P.tenant_code == tenant, P.created_at >= start, P.created_at < end)",
     ".filter(P.created_at >= start, P.created_at < end)", DB),
    ("db loader: inspections lose their explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(Q.tenant_code == tenant, Q.machine_id.isnot(None),", ".filter(Q.machine_id.isnot(None),", DB),
    ("db loader: maintenance loses its explicit tenant filter", "amp_ai/failure_risk/db_history.py",
     ".filter(T.tenant_code == tenant,\n", ".filter(\n", DB),
    ("db loader: events lose their upper time bound", "amp_ai/failure_risk/db_history.py",
     ".filter(E.tenant_code == tenant, E.created_at >= start, E.created_at < end)",
     ".filter(E.tenant_code == tenant, E.created_at >= start)", DB),
    ("truncation keeps a record stamped exactly as_of", "amp_ai/failure_risk/history.py",
     "j = bisect.bisect_left(records, end, lo=i, key=_ts)", "j = bisect.bisect_right(records, end, lo=i, key=_ts)",
     FEATURES),
    ("labels look one day past the 7-day horizon", "amp_ai/failure_risk/history.py",
     "as_of + timedelta(days=horizon_days)", "as_of + timedelta(days=horizon_days + 1)", FEATURES),
    ("a rate with nothing to divide by reads 0 instead of None", "amp_ai/failure_risk/features.py",
     "return numerator / denominator if denominator > 0 else None",
     "return numerator / denominator if denominator > 0 else 0.0", FEATURES),
    ("features.py imports the ORM models", "amp_ai/failure_risk/features.py",
     "from .history import BREAKDOWN, LOOKBACK_DAYS, date_instant, truncate, window_bounds",
     "import models\nfrom .history import BREAKDOWN, LOOKBACK_DAYS, date_instant, truncate, window_bounds", PURITY),
    ("rule adapter drops MachineEvent breakdowns from its counter", "amp_ai/failure_risk/baseline_rule.py",
     "+ sum(1 for e in events if e[2] == BREAKDOWN)),", "+ 0),", RULE),
    ("the generator imports the rule scorer", "amp_ai/failure_risk/synthetic.py",
     "from ..core.rng import make_rng", "from ..core.rng import make_rng\nimport predictive_engine", GENERATOR),
]


def run(suite):
    # PYTHONDONTWRITEBYTECODE is load-bearing (see mutate_amp_ai_core.py): a same-length mutation restored
    # within the same second can leave a .pyc of the MUTATED code that Python then trusts.
    env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, suite], cwd=HERE, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=900)
    return proc.returncode


def drop_bytecode(path):
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.splitext(os.path.basename(path))[0] + "."
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            if name.startswith(stem) and name.endswith(".pyc"):
                os.remove(os.path.join(cache, name))


def main():
    suites = sorted({m[4] for m in MUTATIONS})
    for rel in sorted({m[1] for m in MUTATIONS}):
        drop_bytecode(os.path.join(HERE, *rel.split("/")))
    for suite in suites:
        if run(suite) != 0:
            print(f"ABORT: {suite} fails before any mutation")
            return 2
    print(f"baseline: {len(suites)} suites green\n")
    print(f"{'mutation':<72} verdict")
    print("-" * 84)
    problems = []
    for label, rel, old, new, suite in MUTATIONS:
        path = os.path.join(HERE, *rel.split("/"))
        with open(path, "rb") as fh:
            original = fh.read()
        text = original.decode("utf-8")
        crlf = "\r\n" in text
        needle = old.replace("\n", "\r\n") if crlf else old
        replacement = new.replace("\n", "\r\n") if crlf else new
        hits = text.count(needle)
        if hits != 1:
            print(f"{label:<72} NOT APPLIED ({hits} matches in {rel})")
            problems.append(label)
            continue
        try:
            with open(path, "wb") as fh:
                fh.write(text.replace(needle, replacement, 1).encode("utf-8"))
            code = run(suite)
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            drop_bytecode(path)
        with open(path, "rb") as fh:
            if fh.read() != original:
                print(f"FATAL: {rel} was not restored byte for byte")
                return 3
        verdict = "caught" if code != 0 else "SURVIVED"
        print(f"{label:<72} {verdict} ({suite})", flush=True)
        if code == 0:
            problems.append(label)
    print()
    for suite in suites:
        if run(suite) != 0:
            print(f"FATAL: {suite} fails after restoring every file")
            return 3
    if problems:
        print(f"{len(problems)} of {len(MUTATIONS)} mutations not caught or not applied:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught; every file restored; suites green again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
