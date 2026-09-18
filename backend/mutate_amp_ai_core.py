"""Mutation harness for the AMP-native AI core (amp_ai/core).

Each mutation below is a small, plausible edit that quietly breaks a guard the
native-AI build relies on: the pinned artifact hash, the JSON loader's refusal
of NaN, tie handling in ROC-AUC, the label-horizon term in the leakage-proof
split, the purity scanner's file-count assertion, hashing that does not depend
on PYTHONHASHSEED, and so on. For each one the harness applies the edit, runs
the suite that is supposed to notice, and restores the file byte for byte.
A mutation that leaves its suite green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_amp_ai_core.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

METRICS = "test_amp_ai_core_metrics.py"
MODELS = "test_amp_ai_core_models.py"
ARTIFACT = "test_amp_ai_core_artifact.py"
PURITY = "test_amp_ai_core_purity.py"

# (label, file, old, new, suite that must go red)
MUTATIONS = [
    # --- artifact integrity -------------------------------------------------
    ("load ignores the hash PINNED IN CODE (embedded hash only)", "amp_ai/core/artifact.py",
     '    if not hmac.compare_digest(data["sha256"], expected_sha256):',
     "    if False:", ARTIFACT),
    ("load ignores the EMBEDDED hash", "amp_ai/core/artifact.py",
     '    if not hmac.compare_digest(recomputed, data["sha256"]):',
     "    if False:", ARTIFACT),
    ("json accepts NaN/Infinity again (parse_constant dropped)", "amp_ai/core/artifact.py",
     "parse_constant=_reject_constant, ", "", ARTIFACT),
    ("1e999 overflows to inf unchecked (parse_float dropped)", "amp_ai/core/artifact.py",
     "parse_float=_finite_float,", "", ARTIFACT),
    ("duplicate keys silently keep the last value", "amp_ai/core/artifact.py",
     "                          object_pairs_hook=_no_duplicates)", "                          )", ARTIFACT),
    ("the size cap is not enforced", "amp_ai/core/artifact.py",
     "    if len(raw) > MAX_ARTIFACT_BYTES:", "    if False:", ARTIFACT),
    ("the model_type check is dropped", "amp_ai/core/artifact.py",
     '    if data["model_type"] != expected_model_type:', "    if False:", ARTIFACT),
    ("the hash stops covering the adoption verdict", "amp_ai/core/artifact.py",
     '    payload = {k: v for k, v in artifact.items() if k != "sha256"}',
     '    payload = {k: v for k, v in artifact.items() if k not in ("sha256", "adoption")}', ARTIFACT),

    # --- metrics ------------------------------------------------------------
    ("ROC-AUC: tied items get the top rank instead of the average", "amp_ai/core/metrics.py",
     "        average_rank = position + (n_items + 1) / 2.0",
     "        average_rank = position + n_items", METRICS),
    ("average precision: ties split into separate thresholds", "amp_ai/core/metrics.py",
     "        groups[v] = (n + 1, pos + t)",
     "        groups[(v, len(groups))] = (1, t)", METRICS),
    ("precision@k: a boundary tie is resolved by input order", "amp_ai/core/metrics.py",
     "            positives += need * (n_positive / n_items)",
     "            positives += min(need, n_positive)", METRICS),
    ("precision@k grouped: naive ceil inflates k", "amp_ai/core/metrics.py",
     "        k = min(len(ys), max(1, math.ceil(round(frac * len(ys), 9))))",
     "        k = min(len(ys), max(1, math.ceil(frac * len(ys))))", METRICS),
    ("macro-F1: never-predicted labels are skipped instead of scoring 0", "amp_ai/core/metrics.py",
     "        if tp + fp + fn == 0:", "        if tp == 0:", METRICS),
    ("McNemar: one-sided p reported as two-sided", "amp_ai/core/metrics.py",
     "Fraction(2 * tail, 2 ** m)", "Fraction(tail, 2 ** m)", METRICS),
    ("bootstrap resamples rows, not clusters", "amp_ai/core/metrics.py",
     "    if groups is None:\n        return [[i] for i in range(n)]",
     "    if True:\n        return [[i] for i in range(n)]", METRICS),
    ("bootstrap reports an interval from too few defined replicates", "amp_ai/core/metrics.py",
     "    if len(values) < math.ceil(MIN_VALID_REPLICATE_FRACTION * n) or not values:",
     "    if not values:", METRICS),
    ("empty calibration bin reports 0 instead of None", "amp_ai/core/metrics.py",
     '            "observed_rate": positives[i] / n if n else None,',
     '            "observed_rate": positives[i] / n if n else 0.0,', METRICS),

    # --- split --------------------------------------------------------------
    ("split: train label horizon ignored (t < val_start)", "amp_ai/core/split.py",
     "    return t + horizon < val_start and t <= train_end",
     "    return t < val_start and t <= train_end", MODELS),
    ("split: val label horizon ignored (t < test_start)", "amp_ai/core/split.py",
     "    return val_start <= t and t + horizon < test_start",
     "    return val_start <= t and t < test_start", MODELS),
    ("split: gap check forgets the horizon", "amp_ai/core/split.py",
     "    if test_start < train_end + horizon + gap:",
     "    if test_start < train_end + gap:", MODELS),
    ("split: entity overlap not checked", "amp_ai/core/split.py",
     "        if shared:", "        if False:", MODELS),
    ("split: an empty split is accepted", "amp_ai/core/split.py",
     "        if not part:\n            raise SplitError", "        if False:\n            raise SplitError", MODELS),

    # --- hashing / rng --------------------------------------------------------
    ("stable_hash uses the salted built-in hash()", "amp_ai/core/rng.py",
     '    value = int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")',
     "    value = hash(text) & 0xFFFFFFFFFFFFFFFF", MODELS),
    ("make_rng lets '|' alias two streams", "amp_ai/core/rng.py",
     '        if "|" in name:', "        if False:", MODELS),

    # --- learners / baselines -------------------------------------------------
    ("IRLS penalises the intercept", "amp_ai/core/logistic.py",
     "        for j in range(k):\n            grad[j] += l2 * beta[j]",
     "        for j in range(size):\n            grad[j] += l2 * beta[j]", MODELS),
    ("multinomial early stopping does not restore the best epoch", "amp_ai/core/multinomial.py",
     "        if best is not None:\n            weights, bias = best",
     "        if False:\n            weights, bias = best", MODELS),
    ("multinomial never backs off a diverging learning rate", "amp_ai/core/multinomial.py",
     "                lr *= 0.5", "                break", MODELS),
    ("Standardizer imputes with the mean instead of the training median", "amp_ai/core/standardize.py",
     "            median = float(statistics.median(observed))",
     "            median = math.fsum(observed) / len(observed)", MODELS),
    ("a missing feature key silently becomes None", "amp_ai/core/standardize.py",
     "        return [_value(row[name], name) for name in names]",
     "        return [_value(row.get(name), name) for name in names]", MODELS),
    ("robust z-score ignores the MAD floor", "amp_ai/core/robust.py",
     "(MAD_SCALE * max(self.mad[name], self.floor[name]))",
     "(MAD_SCALE * self.mad[name] or 1e-300)", MODELS),
    ("empirical calibrator counts ties as fully below", "amp_ai/core/robust.py",
     "        return (below + 0.5 * equal) / (self.n + 1)",
     "        return (below + equal) / (self.n + 1)", MODELS),
    ("no shrinkage: plain sample covariance", "amp_ai/core/robust.py",
     "        shrinkage = b2 / d2 if d2 > 0.0 else 0.0", "        shrinkage = 0.0", MODELS),

    # --- ledger / contracts ----------------------------------------------------
    ("ledger: any recorded run counts as the first", "amp_ai/core/ledger.py",
     "    return run_count(ledger, data_hash, test_set) == 1",
     "    return run_count(ledger, data_hash, test_set) >= 1", ARTIFACT),
    ("ConsentRequired accepts a GRANTED decision", "amp_ai/core/contracts.py",
     "        if decision.granted:", "        if False:", ARTIFACT),
    ("a consent decision may omit its reason", "amp_ai/core/contracts.py",
     "        if not isinstance(self.reason, str) or not self.reason.strip():",
     "        if not isinstance(self.reason, str):", ARTIFACT),

    # --- purity scanner ---------------------------------------------------------
    ("purity: min_files is not enforced", "amp_ai/core/purity.py",
     "    if len(files) < min_files:", "    if False:", PURITY),
    ("purity: every top-level import counts as stdlib", "amp_ai/core/purity.py",
     "            elif top in sys.stdlib_module_names or top in extra:",
     "            elif True:", PURITY),
    ("purity: forbidden modules are not refused", "amp_ai/core/purity.py",
     "            hit = _hits(imp, FORBIDDEN_MODULES)", "            hit = None", PURITY),
    ("purity: relative imports are not followed", "amp_ai/core/purity.py",
     "                    continue\n                queue.extend(targets)\n            elif _is_local",
     "                    continue\n            elif _is_local", PURITY),
    ("purity: eval/exec references are not refused", "amp_ai/core/purity.py",
     '            facts.dynamic.append((node.lineno, f"uses {node.id}"))',
     "            pass", PURITY),
    ("purity: a missing path is ignored", "amp_ai/core/purity.py",
     '    violations = [f"{m}: path does not exist" for m in missing]',
     "    violations = []", PURITY),
]


def run(suite):
    # PYTHONDONTWRITEBYTECODE is load-bearing. A .pyc is trusted when the source's
    # mtime (whole seconds) and size match its header. A same-length mutation
    # ("== 1" -> ">= 1") restored within the same second leaves a pyc of the
    # MUTATED code that Python then treats as current: the "restored" module stays
    # broken and every later verdict is contaminated. Seen, not hypothesised.
    env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, suite], cwd=HERE, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600)
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
        print(f"{label:<72} {verdict} ({suite})")
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
