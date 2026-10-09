"""Mutate the ARICO export importer's guards; every one must turn the suite red.

THE DEFECTS THESE REINTRODUCE all share a shape: a figure the controller did
not measure, stored as though it had.

The one this harness was written for is the ZERO. IMM-12 on the Shrinidhi floor
exported 365 days, 1,834,320 shots and exactly 0.0 kWh -- not one non-zero hour
in fourteen months, from a press that never stopped. Its ENERGY PULSE KWh
constant is 000.0, so the controller counts meter pulses and scales them by
nothing. Every one of those zeros, stored as a measurement, draws a flat line
along the axis of the power chart and says the busiest machine in the plant runs
for free.

The others are the same mistake at different scales: an idle hour written as a
record of zero output, a day counted twice because the same export was imported
again, a transcription left in place beside the export that supersedes it.

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.

Run: python backend/mutate_arico_import.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Keyed by the REPO-RELATIVE PATH each mutation names, the layout every other
# harness uses and the one test_mutation_anchors_apply.py reads.
TARGETS = {"backend/arico_import.py": os.path.join(HERE, "arico_import.py")}
TEST = os.path.join(HERE, "test_arico_import.py")

A = "backend/arico_import.py"

MUTATIONS = [
    # ── THE one this harness exists for ──────────────────────────────────
    ("an unscaled meter's 0.0 is stored as a measured zero", A,
     "    return value if value > 0 else None",
     "    return value"),
    ("a negative or zero reading squeaks through as measured", A,
     "    return value if value > 0 else None",
     "    return value if value >= 0 else None"),
    ("an energy file shorter than the day invents a reading", A,
     "    if hour >= len(kwh):\n        return None",
     "    if False:\n        return None"),

    # ── The summary has to tell the truth about what was metered ─────────
    ("the summary counts unmetered hours as metered", A,
     '        "hours_metered": sum(1 for r in rows\n'
     '                             if r["energy_kwh"] is not None),',
     '        "hours_metered": sum(1 for r in rows),'),
    ("the summary reports no unmetered hours whatever happened", A,
     '        "hours_unmetered": sum(1 for r in rows\n'
     '                               if r["energy_kwh"] is None),',
     '        "hours_unmetered": 0,'),

    # ── An idle hour is not an hour of zero output ───────────────────────
    ("an hour the machine did not run is written as a record of zero", A,
     "            if made <= 0:\n                continue",
     "            if False:\n                continue"),

    # ── Idempotency: the same export arrives every single day ────────────
    ("a re-imported export adds a second copy of every hour", A,
     "        found = existing.get(row[\"source_record_id\"])\n"
     "        if found is not None:",
     "        found = None\n"
     "        if found is not None:"),
    ("the idempotency key drops the hour, so a day keeps one row", A,
     '    return f"arico:{machine_name}:{day}:{hour:02d}"',
     '    return f"arico:{machine_name}:{day}"'),
    ("the idempotency key drops the machine, so two presses collide", A,
     '    return f"arico:{machine_name}:{day}:{hour:02d}"',
     '    return f"arico:{day}:{hour:02d}"'),

    # ── The hour is stamped at its start, not its end ────────────────────
    ("every hour is stamped at its end, shifting the whole day forward", A,
     '    return datetime.strptime(day, "%Y/%m/%d") + timedelta(hours=hour)',
     '    return datetime.strptime(day, "%Y/%m/%d") + timedelta(hours=hour + 1)'),

    # ── An export belongs to its own workspace ───────────────────────────
    ("an export is imported into somebody else's workspace", A,
     "    if machine.tenant_code != tenant:",
     "    if False:"),

    # ── The export supersedes the photograph it replaces ─────────────────
    ("the transcribed rows are left in place beside the export", A,
     '    superseded = hmi_sheet.supersede(\n'
     '        db, tenant, machine.name, sorted(production), "arico")',
     '    superseded = 0'),
]


def run():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_arico_mutate.db",
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
