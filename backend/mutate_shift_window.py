"""Mutation harness for the one shift-attainment window (shift_contract).

Each mutation brings back one of the spans that used to disagree, or lets a
surface drop the basis it now states: the contract stops bounding the window,
a rollup goes back to pooling all time or to a row count, a figure with no
target reads as a measured 0%, the card keeps its own cutoff. For each the
harness applies the edit, runs the suites that are supposed to notice, and
restores the file byte for byte. A mutation that leaves the suites green
SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_shift_window.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_shift_rollups_one_window.py", "test_shift.py",
          "test_no_target_no_shift_efficiency.py", "test_analytics_routes.py",
          "test_executive_oee_sql_parity.py"]
SUITE_TIMEOUT = 600

CONTRACT = "shift_contract.py"
ROUTES = "analytics_routes.py"
ENGINE = "analytics_engine.py"
SHIFT = os.path.join("ai", "shift.py")

# (label, file, old, new)
MUTATIONS = [
    ("the contract pools every shift ever recorded (the window's start is ignored)", CONTRACT,
     "    if window.start is not None:\n        q = q.filter(models.ShiftData.created_at >= window.start)\n    q = q.filter(models.ShiftData.created_at < window.end)\n    target, actual, entries = q.one()",
     "    q = q.filter(models.ShiftData.created_at < window.end)\n    target, actual, entries = q.one()"),
    ("the contract reads every tenant's shifts", CONTRACT,
     "    ).filter(models.ShiftData.tenant_code == tenant)\n    if window.start is not None:",
     "    )\n    if window.start is not None:"),
    ("a week with no target is measured at 0%", CONTRACT,
     '        "attainment": shift_attainment(actual, target),',
     '        "attainment": shift_attainment(actual, target) or 0,'),
    ("the contract calls an unplanned week measured", CONTRACT,
     '        "measured": target > 0,',
     '        "measured": True,'),
    ("the rows under a headline stop being bounded by the window", CONTRACT,
     "    return (q.filter(models.ShiftData.created_at < window.end)\n             .order_by(models.ShiftData.id).all())",
     "    return q.order_by(models.ShiftData.id).all()"),
    ("the summary rollup goes back to a lifetime sum", ROUTES,
     "    shift = shift_contract.pooled_attainment(db, request_tenant(current_user), _oee_window)\n    total_shift_target = shift[\"target\"]",
     "    shift = shift_contract.pooled_attainment(db, request_tenant(current_user), oee_contract.OeeWindow(None))\n    total_shift_target = shift[\"target\"]"),
    ("the executive headline goes back to a lifetime sum", ROUTES,
     "    plan = shift_contract.pooled_attainment(db, request_tenant(current_user), _oee_window)",
     "    plan = shift_contract.pooled_attainment(db, request_tenant(current_user), oee_contract.OeeWindow(None))"),
    ("the management rollup goes back to a lifetime sum", ROUTES,
     "    shift = shift_contract.pooled_attainment(db, request_tenant(current_user), _oee_window)\n\n    rate = tenant_unit_value",
     "    shift = shift_contract.pooled_attainment(db, request_tenant(current_user), oee_contract.OeeWindow(None))\n\n    rate = tenant_unit_value"),
    ("the management figure stops saying whether it measured anything", ENGINE,
     '        "target_achievement_measured": target_output > 0,',
     '        "target_achievement_measured": True,'),
    ("the card keeps its own window", SHIFT,
     "WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS",
     "WINDOW_DAYS = 30"),
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
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 116)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<74} {verdict:<10} {note}", flush=True)
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
