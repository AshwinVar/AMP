"""Mutation harness for the OEM disclosure floor (ADR-0033).

The arithmetic is a mean. What matters is the rule around it: an average over
ONE customer is that customer's reading with a new label, and every way of
walking round that rule changes no number on screen.

  * the floor can count machines instead of customers, and ten machines at one
    site then publish that site's operation;
  * the floor can be lowered, or dropped;
  * a withheld figure can become a zero, or simply vanish;
  * the per-model slice can skip the floor, which is the obvious way round it;
  * a customer's code can appear beside a shared figure.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_intelligence.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_intelligence.py"]

OI = os.path.join("ai", "oem_intelligence.py")

MUTATIONS = [
    # --- the floor ----------------------------------------------------------
    ("the floor counts MACHINES instead of customers", OI,
     "    customers = len(contributing)\n    machines = sum(len(v) for v in contributing.values())",
     "    machines = sum(len(v) for v in contributing.values())\n    customers = machines"),
    ("the floor is lowered to one customer", OI,
     "MIN_CUSTOMERS = 2", "MIN_CUSTOMERS = 1"),
    ("the floor is dropped entirely", OI,
     "    if customers < MIN_CUSTOMERS:", "    if False:"),

    # --- what a withheld figure looks like ----------------------------------
    ("a withheld figure becomes a zero", OI,
     '        return {"label": label, "value": None, "customers": customers, "machines": machines,',
     '        return {"label": label, "value": 0.0, "customers": customers, "machines": machines,'),
    ("a withheld figure stops saying it was withheld", OI,
     '                "withheld": True,', '                "withheld": False,'),
    ("a withheld figure loses its reason", OI,
     '                "reason": ("no customer shares this" if customers == 0\n'
     '                           else WITHHELD.format(n=MIN_CUSTOMERS))}',
     '                "reason": None}'),
    ("the withheld FACT stops being UNKNOWN", OI,
     '            facts.append(_fact(f"oem.{key}", pooled["label"], None, ev.UNKNOWN, "",',
     '            facts.append(_fact(f"oem.{key}", pooled["label"], 0, ev.DERIVED, "",'),

    # --- the slice ----------------------------------------------------------
    ("the per-model slice skips the floor", OI,
     '        pooled = _pooled(hours_by_model[model_id], "Average operating hours")',
     '        pooled = {"label": "Average operating hours", "value": 1.0, "customers": 1,\n'
     '                  "machines": 1, "state": ev.OK, "withheld": False, "reason": None}'),

    # --- consent ------------------------------------------------------------
    ("hours are read without the grant", OI,
     "        if oem_sharing.SHARE_OPERATING_HOURS in g and inst.operating_hours is not None:",
     "        if inst.operating_hours is not None:"),
    ("utilisation is read without the grant", OI,
     "        if oem_sharing.SHARE_MACHINE_HEALTH in g:", "        if True:"),

    # --- the fleet boundary --------------------------------------------------
    ("the fleet stops being filtered to this manufacturer", OI,
     "    installations = oem_sharing.installations_for(db, oem_code)",
     "    installations = db.query(models.MachineInstallation).all()"),

    # --- coverage -------------------------------------------------------------
    ("coverage claims every customer shares", OI,
     '        "customers_sharing_anything": len(sharing_customers),',
     '        "customers_sharing_anything": len(customers),'),
    ("a fleet where nobody shares is reported as OK", OI,
     "    elif not sharing_customers:", "    elif False:"),

    # --- the margin gives back what the cell withheld (§7) -------------------
    # A floor on each cell is not a floor on the table. Each of these restores
    # the one-equation-one-unknown table that hands the withheld figure back.
    ("the complementary suppression is skipped entirely", OI,
     "    complementary_suppression(model_rows, hours)", "    pass"),
    ("one withheld slice is left solvable", OI,
     "    if len(withheld) != 1:", "    if True:"),
    ("a withheld slice that is not in the margin costs a good row anyway", OI,
     '    withheld = [r for r in model_rows if r["average_operating_hours"]["withheld"]\n'
     '                and r["average_operating_hours"]["machines"] > 0]',
     '    withheld = [r for r in model_rows if r["average_operating_hours"]["withheld"]]'),
    ("the suppressed complement keeps its value", OI,
     '        "value": None, "withheld": True, "state": ev.PARTIAL_DATA, "reason": COMPLEMENT})',
     '        "withheld": True, "state": ev.PARTIAL_DATA, "reason": COMPLEMENT})'),
    ("the suppressed complement stops saying it was suppressed", OI,
     '        "value": None, "withheld": True, "state": ev.PARTIAL_DATA, "reason": COMPLEMENT})',
     '        "value": None, "withheld": False, "state": ev.OK, "reason": COMPLEMENT})'),
    ("a margin over a single unknown is published anyway", OI,
     '        fleet.update({"value": None, "withheld": True, "state": ev.PARTIAL_DATA,\n'
     '                      "reason": MARGIN_IS_THE_CELL})\n        return',
     '        return'),
    ("the suppression runs even when the margin is already withheld", OI,
     '    if fleet.get("withheld"):\n        return                                  # no margin to subtract from',
     '    if False:\n        return                                  # no margin to subtract from'),
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


EXPECTED_SURVIVORS = {
    # oem_sharing.visible_machine opens with `if SHARE_MACHINE_HEALTH not in
    # grants: return None`, so the rule is enforced by the module that OWNS it
    # and this module's check is only an optimisation that avoids a pointless
    # call. Removing it changes nothing a customer could observe -- test section
    # 4b proves the end behaviour holds either way: a customer that shares hours
    # but not health still contributes no utilisation. Defence in depth, working
    # as intended; investigated, not waved through.
    "utilisation is read without the grant":
        "shadowed by oem_sharing.visible_machine, which enforces the same grant",
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
