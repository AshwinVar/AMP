"""Mutation harness for the closed loop (ADR-0029).

The arithmetic is a subtraction. Everything that could make this table dangerous
is in the rules AROUND the subtraction, and none of those changes a number on
screen:

  * a rejected action could be followed up as though it had happened;
  * the baseline could be read from the wrong side of the decision;
  * "no reading" could quietly become zero, turning an absence into a result;
  * a verdict could appear before the window has elapsed;
  * a frozen reading could drift on every page load;
  * the caveat could fall off, or grow into a causal claim;
  * an approval a person already made could start depending on the follow-up.

Each of those is a way for AMP to say something it has not earned, so each has a
mutation here.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_action_outcomes.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_action_outcomes.py", "test_migration_0011_action_outcomes.py",
          "test_copilot_tools.py", "test_agent_actions.py"]

OUT = os.path.join("ai", "outcomes.py")
AGENTS = os.path.join("ai", "agents.py")
TOOLS = os.path.join("ai", "tools", "factory.py")

MUTATIONS = [
    # --- what gets followed up ---------------------------------------------
    ("a rejected action is followed up as though it had happened", AGENTS,
     "    if approve:\n        # CLOSE THE LOOP", "    if True:\n        # CLOSE THE LOOP"),
    ("an action kind with no metric is followed up anyway", OUT,
     '    metric = METRICS.get(action.ref_kind)\n    if metric is None:\n        return None\n    existing',
     '    metric = METRICS.get(action.ref_kind) or "downtime_minutes"\n    if False:\n        return None\n    existing'),
    ("the approval starts depending on the follow-up", AGENTS,
     "        try:\n            outcomes.record_baseline(db, action.tenant_code, action)\n"
     "        except Exception:   # noqa: BLE001 - the decision is what matters",
     "        if True:\n            outcomes.record_baseline(db, action.tenant_code, action)\n"
     "        if False:   # noqa: BLE001 - the decision is what matters"),

    # --- which window ------------------------------------------------------
    ("the baseline is read from AFTER the decision", OUT,
     "        baseline_value=measure(db, tenant, metric, scope_id, at - timedelta(days=WINDOW_DAYS), at),",
     "        baseline_value=measure(db, tenant, metric, scope_id, at, at + timedelta(days=WINDOW_DAYS)),"),
    ("a verdict is given before the window has elapsed", OUT,
     "        if at < end:\n            continue", "        if False:\n            continue"),

    # --- frozen means frozen -----------------------------------------------
    ("a frozen reading is recomputed on every read", OUT,
     "                   models.ActionOutcome.measured_at.is_(None))",
     "                   models.ActionOutcome.id.isnot(None))"),
    ("the freeze is never committed", OUT,
     "    if frozen:\n", "    if False:\n"),

    # --- null is not zero --------------------------------------------------
    ("a missing reading becomes a zero", OUT,
     "    if baseline is None or measured is None:\n        return NOT_MEASURABLE",
     "    baseline = 0.0 if baseline is None else baseline\n"
     "    measured = 0.0 if measured is None else measured\n    if False:\n        return NOT_MEASURABLE"),
    ("an empty downtime window returns nothing instead of zero", OUT,
     "        return 0.0\n    return float(sum(", "        return None\n    return float(sum("),

    # --- the verdict itself ------------------------------------------------
    ("the noise floor is dropped, so any wobble is a result", OUT,
     '    if abs(delta) <= max(spec["floor"], abs(baseline) * NOISE_FRACTION):',
     "    if False:"),
    ("the direction of good is inverted", OUT,
     '    improved = delta < 0 if spec["better"] == "lower" else delta > 0',
     '    improved = delta > 0 if spec["better"] == "lower" else delta < 0'),

    # --- the caveat --------------------------------------------------------
    ("the change is relabelled a measurement", OUT,
     'round(row.measured_value - row.baseline_value, 1), ev.CORRELATION,',
     'round(row.measured_value - row.baseline_value, 1), ev.MEASURED,'),
    ("the caveat falls off the change fact", OUT,
     "                               spec[\"unit\"], \"the two measurements above\", window,\n"
     "                               detail=CAUSATION_NOTE))",
     "                               spec[\"unit\"], \"the two measurements above\", window))"),
    ("the caveat becomes a causal claim", OUT,
     'CAUSATION_NOTE = ("AMP measured what changed after the decision. It cannot show the action caused "\n'
     '                  "the change: nothing else in the plant was held still.")',
     'CAUSATION_NOTE = ("AMP measured what the action achieved: the change below happened because of it.")'),
    ("the Copilot drops the caveat from its sentence", OUT,
     'return f"{summary[\'headline\']} {CAUSATION_NOTE}", "agentactivity"',
     'return summary["headline"], "agentactivity"'),

    # --- the tool ----------------------------------------------------------
    ("the tool claims OK when nothing has been measured", TOOLS,
     '    state = s["state"] if s["state"] in ev.DATA_STATES else ev.OK\n'
     '    return _result("get_action_outcomes"',
     '    state = ev.OK\n    return _result("get_action_outcomes"'),
]


def run_suites():
    failed = []
    for suite in SUITES:
        if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), suite)):
            continue
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=os.path.dirname(os.path.abspath(__file__)))
        if proc.returncode != 0:
            failed.append(suite)
    return failed


EXPECTED_SURVIVORS = {
    # record_baseline's metric guard is not the only one: _scope_of resolves the
    # scope through METRICS as well, and returns (None, None) for a kind it does
    # not know, so record_baseline still returns None with this guard removed.
    # A second guard already covers it — investigated, not waved through.
    "an action kind with no metric is followed up anyway":
        "shadowed by _scope_of, which resolves the scope through METRICS too",
}


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
    print(f"{'mutation':<60} {'verdict':<10} caught by")
    print("-" * 100)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<60} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<60} {verdict:<10} {note}")
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
