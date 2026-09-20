"""Mutation harness for the explained health score and the model beside it (ADR-0027).

The arithmetic here is trivial: eleven thresholds and a sum. What is fragile is
everything AROUND the arithmetic, because none of it changes a number:

  * a rule can record points it did not add, and the card still shows a score;
  * a rule can record the wrong input, and the threshold beside it still reads
    plausibly;
  * a capped score can hide the fact that it was capped;
  * a machine the scorer never saw can be presented as a machine that passed;
  * the rule score can be relabelled a model estimate, and the model's estimate
    relabelled a measurement, with every figure unchanged;
  * the caveat can fall off the model's output, and the role restriction off the
    tool that serves it.

Each of those is a lie that no assertion about a number would catch, so each has
a mutation here.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_machine_health.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_machine_health_explained.py", "test_copilot_tools_no_wider_than_routes.py",
          "test_twin.py", "test_copilot_tools.py"]

ENGINE = "predictive_engine.py"
EXPLAIN = os.path.join("ai", "machine_health.py")
TOOLS = os.path.join("ai", "tools", "factory.py")
TWIN = os.path.join("ai", "twin.py")

MUTATIONS = [
    # --- the record disagrees with the score it explains --------------------
    ("a rule records fewer points than it adds", ENGINE,
     '"Currently in breakdown", 35, machine.status == "Breakdown",',
     '"Currently in breakdown", 30, machine.status == "Breakdown",'),
    ("a rule records points it never added", ENGINE,
     '        components.append({"key": key, "label": label, "points": points if fired else 0,',
     '        components.append({"key": key, "label": label, "points": points,'),
    ("a rule records the wrong measured input", ENGINE,
     "downtime_minutes >= 120, downtime_minutes,",
     "downtime_minutes >= 120, downtime_events,"),
    ("a rule's threshold is left blank", ENGINE,
     '"min", "120 minutes or more in the risk window", "high accumulated downtime"',
     '"min", "", "high accumulated downtime"'),
    ("a rule's recorded reason stops matching its own words", ENGINE,
     '"5 or more stoppages in the risk window", "frequent downtime events"',
     '"5 or more stoppages in the risk window", "downtime"'),

    # --- the thresholds themselves -----------------------------------------
    ("the low-utilisation threshold moves by one", ENGINE,
     '"Low utilisation", 20, utilization < 40, utilization, "%"',
     '"Low utilisation", 20, utilization <= 40, utilization, "%"'),
    ("the downtime pair stops being either/or", ENGINE,
     '        elif check("downtime_moderate", "Moderate accumulated downtime", 15,',
     '        if check("downtime_moderate", "Moderate accumulated downtime", 15,'),
    ("the reject threshold loses its equality", ENGINE,
     '"High reject rate", 20, reject_rate >= 8,',
     '"High reject rate", 20, reject_rate > 8,'),
    ("open work-order pressure counts closed orders too", ENGINE,
     "        if not work_order_status.is_closed(work_order.status):",
     "        if True:"),

    # --- the cap ------------------------------------------------------------
    ("the cap becomes invisible in the points before it", ENGINE,
     '            "points_before_cap": raw_score,',
     '            "points_before_cap": min(raw_score, 100),'),
    ("a capped score stops declaring that it was capped", ENGINE,
     '            "capped": raw_score > 100,',
     '            "capped": False,'),

    # --- the explanation ----------------------------------------------------
    ("only the rules that fired are kept (the checks that passed vanish)", ENGINE,
     '            "components": components,',
     '            "components": [c for c in components if c["fired"]],'),
    ("the deductions stop being ordered by what they cost", EXPLAIN,
     '    deductions.sort(key=lambda d: -d["points"])',
     '    deductions.sort(key=lambda d: d["points"])'),
    ("a band boundary moves by one point", EXPLAIN,
     "    if health >= 80:", "    if health > 80:"),
    ("an unscored machine is presented as a healthy one", EXPLAIN,
     '        return {"health_score": None, "band": None, "band_rule": BAND_RULE, "start": START,',
     '        return {"health_score": START, "band": "Healthy", "band_rule": BAND_RULE, "start": START,'),
    ("the sentence states a number the evidence does not have", EXPLAIN,
     '    return (f"{machine_name} is at {score} out of 100 ({explanation[\'band\']}). {lost} points came "',
     '    return (f"{machine_name} is at {score + 1} out of 100 ({explanation[\'band\']}). {lost} points came "'),
    ("the note stops denying that this is machine learning", EXPLAIN,
     '        "threshold over recorded data, hand-weighted by AMP — not machine learning, and not a "',
     '        "threshold over recorded data, hand-weighted by AMP — machine learning, and a "'),
    ("the cockpit stops carrying the explanation", TWIN,
     '    detail["health_explanation"] = machine_health.explain(risk)',
     '    detail["health_explanation"] = {}'),

    # --- the rule score, relabelled -----------------------------------------
    ("the rule score claims to be a model estimate", EXPLAIN,
     '        facts.append(ev.Fact(key=f"health.cost.{d[\'key\']}", label=f"{d[\'label\']} cost",\n'
     '                             value=d["points"], provenance=ev.RULE, unit="points",',
     '        facts.append(ev.Fact(key=f"health.cost.{d[\'key\']}", label=f"{d[\'label\']} cost",\n'
     '                             value=d["points"], provenance=ev.MODEL, unit="points",'),

    # --- the model, relabelled ----------------------------------------------
    ("the model's estimate claims to be a measurement", TOOLS,
     '                           round(m["probability"] * 100, 1), ev.MODEL, "%", "amp_ai", horizon,',
     '                           round(m["probability"] * 100, 1), M, "%", "amp_ai", horizon,'),
    ("the model result claims the data state OK", TOOLS,
     '    return ev.ToolResult(tool="get_failure_risk", state=ev.MODEL_NOT_VALIDATED, summary=summary,',
     '    return ev.ToolResult(tool="get_failure_risk", state=ev.OK, summary=summary,'),
    ("the synthetic-only caveat falls off each estimate", TOOLS,
     '                           detail=f"band {m[\'band\']}; {caveat}"))',
     '                           detail=f"band {m[\'band\']}"))'),
    ("the caveat falls off the result", TOOLS,
     '                         notes=[caveat, "Not a maintenance instruction: no action here has been "',
     '                         notes=["Not a maintenance instruction: no action here has been "'),
    ("the model tool stops restricting roles", TOOLS,
     "      roles=FAILURE_RISK_ROLES)", "      roles=())"),
    ("an unavailable model produces an estimate anyway", TOOLS,
     '    if result.get("status") != "ok":', '    if False:'),
]


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace",
                              cwd=os.path.dirname(os.path.abspath(__file__)))
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
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
                s.replace("test_", "").replace(".py", "")[:24] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<62} {verdict:<10} {note}")
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
