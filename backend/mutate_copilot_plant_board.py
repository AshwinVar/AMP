"""Mutate the plant-board tools' honesty guards; every one must turn the suite red.

THE DEFECT EACH MUTATION REINTRODUCES is the same one: a figure AMP cannot
derive, shipped as a zero. `ai/plant_board.py` hands the tool 0.0 for every
conversion it could not do and marks power and packing unavailable, because a
chart needs a number to draw. The tool's job is to not pass those on. A plant
that consumed no material, earned nothing and drew no power reads exactly like a
plant with no part spec and no energy meter, once both are figures in a sentence
-- and only one of them is true.

So each mutation below turns one UNKNOWN back into a 0, or one honest data state
back into OK, and test_copilot_plant_board_tool.py must notice.

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.

Run: python backend/mutate_copilot_plant_board.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Keyed by the REPO-RELATIVE PATH each mutation names, the layout every other
# harness uses and the one test_mutation_anchors_apply.py reads.
TARGETS = {"ai/tools/factory.py": os.path.join(HERE, "ai", "tools", "factory.py")}
TEST = os.path.join(HERE, "test_copilot_plant_board_tool.py")

F = "ai/tools/factory.py"

MUTATIONS = [
    # ── THE one this harness exists for ──────────────────────────────
    # An unavailable power figure becomes a measured zero: "no meter is fitted"
    # turned into "0 kWh were consumed", which is the exact claim the plant board
    # was built to refuse.
    ("an unavailable power figure is shipped as a measured 0", F,
     'label, None, U, unit, "no source is connected"',
     'label, 0, M, unit, "no source is connected"'),

    # ── The builder's own zeros, passed through ──────────────────────
    ("the builder's 0.0 kg for a machine with no spec is passed through", F,
     '.get("kg_total") if material else None,',
     '.get("kg_total"),'),
    ("an unpriced machine is treated as priced, so its rate becomes 0", F,
     'priced = bool((sr.get(mid) or {}).get("priced")) and bool(blocks)',
     'priced = bool(blocks)'),

    # ── The plant-level figures ──────────────────────────────────────
    ("a plant with no part spec reports 0 kg instead of an unknown", F,
     '"board.kg", "Material consumed", None, U, "kg"',
     '"board.kg", "Material consumed", 0.0, D, "kg"'),
    ("a plant with nothing priced reports a day's value of 0", F,
     '"Value of the day\'s output", None, U, CURRENCY',
     '"Value of the day\'s output", 0.0, D, CURRENCY'),
    ("a plant with nothing priced reports a best shift-hour rate of 0", F,
     '"Highest shift-hour rate", None, U, CURRENCY',
     '"Highest shift-hour rate", 0.0, D, CURRENCY'),
    ("a plant with no declared cycle reports 0 hours below target, not unrated", F,
     '"Hours below target", None, U, "hours"',
     '"Hours below target", 0, M, "hours"'),

    # ── The per-machine figures ──────────────────────────────────────
    ("a machine with no spec reports 0 kg of material", F,
     'material consumed", None, U, "kg"',
     'material consumed", 0.0, D, "kg"'),
    ("a machine with no price reports a shift-hour rate of 0", F,
     'f"{r[\'machine\']} shift-hour rate", None, U, CURRENCY',
     'f"{r[\'machine\']} shift-hour rate", 0.0, D, CURRENCY'),

    # ── The month tables ─────────────────────────────────────────────
    ("a month with no part spec reports 0 kg", F,
     '"month.kg", "Material consumed", None, U, "kg"',
     '"month.kg", "Material consumed", 0.0, D, "kg"'),
    ("a month with nothing priced reports revenue of 0", F,
     '"Value of the month\'s output", None, U, CURRENCY',
     '"Value of the month\'s output", 0.0, D, CURRENCY'),
    ("a month with nothing priced reports a plant rate of 0", F,
     '"Shift-hour rate for the plant", None, U,',
     '"Shift-hour rate for the plant", 0.0, D,'),

    # ── The data states, which are the same claim at result level ────
    ("a board with no conversions at all calls itself OK", F,
     'ev.NOT_MEASURED if not specced and not priced',
     'ev.OK if True'),
    ("a month with no conversions at all calls itself OK", F,
     'ev.NOT_MEASURED if not materials and not machines',
     'ev.OK if True'),
    ("the power answer calls itself OK, so 'not measured' stops being the answer", F,
     '"get_plant_power", ev.NOT_MEASURED',
     '"get_plant_power", ev.OK'),
]


def run():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_board_mutate.db",
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, TEST], capture_output=True, text=True,
                          timeout=300, env=env, cwd=os.path.dirname(HERE)).returncode


def main():
    originals = {}
    for k, path in TARGETS.items():
        shutil.copyfile(path, path + ".orig")
        originals[k] = open(path + ".orig", encoding="utf-8").read()
    survived = []
    try:
        if run() != 0:
            print("BASELINE IS ALREADY RED - fix that before trusting any mutant")
            return 1
        print("baseline green\n")
        for name, key, old, new in MUTATIONS:
            src = originals[key]
            if old not in src:
                print(f"  SKIP      {name}  (anchor missing - harness drifted)")
                survived.append(name + " [ANCHOR MISSING]")
                continue
            open(TARGETS[key], "w", encoding="utf-8").write(src.replace(old, new, 1))
            try:
                rc = run()
            finally:
                open(TARGETS[key], "w", encoding="utf-8").write(src)
            print(("  SURVIVED  " if rc == 0 else "  caught    ") + name)
            if rc == 0:
                survived.append(name)
    finally:
        for k, path in TARGETS.items():
            open(path, "w", encoding="utf-8").write(originals[k])
            os.remove(path + ".orig")
    print()
    if survived:
        print(f"{len(survived)}/{len(MUTATIONS)} SURVIVED - investigate WHY each did:")
        for s in survived:
            print("  -", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
