"""Mutation harness for the Daily Factory Brief (ADR-0028).

A brief is prose, and prose is where a product lies most easily: a sentence
carries no provenance, so a figure can be invented, a gap can be dropped and a
caveat can be softened without anything looking broken. None of the mutations
below changes an engine's output. Every one of them changes only what the brief
SAYS about it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_daily_brief.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_daily_brief.py", "test_copilot_tools.py", "test_read_model_routes.py"]

BRIEF = os.path.join("ai", "brief.py")

MUTATIONS = [
    # --- a figure that no engine produced ----------------------------------
    ("the brief scales a figure on its way into prose", BRIEF,
     "{out['good']:,} good units of", "{out['good'] * 2:,} good units of"),
    ("an unsized problem is given a number", BRIEF,
     '            cost = "a size AMP cannot measure"',
     '            cost = "0 units"'),
    ("a shift with no target is reported as 0% attainment", BRIEF,
     '        said = f"{att}% of target" if att is not None else "no target set, so no attainment"',
     '        said = f"{att or 0}% of target"'),

    # --- the window --------------------------------------------------------
    ("the brief stops saying what window it covers", BRIEF,
     '''        "window": f"the last {cc.get('days')} days, ending {at.date().isoformat()}",''',
     '''        "window": "today",'''),
    ("the brief claims a shift boundary AMP does not know", BRIEF,
     'f"in the last {out[\'days\']} days."', '"since 6am this shift."'),

    # --- the blind spots ---------------------------------------------------
    ("the coverage gap is reported the wrong way round", BRIEF,
     "    if expected and reporting is not None and reporting < expected:",
     "    if expected and reporting is not None and reporting >= expected:"),
    ("the money gap is dropped", BRIEF,
     '    if not cost.get("priced"):',
     "    if False:"),
    ("unlogged stoppage time stops being reported", BRIEF,
     '    if rc.get("unattributed_units"):\n        # NOT MEASURED',
     '    if False:\n        # NOT MEASURED'),
    ("a brief with nothing missing says nothing about what it cannot see", BRIEF,
     '    if not out:\n        out.append({"key": "none"',
     '    if False:\n        out.append({"key": "none"'),
    ("unlogged time is relabelled PARTIAL DATA", BRIEF,
     '        out.append({"key": "unlogged", "state": ev.NOT_MEASURED,',
     '        out.append({"key": "unlogged", "state": ev.PARTIAL_DATA,'),

    # --- the state ---------------------------------------------------------
    ("a blind spot relabels the whole brief", BRIEF,
     '        "state": _worst([s["state"] for s in sections]),',
     '        "state": _worst([s["state"] for s in sections] + [s["state"] for s in spots]),'),
    ("the brief always reports itself as OK", BRIEF,
     '        "state": _worst([s["state"] for s in sections]),',
     '        "state": ev.OK,'),

    # --- the headline ------------------------------------------------------
    ("the headline stops admitting what AMP could not see", BRIEF,
     '''        base += f" {len(partial)} thing{'s' if len(partial) != 1 else ''} AMP could not see — see the last section."''',
     '        base += ""'),

    # --- the actions -------------------------------------------------------
    ("the actions stop saying a person decides", BRIEF,
     '    lines.append("Nothing here runs by itself: each one waits for a person to approve it.")',
     '    lines.append("AMP will handle these automatically.")'),

    # --- the Copilot's own count -------------------------------------------
    ("the tool counts every gap as one AMP could see", os.path.join("ai", "tools", "factory.py"),
     '    blind = [s for s in b["blind_spots"] if s["state"] != ev.OK]',
     '    blind = []'),
]


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=os.path.dirname(os.path.abspath(__file__)))
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
    print(f"{'mutation':<58} {'verdict':<10} caught by")
    print("-" * 100)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<58} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<58} {verdict:<10} {note}")
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
