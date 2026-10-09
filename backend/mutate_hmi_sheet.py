"""Mutate the transcription guards; every one must turn the suite red.

THE TWO DEFECTS EVERY MUTATION BELOW REINTRODUCES, in one form or another:

  A DAY COUNTED TWICE. The same machine-day reaches AMP as a MONTH-page
  photograph, an HOUR PROD. photograph and a USB export. Any two of them stored
  together ADD. The error always runs the same way -- upward -- and a plant
  manager does not audit the number that says he had a good week.

  A DAY DRAWN AS AN HOUR. A MONTH-page figure has no hour in it. Let one into
  the hourly bucket and a day's output becomes a single bar at midnight, which
  reads as data and is not.

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.

Run: python backend/mutate_hmi_sheet.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Keyed by the REPO-RELATIVE PATH each mutation names, the layout every other
# harness uses and the one test_mutation_anchors_apply.py reads.
TARGETS = {
    "backend/hmi_sheet.py": os.path.join(HERE, "hmi_sheet.py"),
    "backend/ai/plant_board.py": os.path.join(HERE, "ai", "plant_board.py"),
}
TEST = os.path.join(HERE, "test_hmi_sheet.py")

S = "backend/hmi_sheet.py"
B = "backend/ai/plant_board.py"

MUTATIONS = [
    # ── Precedence: one machine-day, one source ──────────────────────────
    ("a better source adds to the weaker one instead of replacing it", S,
     "    weaker = [k for k in PRECEDENCE if rank(k) > rank(kind)]",
     "    weaker = []"),
    ("a source deletes rows it does not outrank", S,
     "    weaker = [k for k in PRECEDENCE if rank(k) > rank(kind)]",
     "    weaker = [k for k in PRECEDENCE if k != kind]"),
    ("a photograph is allowed to overwrite the controller's own export", S,
     "    stronger = [k for k in PRECEDENCE if rank(k) < rank(kind)]",
     "    stronger = []"),
    # Anchored from `q = db.query` down, because the .like() line on its own is
    # a substring of the identically-shaped one in blocked_days four lines
    # later -- test_mutation_anchors_apply.py caught it matching twice.
    ("precedence ignores the day, so one day wipes every other", S,
     "            q = db.query(models.ProductionRecord).filter(\n"
     "                models.ProductionRecord.tenant_code == tenant,\n"
     "                models.ProductionRecord.source_record_id.like(\n"
     "                    day_prefix(k, machine_name, day) + \"%\"))",
     "            q = db.query(models.ProductionRecord).filter(\n"
     "                models.ProductionRecord.tenant_code == tenant,\n"
     "                models.ProductionRecord.source_record_id.like(\n"
     "                    f\"{k}:{machine_name}:%\"))"),
    ("an unknown source kind outranks everything instead of nothing", S,
     "    return PRECEDENCE.index(kind) if kind in PRECEDENCE else len(PRECEDENCE)",
     "    return PRECEDENCE.index(kind) if kind in PRECEDENCE else -1"),

    # ── A blank is not a zero ────────────────────────────────────────────
    ("a blank kwh becomes a measured zero", S,
     "    kwh = _parse_number(row.get(\"kwh\"), where, \"kwh\", allow_blank=True)",
     "    kwh = _parse_number(row.get(\"kwh\"), where, \"kwh\", allow_blank=True) or 0.0"),

    # ── An idle hour is not an hour of zero ──────────────────────────────
    ("an hour that made nothing is stored as a record of zero", S,
     "        if row[\"shots\"] <= 0:\n            continue",
     "        if False:\n            continue"),

    # ── The reader guesses instead of refusing ───────────────────────────
    ("a counter that went backwards is accepted", S,
     "            if shots < 0:",
     "            if False:"),
    ("a day's figure is written as though it covered an hour", S,
     "            minutes = MINUTES_IN_DAY",
     "            minutes = MINUTES_IN_HOUR"),

    # ── The summary tells the truth about what was stored ────────────────
    ("the summary counts the sheet, including days it refused to store", S,
     "        \"shots\": sum(r[\"total_count\"] for r in written),",
     "        \"shots\": sum(r[\"total_count\"] for r in rows),"),

    # ── A day is never drawn as an hour ──────────────────────────────────
    ("a day-level record is let into the hourly bucket", B,
     "    return [r for r in records if (r.planned_minutes or 0) <= HOUR_MINUTES]",
     "    return list(records)"),
    ("the hourly series is built from every record again", B,
     "    hourly = hourly_only(records)",
     "    hourly = list(records)"),
    ("the power chart buckets day-level kWh onto midnight", B,
     "    energy_points = _energy_hours(hourly, HOURS)",
     "    energy_points = _energy_hours(records, HOURS)"),
    ("a day whose hours are unknown claims they are known", B,
     "        hours_known = total == hourly_total",
     "        hours_known = True"),
    ("the day total is taken from the hours, so a MONTH-page day reads zero", B,
     "        total = day_total.get(m.id, 0)",
     "        total = hourly_total"),

    # ── The third power state, which used to not exist ───────────────────
    ("a meter that reports per day is called no meter at all", B,
     "    measured = [r for r in records if r.energy_kwh is not None]\n"
     "    if not measured:\n"
     "        return _unavailable(*NO_ENERGY)",
     "    measured = [r for r in records if r.energy_kwh is not None]\n"
     "    if not energy_points:\n"
     "        return _unavailable(*NO_ENERGY)"),
    ("the day's kWh total is shrunk to only the part that fits the chart", B,
     "        \"total\": round(sum(float(r.energy_kwh) for r in measured), 2),",
     "        \"total\": round(sum(p[\"kwh\"] or 0 for p in (energy_points or [])), 2),"),
    ("a power card with no hours claims it has them", B,
     "        \"hours_known\": energy_points is not None and round(",
     "        \"hours_known\": True or round("),
]


def run():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_hmi_mutate.db",
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
