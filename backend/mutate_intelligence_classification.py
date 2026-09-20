"""Mutation harness for the intelligence-classification guard.

test_intelligence_classification.py is the one test whose subject is a
DOCUMENT: it holds the founder's handbook (ch. 31, "Every intelligence
engine, classified") to what the model cards and the adopted-LLM record say,
so a stale sentence cannot market one kind of engine as another. A guard over
prose is easy to weaken without noticing -- a reworded row, a verdict that
quietly flips, an old claim pasted back in -- so this harness bends the
DOCUMENT and the RECORD, not the code, and expects the guard to go red each
time. These are the four mutations that were run by hand before the guard was
committed (#663), plus three more the follow-up verdict (#665) made possible.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_intelligence_classification.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_intelligence_classification.py", "test_copilot_model_adoption.py"]

H = os.path.join("..", "docs", "training", "AMP-FOUNDER-TECHNICAL-HANDBOOK.md")
R = os.path.join("ai", "adopted_models.json")

MUTATIONS = [
    ("the failure-risk model is reclassified as STATISTICAL", H,
     "| Failure-risk model (`amp_ai/failure_risk`) | **ML MODEL** |",
     "| Failure-risk model (`amp_ai/failure_risk`) | **STATISTICAL** |"),
    ("the failure-risk verdict is flipped to not adopted (the card says adopted)", H,
     "**Adopted** against the rule scorer on **synthetic** machines only",
     "**Not adopted** against the rule scorer on **synthetic** machines only"),
    ("the anomaly check is called adopted (the card says not)", H,
     "**Not adopted**: on held-out synthetic data",
     "**Adopted**: on held-out synthetic data"),
    ("a retired claim reappears", H,
     "**Do not market one as another.**",
     "**Do not market one as another.** (As of today no trained ML models exist.)"),
    ("the copilot row stops saying production answers from the rules", H,
     "so production answers from the rules — as does everywhere else until a model passes;",
     "so production answers from the model where one is configured;"),
    ("Current Reality no longer says no model is promoted", H,
     "🟡 **BUILT · no model currently promoted · off on production**",
     "🟡 **BUILT · self-hosted model promoted · off on production**"),
    ("the failing adoption record is hand-edited to passed", R,
     "\"passed\": false",
     "\"passed\": true"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        if not os.path.exists(os.path.join(here, suite)):
            continue
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 116)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<74} {verdict:<10} {note}")
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print(f"  - {s}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
