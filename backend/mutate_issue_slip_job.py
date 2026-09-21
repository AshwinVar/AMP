"""Mutation harness for "material issued for a job is in that job's trace".

Each mutation quietly breaks the genealogy link an issue slip now makes: the
transaction referencing the slip again, a fuzzy match that lands "WO-1" on
WO-100, an untrimmed match, the unresolved note dropped, the list claiming every
reference resolved. For each the harness applies the edit, runs the suite that
is supposed to notice, and restores the file byte for byte. A mutation that
leaves the suite green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_issue_slip_job.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_issue_slip_counts_against_its_job.py"]
SUITE_TIMEOUT = 300

EIR = "enterprise_inventory_routes.py"

# (label, file, old, new)
MUTATIONS = [
    ("the transaction references the slip again, whatever the job", EIR,
     "        reference, note = wo.work_order_no, f\"Issued via {s.slip_no} for {wo.work_order_no}\"",
     "        reference, note = s.slip_no, f\"Issued via {s.slip_no} for {wo.work_order_no}\""),
    ("a fuzzy match: 'WO-1' lands on WO-100", EIR,
     "    return db.query(models.WorkOrder).filter(models.WorkOrder.work_order_no == text).first()",
     "    return db.query(models.WorkOrder).filter(models.WorkOrder.work_order_no.like(text + \"%\")).first()"),
    ("the reference is not trimmed before the match", EIR,
     '    text = (ref or "").strip()\n    if not text:\n        return None\n    return db.query(models.WorkOrder)',
     '    text = (ref or "")\n    if not text:\n        return None\n    return db.query(models.WorkOrder)'),
    ("an unresolved reference is not said to be one", EIR,
     '        reference, note = s.slip_no, (f"Issued via {s.slip_no} for {s.work_order_ref.strip()} "\n'
     '                                      "(not a work order in AMP; not in any job\'s trace)")',
     '        reference, note = s.slip_no, f"Issued via {s.slip_no} for {s.work_order_ref.strip()}"'),
    ("the list says every reference resolved", EIR,
     '            "work_order_resolved": (s.work_order_ref or "").strip() in known,',
     '            "work_order_resolved": bool((s.work_order_ref or "").strip()),'),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True, errors="replace",
                                  cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
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
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<62} {verdict:<10} {note}", flush=True)
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
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
