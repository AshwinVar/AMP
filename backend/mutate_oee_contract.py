"""Mutation harness for the canonical OEE contract (ADR-0014).

The maths was never the fragile part. The fragile parts are the HONESTY rules —
undefined vs zero, the window boundary, and the coverage disclosure — because
each of them can be removed without any arithmetic looking wrong.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oee_contract.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oee_contract.py", "test_oee.py", "test_analytics_routes.py"]

MUTATIONS = [
    # --- the formula -------------------------------------------------------
    ("availability inverted (planned/runtime)", "oee_contract.py",
     "    availability = _clamp(runtime / planned) if planned > 0 else None",
     "    availability = _clamp(planned / runtime) if runtime > 0 else None"),
    ("performance loses the seconds conversion", "oee_contract.py",
     "    performance = _clamp(ideal_seconds / (runtime * 60)) if runtime > 0 else None",
     "    performance = _clamp(ideal_seconds / runtime) if runtime > 0 else None"),
    ("quality counts rejects as good", "oee_contract.py",
     "    quality = _clamp(good / total) if total > 0 else None",
     "    quality = _clamp(total / total) if total > 0 else None"),
    ("OEE becomes a mean instead of a product", "oee_contract.py",
     "        oee = availability * performance * quality",
     "        oee = (availability + performance + quality) / 3"),

    # --- the honesty rules -------------------------------------------------
    ("an undefined component becomes a measured zero", "oee_contract.py",
     "    availability = _clamp(runtime / planned) if planned > 0 else None\n"
     "    performance = _clamp(ideal_seconds / (runtime * 60)) if runtime > 0 else None\n"
     "    quality = _clamp(good / total) if total > 0 else None",
     "    availability = _clamp(runtime / planned) if planned > 0 else 0.0\n"
     "    performance = _clamp(ideal_seconds / (runtime * 60)) if runtime > 0 else 0.0\n"
     "    quality = _clamp(good / total) if total > 0 else 0.0"),
    ("has_data goes back to 'a row exists'", "oee_contract.py",
     '        "has_data": planned > 0 or total > 0,',
     '        "has_data": True,'),
    ("the upper clamp is dropped (OEE can exceed 100%)", "oee_contract.py",
     "    return max(0.0, min(x, 1.0))", "    return max(0.0, x)"),
    ("the lower clamp is dropped (OEE can go negative)", "oee_contract.py",
     "    return max(0.0, min(x, 1.0))", "    return min(x, 1.0)"),
    ("as_percentages renders 'not measurable' as 0", "oee_contract.py",
     "        out[key] = missing if v is None else round(v * 100)",
     "        out[key] = 0 if v is None else round(v * 100)"),

    # --- the window --------------------------------------------------------
    ("the window end becomes inclusive (adjacent windows overlap)",
     "oee_contract.py",
     "    q = q.filter(models.ProductionRecord.created_at < window.end)",
     "    q = q.filter(models.ProductionRecord.created_at <= window.end)"),
    # The `>= start` filter appears in BOTH _sums and coverage, so the bare line
    # matches twice and reports SKIP. Anchored on the line that follows it in
    # _sums, which coverage does not share.
    ("the window start becomes exclusive", "oee_contract.py",
     "        q = q.filter(models.ProductionRecord.created_at >= window.start)\n"
     "    q = q.filter(models.ProductionRecord.created_at < window.end)",
     "        q = q.filter(models.ProductionRecord.created_at > window.start)\n"
     "    q = q.filter(models.ProductionRecord.created_at < window.end)"),

    # --- coverage ----------------------------------------------------------
    ("coverage counts every machine as reporting", "oee_contract.py",
     "        \"machines_reporting\": int(reporting),",
     "        \"machines_reporting\": int(expected),"),
    ("a partial measurement claims to be complete", "oee_contract.py",
     '        "complete": expected > 0 and reporting == expected,',
     '        "complete": True,'),

    # --- tenant scoping ----------------------------------------------------
    ("the sums are not filtered by tenant", "oee_contract.py",
     "    ).filter(models.ProductionRecord.tenant_code == tenant)",
     "    )"),
    ("coverage is not filtered by tenant", "oee_contract.py",
     "    expected = (db.query(func.count(models.Machine.id))\n"
     "                .filter(models.Machine.tenant_code == tenant).scalar()) or 0",
     "    expected = (db.query(func.count(models.Machine.id))\n"
     "                ).scalar() or 0"),

    # --- the wiring that closed the 90-point gap ---------------------------
    ("the AI context goes back to a row-count window", "ai_copilot.py",
     "    plant = oee_contract.plant_oee(db, tenant)",
     "    plant = oee_contract.plant_oee(db, tenant, oee_contract.OeeWindow(None))"),
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
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<56} {'verdict':<10} caught by")
    print("-" * 100)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<56} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:20] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<56} {verdict:<10} {note}")
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
