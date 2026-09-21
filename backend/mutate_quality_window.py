"""Mutation harness for the one quality window (quality_contract).

Each mutation brings back one of the three spans that used to disagree, or lets
a surface publish a rate it did not measure: the contract stops bounding the
window or stops filtering the tenant, a route goes back to the whole inspection
register, a rate over nothing reads as a measured 0%, the trend re-cuts its
halves by calendar day, a consumer reads a missing figure aloud. For each the
harness applies the edit, runs the suites that are supposed to notice, and
restores the file byte for byte. A mutation that leaves the suites green
SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_quality_window.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_quality_one_window.py", "test_quality.py", "test_quality_trend.py",
          "test_analytics_routes.py", "test_twin.py", "test_briefing.py"]
SUITE_TIMEOUT = 900

CONTRACT = "quality_contract.py"
ROUTES = "analytics_routes.py"
QUALITY = os.path.join("ai", "quality.py")
TWIN = os.path.join("ai", "twin.py")
ASSISTANT = os.path.join("ai", "assistant.py")
BRIEFING = os.path.join("ai", "briefing.py")
WINDOWS = "oee_contract.py"

# (label, file, old, new)
MUTATIONS = [
    ("the contract pools every inspection ever recorded (the start is ignored)", CONTRACT,
     "    if window.start is not None:\n        q = q.filter(QI.created_at >= window.start)\n    q = q.filter(QI.created_at < window.end)",
     "    q = q.filter(QI.created_at < window.end)"),
    ("the contract stops dropping rows dated after the window", CONTRACT,
     "    q = q.filter(QI.created_at < window.end)\n    # int() so a DB that returns Decimal",
     "    # int() so a DB that returns Decimal"),
    ("the contract reads every tenant's inspections", CONTRACT,
     "    ).filter(QI.tenant_code == tenant)\n    if window.start is not None:",
     "    )\n    if window.start is not None:"),
    ("a rate over nothing is a measured 0% again", CONTRACT,
     "    return round(part / whole * 100) if whole else None\n\n\ndef rate1",
     "    return round(part / whole * 100) if whole else 0\n\n\ndef rate1"),
    ("a daily bar over nothing is a measured 0.0% again", CONTRACT,
     "    return round(part / whole * 100, 1) if whole else None",
     "    return round(part / whole * 100, 1) if whole else 0.0"),
    ("a window that inspected nothing calls itself measured", CONTRACT,
     '        "measured": inspected > 0,',
     '        "measured": True,'),
    ("`measured` counts rows instead of units", CONTRACT,
     '        "measured": inspected > 0,',
     '        "measured": inspections > 0,'),
    ("the breakdown rows stop being bounded by the window", CONTRACT,
     "    return (q.filter(models.QualityInspection.created_at < window.end)\n             .order_by(models.QualityInspection.id).all())",
     "    return q.order_by(models.QualityInspection.id).all()"),
    ("the Quality view's tiles go back to the whole register", ROUTES,
     "    window = oee_contract.OeeWindow(oee_contract.DEFAULT_WINDOW_DAYS)\n    plant = quality_contract.plant_quality(db, request_tenant(current_user), window)",
     "    window = oee_contract.OeeWindow(None)\n    plant = quality_contract.plant_quality(db, request_tenant(current_user), window)"),
    ("the Pareto under the headline is measured over a different span", ROUTES,
     "        return q.filter(QI.created_at >= window.start, QI.created_at < window.end)",
     "        return q"),
    ("the twin's tile goes back to the whole register", ROUTES,
     "    quality = quality_contract.plant_quality(\n        db, request_tenant(current_user),\n        oee_contract.OeeWindow(oee_contract.DEFAULT_WINDOW_DAYS))",
     "    quality = quality_contract.plant_quality(\n        db, request_tenant(current_user),\n        oee_contract.OeeWindow(None))"),
    ("the read-model keeps its own week", QUALITY,
     "WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS",
     "WINDOW_DAYS = 30"),
    ("the trend's halves overlap instead of tiling", QUALITY,
     "    prior_w = oee_contract.prior_window(window)",
     "    prior_w = oee_contract.OeeWindow(WINDOW_DAYS * 2, now=window.end)"),
    ("the trend reports a level it did not measure", QUALITY,
     '    if not current["measured"]:',
     "    if False:"),
    ("the machine cockpit's fail rate is 0% over nothing again", TWIN,
     '        "fail_rate": quality_contract.rate(failed, inspected),',
     '        "fail_rate": quality_contract.rate(failed, inspected) or 0,'),
    ("the assistant reads a missing rate aloud", ASSISTANT,
     '    if not q.get("measured"):',
     "    if False:"),
    ("the briefing compares a missing rate against its threshold", BRIEFING,
     '    if quality.get("measured") and quality["fail_rate"] >= FAIL_RATE_ALERT:',
     '    if quality["inspections"] > 0 and quality["fail_rate"] >= FAIL_RATE_ALERT:'),
    ("a series is drawn over seven dates when the window touches eight", WINDOWS,
     "    end_date = (window.end - _TICK).date()",
     "    end_date = window.end.date()"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                                  errors="replace", cwd=here, timeout=SUITE_TIMEOUT)
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
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(
            source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28]
                                                for s in failing)
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
