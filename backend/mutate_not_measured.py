"""Mutation harness for the six figures that still published 0 for "nothing".

Each mutation puts back one of the readings this branch removed: a rate over an
empty denominator reading 0, a flag that lies about whether anything was
measured, the command header's lifetime rate under its weekly caption, the
executive page's fourth lifetime copy of the quality rate, the unbounded cutoff
that let a future-dated action into this week. One mutation goes the other way —
turning a legitimate SUM into None — because refusing to state a figure we do
have is the same defect from the other side.

For each the harness applies the edit, runs the suites that are supposed to
notice, and restores the file byte for byte. A mutation that leaves the suites
green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_not_measured.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_rates_say_not_measured.py", "test_analytics_routes.py", "test_impact.py",
          "test_pulse.py", "test_core_routes.py", "test_summary_never_invents_oee.py"]
SUITE_TIMEOUT = 900

ROUTES = "analytics_routes.py"
CORE = "core_routes.py"
IMPACT = os.path.join("ai", "impact.py")
PULSE = os.path.join("ai", "pulse.py")

# (label, file, old, new)
MUTATIONS = [
    ("a work order book with no target reads 0% achieved", ROUTES,
     "    achievement = round((total_actual / total_target) * 100) if total_target else None",
     "    achievement = round((total_actual / total_target) * 100) if total_target else 0"),
    ("...and says it measured that", ROUTES,
     '        "achievement_measured": total_target > 0,\n        "planned": status_counts.get("Planned", 0),',
     '        "achievement_measured": True,\n        "planned": status_counts.get("Planned", 0),'),
    ("a plan with nothing planned reads 0% achieved", ROUTES,
     "    achievement = round((actual_quantity / planned_quantity) * 100) if planned_quantity else None",
     "    achievement = round((actual_quantity / planned_quantity) * 100) if planned_quantity else 0"),
    ("no completed task reads as a 0-minute repair record", ROUTES,
     "    avg_repair = round(completed_downtime / completed) if completed else None",
     "    avg_repair = round(completed_downtime / completed) if completed else 0"),
    ("a crew that logged no unit reads 0% quality", ROUTES,
     "    quality_rate = round((good / total) * 100) if total else None",
     "    quality_rate = round((good / total) * 100) if total else 0"),
    ("no order raised reads 0% dispatched", ROUTES,
     "    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else None",
     "    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else 0"),
    ("the executive page goes back to its own lifetime quality rate", ROUTES,
     "    quality = quality_contract.plant_quality(\n        db, request_tenant(current_user),\n        oee_contract.OeeWindow(oee_contract.DEFAULT_WINDOW_DAYS))\n    inspected = quality[\"inspected\"]",
     "    quality = quality_contract.plant_quality(\n        db, request_tenant(current_user),\n        oee_contract.OeeWindow(None))\n    inspected = quality[\"inspected\"]"),
    ("an unreported plant reads 0% utilization", ROUTES,
     "    avg_utilization = round(sum(util_values) / len(util_values)) if util_values else None",
     "    avg_utilization = round(sum(util_values) / len(util_values)) if util_values else 0"),
    ("...and claims it measured every machine", ROUTES,
     '        "utilization_measured": bool(util_values),\n        "utilization_machines": len(util_values),',
     '        "utilization_measured": True,\n        "utilization_machines": len(util_values),'),
    ("the daily summary prints the utilization figure whatever it is", CORE,
     '    if summary.get("utilization_measured"):',
     "    if True:"),
    ("...and stops saying how much of the plant it covers", CORE,
     "                     + (f\" (from {covered} of {total_machines} machines)\"\n                        if covered is not None and total_machines and covered != total_machines\n                        else \"\"))",
     "                     )"),
    ("an agent fleet that has decided nothing reads 0% autonomous", IMPACT,
     "    return round(part / whole * 100) if whole else None",
     "    return round(part / whole * 100) if whole else 0"),
    ("the fleet claims it measured a decision it never made", IMPACT,
     '        "auto_measured": decided > 0,',
     '        "auto_measured": True,'),
    ("the recent slice stops being bounded at the far end", IMPACT,
     "                      models.AgentAction.created_at >= window.start,\n                      models.AgentAction.created_at < window.end)",
     "                      models.AgentAction.created_at >= window.start)"),
    ("the recent slice cuts its own week again", IMPACT,
     "WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS",
     "WINDOW_DAYS = 30"),
    ("the week's autonomy rate is taken over every decision ever made", IMPACT,
     '            "auto_rate": _rate(recent_auto, recent_decided),',
     '            "auto_rate": _rate(auto, decided),'),
    ("the command header shows the lifetime rate under its weekly caption", PULSE,
     '            "auto_rate": imp["last_7_days"]["auto_rate"],',
     '            "auto_rate": imp["auto_rate"],'),
    # The rule's own boundary, mutated from the other side: a SUM over an empty
    # set IS zero, and refusing to state it is the same defect reversed.
    ("a cost register with no row refuses to say it spent nothing", ROUTES,
     "    total_cost = int(db.query(func.coalesce(func.sum(models.CostRecord.amount), 0)).scalar() or 0)",
     "    total_cost = int(db.query(func.sum(models.CostRecord.amount)).scalar() or 0) or None"),
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
