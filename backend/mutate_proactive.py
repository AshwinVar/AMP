"""Mutation harness for proactive restraint (ADR-0031).

Nothing here computes a number. The whole feature is a set of decisions about
what NOT to say, and every one of them can be removed without a single figure
changing on a screen:

  * the bar can widen, so a POSSIBLE risk starts interrupting people;
  * the cooldown can stop working, so the same thing announces itself every run;
  * the cap can leak, so one bad morning produces thirty notifications;
  * the held-back list can be emptied, so the restraint becomes unauditable;
  * the read can start writing, so opening a dashboard notifies the plant.

That last one is the difference between a product people keep and a product
people mute, so each has a mutation here.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_proactive.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_proactive.py", "test_copilot_tools.py", "test_read_model_routes.py"]

PA = os.path.join("ai", "proactive.py")
TOOLS = os.path.join("ai", "tools", "factory.py")

MUTATIONS = [
    # --- the bar ------------------------------------------------------------
    ("a POSSIBLE risk starts interrupting people", PA,
     '        likely = r.get("likelihood") == ev.LIKELY', "        likely = True"),
    ("a ranked-but-not-live problem starts interrupting people", PA,
     '        live = p.get("rank_basis") == "stopped now"', "        live = True"),
    ("an outcome that IMPROVED is raised as though it got worse", PA,
     '        worse = o["verdict"] == "WORSE"', '        worse = o["verdict"] != "WORSE"'),
    ("an outcome still inside its window is raised", PA,
     '        if o.get("waiting") or not o.get("verdict"):\n            continue',
     "        if False:\n            continue"),

    # --- the cooldown -------------------------------------------------------
    ("the cooldown stops working", PA,
     "        elif c[\"signature\"] in recent:", "        elif False:"),
    ("the cooldown window is ignored when reading what was said", PA,
     "                    models.Notification.created_at >= since)",
     "                    models.Notification.created_at >= since - timedelta(days=3650))"),
    ("signatures stop being stable, so nothing ever matches", PA,
     '            f"problem:{p[\'key\']}", "Machine" if live else "Problem",',
     '            f"problem:{p[\'key\']}:{id(p)}", "Machine" if live else "Problem",'),

    # --- the cap ------------------------------------------------------------
    ("the cap leaks, so one bad morning sends everything", PA,
     "    over_cap = qualified[MAX_PER_RUN:]\n    qualified = qualified[:MAX_PER_RUN]",
     "    over_cap = []\n    qualified = qualified"),
    ("what the cap held back is dropped instead of reported", PA,
     '        suppressed.append({**c, "suppressed_by": OVER_THE_CAP,', "        _ = ({"),

    # --- the audit ----------------------------------------------------------
    ("the held-back list is emptied, so the restraint cannot be audited", PA,
     '        "suppressed": suppressed,', '        "suppressed": [],'),
    ("a suppression stops saying which rule held it back", PA,
     '            suppressed.append({**c, "suppressed_by": BELOW_THE_BAR})',
     '            suppressed.append({**c, "suppressed_by": None})'),
    ("Critical stops going first when the cap has to choose", PA,
     '    qualified.sort(key=lambda c: 0 if c["severity"] == "Critical" else 1)',
     '    qualified.sort(key=lambda c: 1 if c["severity"] == "Critical" else 0)'),

    # --- reading must not write ----------------------------------------------
    ("the read starts writing, so opening a dashboard notifies the plant", PA,
     "    plan = build_proactive(db, tenant, now=at)\n    for c in plan[\"qualified\"]:",
     "    plan = build_proactive(db, tenant, now=at)\n    for c in plan[\"qualified\"] + plan[\"suppressed\"]:"),
    ("a quiet plant is reported as all clear rather than as held back", PA,
     '        headline = (f"Nothing worth interrupting you for. {len(suppressed)} thing"',
     '        headline = ("All good, nothing to report." + f"{\'\'}{len(suppressed) and \'\'}"'),

    # --- the tool -------------------------------------------------------------
    ("the tool hides how much it held back", TOOLS,
     '        _fact("raise.held_back", "Held back", len(p["suppressed"]), R, "findings",',
     '        _fact("raise.held_back", "Held back", 0, R, "findings",'),

    # --- the sentence has to agree with the state ---------------------------
    # NO DATA says AMP had nothing to look at. Any wording that instead reports
    # on the plant is a claim it has not earned, and a brand-new workspace is
    # exactly who reads it.
    ("an empty workspace is told it has no problems", PA,
     'ev.NO_DATA, ("Nothing reached AMP to consider raising. That is not the "\n'
     '                                       "same as a clear plant: with nothing to look at, there is "\n'
     '                                       "nothing to hold back either.")',
     'ev.NO_DATA, "There is nothing to raise: no problems and no risks."'),
    ("the empty workspace stops denying that the plant is clear", PA,
     '                                       "same as a clear plant: with nothing to look at, there is "',
     '                                       "same as this: with nothing to look at, there is "'),
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
    print(f"{'mutation':<64} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<64} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
                s.replace("test_", "").replace(".py", "")[:22] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<64} {verdict:<10} {note}")
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
