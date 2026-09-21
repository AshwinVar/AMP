"""Mutation harness for the shortage-to-production link (ADR-0030).

This module exists because ADR-0026 refused to put a number on a stock-out. The
danger in supplying that number is not arithmetic — it is the two cases getting
blurred: an item with a recipe, which can be sized, and an item without one,
which cannot. Every mutation below is a way for the second to start looking like
the first, or for the first to quietly say more than the data supports.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_shortage_impact.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_shortage_impact.py", "test_risk_radar.py", "test_copilot_tools.py",
          "test_daily_brief.py"]

SH = os.path.join("ai", "shortage.py")
RADAR = os.path.join("ai", "risk_radar.py")
TOOLS = os.path.join("ai", "tools", "factory.py")

MUTATIONS = [
    # --- no recipe, no number ----------------------------------------------
    ("an item in no recipe is given a number anyway", SH,
     "        if not lines:\n", "        if False:\n"),
    ("the unlinked items are dropped from the payload instead of listed", SH,
     '        "unlinked": unlinked,', '        "unlinked": [],'),
    ("an unsized item is reported as OK rather than NOT CONFIGURED", SH,
     '                             "state": ev.NOT_CONFIGURED, "why": NO_LINK})',
     '                             "state": ev.OK, "why": NO_LINK})'),
    ("a result with unsized items claims to be complete", SH,
     "        state = ev.PARTIAL_DATA if unlinked else ev.OK",
     "        state = ev.OK"),

    # --- the arithmetic ----------------------------------------------------
    ("a closed work order creates demand", SH,
     "            .filter(models.WorkOrder.tenant_code == tenant, work_order_status.open_clause())",
     "            .filter(models.WorkOrder.tenant_code == tenant)"),
    ("an order already part-made is counted in full", SH,
     "        outstanding = max((w.target_quantity or 0) - (w.actual_quantity or 0), 0)",
     "        outstanding = w.target_quantity or 0"),
    ("partial stock makes fractional units", SH,
     "        can_make = min(outstanding, int(left // per_unit)) if per_unit > 0 else outstanding",
     "        can_make = min(outstanding, left / per_unit) if per_unit > 0 else outstanding"),
    ("the stock is never actually spent, so every order looks covered", SH,
     "        left = max(0.0, left - can_make * per_unit)", "        left = left"),
    ("the allocation stops being by due date", SH,
     "    live.sort(key=lambda p: (p[0].planned_end is None, p[0].planned_end or datetime.max, p[0].id))",
     "    live.sort(key=lambda p: -p[0].id)"),
    ("an order with no due date claims stock first", SH,
     "    live.sort(key=lambda p: (p[0].planned_end is None, p[0].planned_end or datetime.max, p[0].id))",
     "    live.sort(key=lambda p: (p[0].planned_end is not None, p[0].planned_end or datetime.min, p[0].id))"),

    # --- the recipe is the tenant's own -------------------------------------
    ("a component with no quantity per unit is counted as one each", SH,
     "            if qty_per_unit > 0:", "            if True:"),

    # --- money --------------------------------------------------------------
    ("money appears without a configured unit value", SH,
     "    if unit_value is not None and at_risk:", "    if at_risk:"),

    # --- the radar ----------------------------------------------------------
    ("the radar sizes a stock-out that has no recipe", RADAR,
     '            units=impact.get(item["item_code"]), unit_value=unit_value))',
     '            units=impact.get(item["item_code"], 1), unit_value=unit_value))'),

    # --- the tool -----------------------------------------------------------
    ("the tool hides how many items it could not size", TOOLS,
     '        _fact("shortage.items_unlinked", "Short items with no recipe linking them to production",\n'
     '              len(s["unlinked"]), M, "items", "bills_of_materials", "now",',
     '        _fact("shortage.items_unlinked", "Short items with no recipe linking them to production",\n'
     '              0, M, "items", "bills_of_materials", "now",'),
    ("the tool claims a money figure with no unit value", TOOLS,
     '        facts.append(_fact("shortage.money_at_risk", "Value of the units at risk", None, U, CURRENCY,',
     '        facts.append(_fact("shortage.money_at_risk", "Value of the units at risk", 0, D, CURRENCY,'),

    # --- an empty workspace is not a healthy one ----------------------------
    # An empty at-risk list means two different things, and only one of them is
    # "nothing is low". Each of these collapses the distinction again.
    ("a workspace with NO stock is reported as OK", SH,
     "        if not inv[\"total_items\"]:", "        if False:"),
    ("an unlooked-at workspace is given a zero rather than nothing", SH,
     '                    "shortages": [], "unlinked": [], "units_at_risk": None, "money_at_risk": None,\n'
     '                    "currency": None, "priced": bool(unit_value is not None),\n'
     '                    "note": ALLOCATION_RULE}',
     '                    "shortages": [], "unlinked": [], "units_at_risk": 0, "money_at_risk": None,\n'
     '                    "currency": None, "priced": bool(unit_value is not None),\n'
     '                    "note": ALLOCATION_RULE}'),
    ("the empty workspace stops denying that stock is healthy", SH,
     '                                 "would stop. This is not a report that stock is healthy."),',
     '                                 "would stop."),'),
    ("the empty and the stocked workspace get the same sentence", SH,
     '                    "headline": ("No stock items are set up, so AMP cannot say what a shortage "',
     '                    "headline": ("Nothing is at or below its reorder level. "'),
    # --- one instant for every rule ------------------------------------------
    ("the radar's delivery outlook is judged at the wall clock, not the instant named", RADAR,
     "    delivery = build_delivery_summary(db, tenant, now=now)\n",
     "    delivery = build_delivery_summary(db, tenant)\n"),
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
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<62} {verdict:<10} {note}")
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
