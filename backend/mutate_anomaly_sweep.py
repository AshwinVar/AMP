"""Mutation harness for the fleet anomaly sweep (ADR-0032).

The sweep computes nothing: it calls the same scorer the single-machine route
calls and arranges the answers. Everything that could make it dishonest is in
that arrangement, and none of it changes a score:

  * a machine AMP could not look at can vanish from the list, and then reads as
    a machine that was fine;
  * "no reading" can become a zero, and then reads as "quiet";
  * a consent refusal can become an empty list, and then reads as "nothing
    unusual";
  * a preview can start fitting baselines from a customer's telemetry;
  * an experimental model's score can lose its caveat, its MODEL ESTIMATE label
    or its MODEL NOT VALIDATED state, and then reads as a measurement.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_anomaly_sweep.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_anomaly_sweep.py", "test_copilot_tools.py",
          "test_copilot_tools_no_wider_than_routes.py"]

SW = os.path.join("ai", "anomaly_sweep.py")
TOOLS = os.path.join("ai", "tools", "factory.py")

MUTATIONS = [
    # --- every machine must appear -----------------------------------------
    ("machines AMP could not score are dropped from the list", SW,
     '        "machines": scored + unscored,', '        "machines": scored,'),
    ("an unscored machine is reported as a zero", SW,
     '    base = {"machine_id": machine.id, "name": machine.name, "line": machine.line or "",\n'
     '            "score": None, "state": ev.NOT_MEASURED, "reason": None, "facts": []}',
     '    base = {"machine_id": machine.id, "name": machine.name, "line": machine.line or "",\n'
     '            "score": 0.0, "state": ev.NOT_MEASURED, "reason": None, "facts": []}'),
    ("thin history stops saying how thin", SW,
     '        base["reason"] = (f"not enough history yet (have {have[short]} of {needed[short]} {short})"\n'
     '                          if short else "not enough history yet")',
     '        base["reason"] = "not enough history yet"'),
    ("thin history is relabelled as simply not measured", SW,
     '        base["state"] = ev.INSUFFICIENT_HISTORY', '        base["state"] = ev.NOT_MEASURED'),

    # --- consent ------------------------------------------------------------
    ("a consent refusal becomes an empty list", SW,
     '    if not consent_ok:\n        return {"generated_at": at.isoformat(), "state": ev.NOT_CONFIGURED, "headline": NO_CONSENT,',
     '    if False:\n        return {"generated_at": at.isoformat(), "state": ev.NOT_CONFIGURED, "headline": NO_CONSENT,'),
    ("the consent refusal is swallowed per machine instead of stopping the sweep", SW,
     "        except ConsentRequired:\n", "        except NotImplementedError:\n"),
    ("a preview starts fitting baselines from a customer's telemetry", SW,
     "    if previewing:\n", "    if False:\n"),
    ("each machine is scored at the wall clock, not the sweep's instant", SW,
     "            rows.append(_row(m, scorer(db, tenant, m.id, gate=gate, now=at)))",
     "            rows.append(_row(m, scorer(db, tenant, m.id, gate=gate)))"),

    # --- the experimental model is not an alarm -----------------------------
    ("a score is relabelled a measurement", SW,
     "                  round(float(score), 2), ev.MODEL, \"\", \"telemetry baseline\",",
     "                  round(float(score), 2), ev.MEASURED, \"\", \"telemetry baseline\","),
    ("a scored row claims the model was validated", SW,
     '        "state": ev.MODEL_NOT_VALIDATED,\n        "reason": None,',
     '        "state": ev.OK,\n        "reason": None,'),
    ("the caveat falls off the score", SW,
     "                  detail=NOT_ADOPTED),", "                  detail=\"\"),"),
    ("the note stops saying the model did not beat the rules", SW,
     'NOT_ADOPTED = ("This check is experimental: on held-out synthetic data it did not beat the rules AMP "\n'
     '               "already uses, so it is shown as an estimate and never as an alarm.")',
     'NOT_ADOPTED = ("AMP checked the telemetry and raised the machines below as alarms.")'),
    ("a fleet with nothing scorable is reported as normal", SW,
     '        headline = (f"None of {len(machines)} machines could be scored yet. Every one says why "\n'
     '                    "below; none of them is being reported as normal.")',
     '        headline = "Nothing unusual across the fleet."'),

    # --- the tool -----------------------------------------------------------
    ("the tool hides how many machines it could not score", TOOLS,
     '        _fact("anomaly.not_scored", "Machines it could not score", s["not_scored"], M, "machines",',
     '        _fact("anomaly.not_scored", "Machines it could not score", 0, M, "machines",'),
    ("the tool stops restricting roles", TOOLS,
     '      mirrors="/ai/native/anomaly/sweep", view="machines", domain="machines",\n'
     "      roles=FAILURE_RISK_ROLES)",
     '      mirrors="/ai/native/anomaly/sweep", view="machines", domain="machines",\n'
     "      roles=())"),
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
    print(f"{'mutation':<66} {'verdict':<10} caught by")
    print("-" * 106)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<66} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<66} {verdict:<10} {note}")
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
