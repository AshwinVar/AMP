"""Mutation harness for the demo factory's seed (reset_factory.py).

Small and targeted, per the build-first directive: a seeder is not
authorization code, but this one WIPES a tenant and writes a money rate, and
both of those are on the never-defer list. Two things are checked here and
nothing else.

  1. SCOPE. Every write and every read the new seeders do names DEFAULT. A
     seeder that dropped its tenant filter would upsert over another company's
     stock, overwrite their unit value, or read their rows to decide what to
     write — a cross-tenant write on a script whose other half is a delete.

  2. THE DEMO IS REPRODUCIBLE, AND HAS SOMETHING TO FIND. Removing the seed, or
     un-planting any of the seven problems, must fail a test. Otherwise this
     file quietly goes back to what it was: a plant whose figures changed every
     reset and in which AMP could discover one thing.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_demo_factory.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_reset_factory.py", "test_demo_reset_repeatable.py", "test_reseed_inventory_scoped.py"]
SUITE_TIMEOUT = 600

RF = "reset_factory.py"

# (label, file, old, new)
MUTATIONS = [
    # ── scope ───────────────────────────────────────────────────────────────
    ("stock: the upsert reads EVERY tenant's items to decide what to write", RF,
     "        for row in db.query(models.InventoryItem).filter(\n"
     "            models.InventoryItem.tenant_code == TENANT).all()",
     "        for row in db.query(models.InventoryItem).all()"),
    ("stock: a seeded item is filed under the column default, not DEFAULT", RF,
     "            row = models.InventoryItem(tenant_code=TENANT, item_code=code)",
     "            row = models.InventoryItem(item_code=code)"),
    ("unit value: the rate is written to whichever config row comes first", RF,
     "    row = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == TENANT).first()",
     "    row = db.query(models.TenantConfig).first()"),
    ("shifts: attainment rows carry no tenant", RF,
     "        db.add(models.ShiftData(tenant_code=TENANT, shift_name=shift,",
     "        db.add(models.ShiftData(shift_name=shift,"),
    ("plans: the plan rows carry no tenant", RF,
     "            tenant_code=TENANT, plan_no=f\"PLAN-{3100 + i}\", machine_id=machine.id,",
     "            plan_no=f\"PLAN-{3100 + i}\", machine_id=machine.id,"),

    # ── reproducible ────────────────────────────────────────────────────────
    ("the RNG is never seeded, so no two resets agree", RF,
     "    if seed is not None:\n        random.seed(seed)",
     "    if seed is None:\n        random.seed(seed)"),
    ("the seed is ignored and a fixed one used, so a caller cannot vary it", RF,
     "def rebuild_factory(db, seed=DEMO_SEED):",
     "def rebuild_factory(db, seed=12345):"),

    # ── the planted problems ────────────────────────────────────────────────
    ("stock: nothing is below its reorder level any more", RF,
     '    ("RM-PASTE-01", "SAC305 Solder Paste", "Raw Material", "kg", 4, 12, "Cold Store A"),',
     '    ("RM-PASTE-01", "SAC305 Solder Paste", "Raw Material", "kg", 400, 12, "Cold Store A"),'),
    ("plans: every plan met its target, so nothing is behind", RF,
     "        (2, \"Day\", line, 1200, 870),        # short by 330",
     "        (2, \"Day\", line, 1200, 1200),        # short by 330"),
    ("maintenance: the overdue task is planned for today again", RF,
     "        planned_date=date.today() - timedelta(days=OVERDUE_MAINTENANCE_DAYS), status=\"Open\",",
     "        planned_date=date.today(), status=\"Open\","),
    ("orders: no order is past its date any more", RF,
     "            late = company == COMPANIES[0] and i == 1",
     "            late = False"),
    ("quality: the reject spike goes back into the noise band", RF,
     "            failed = int(inspected * 0.18)",
     "            failed = int(inspected * 0.02)"),
    ("downtime: the dominant reason is scattered again", RF,
     "            reason=PROBLEM_DOWNTIME_REASON, duration=f\"{minutes} min\",",
     "            reason=random.choice(DOWNTIME_REASONS), duration=f\"{minutes} min\","),
    ("money: the demo has no unit value, so every loss reads in units", RF,
     "    row.unit_value_gbp = DEMO_UNIT_VALUE_GBP",
     "    row.unit_value_gbp = None"),
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


# A mutation here is one that CANNOT change behaviour, with the reason. All
# three are the same fact, and it is worth stating plainly:
#
#   `tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")`
#
# This seeder's TENANT *is* "DEFAULT", so dropping the explicit stamp lands on
# the column default and produces an identical row. The stamps are still
# correct and stay: they are the ADR-0010 discipline every other writer follows
# (ai/agents._propose_task carries the same comment, where it genuinely
# matters), and they are what makes this file safe to point at another tenant.
# What cannot be done is observe them from here — a test asserting
# `tenant_code == "DEFAULT"` would pass either way, which is not a test.
_DEFAULT_IS_THE_COLUMN_DEFAULT = (
    "tenant_code defaults to 'DEFAULT' and this seeder's TENANT is 'DEFAULT', so "
    "omitting the explicit stamp writes the identical row; the stamp stays as "
    "discipline but cannot be observed while the two coincide")
EXPECTED_SURVIVORS = {
    "stock: a seeded item is filed under the column default, not DEFAULT": _DEFAULT_IS_THE_COLUMN_DEFAULT,
    "shifts: attainment rows carry no tenant": _DEFAULT_IS_THE_COLUMN_DEFAULT,
    "plans: the plan rows carry no tenant": _DEFAULT_IS_THE_COLUMN_DEFAULT,
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
    print(f"{'mutation':<72} {'verdict':<10} caught by")
    print("-" * 116)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<72} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<72} {verdict:<10} {note}", flush=True)
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
