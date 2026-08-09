"""Mutation harness for the per-tenant bill of materials (ADR-0013).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_bom.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_per_tenant_bom.py", "test_event_bus.py",
          "test_work_orders_routes.py", "test_unscoped_model_reads.py"]

MUTATIONS = [
    # --- resolution -------------------------------------------------------
    ("the recipe is resolved without a tenant filter", "bom.py",
     "        .filter(models.BillOfMaterials.tenant_code == tenant,\n"
     "                models.BillOfMaterials.part_number == part_number,",
     "        .filter(models.BillOfMaterials.part_number == part_number,"),
    ("inactive revisions are used", "bom.py",
     "                models.BillOfMaterials.active.is_(True))\n", "                )\n"),
    ("a future-dated revision is used before its date", "bom.py",
     "    usable = [b for b in candidates\n"
     "              if b.effective_from is None or b.effective_from <= today]",
     "    usable = list(candidates)"),
    ("the OLDEST revision wins instead of the latest", "bom.py",
     "    return usable[-1]", "    return usable[0]"),

    # --- the subscriber ---------------------------------------------------
    ("only the FIRST component is consumed", "subscribers.py",
     "    for component_code, per_unit, _unit in bom.components_of(recipe):",
     "    for component_code, per_unit, _unit in bom.components_of(recipe)[:1]:"),
    ("the component lookup is not scoped by tenant", "subscribers.py",
     "        raw_item = db.query(models.InventoryItem).filter(\n"
     "            models.InventoryItem.tenant_code == event.tenant_code,\n"
     "            models.InventoryItem.item_code == component_code,\n"
     "        ).first()",
     "        raw_item = db.query(models.InventoryItem).filter(\n"
     "            models.InventoryItem.item_code == component_code,\n"
     "        ).first()"),
    ("the per-unit rate is ignored (1 per unit for everyone)", "subscribers.py",
     "        consume = min(qty * per_unit, current_stock)",
     "        consume = min(qty, current_stock)"),
    ("the finished good is not received", "subscribers.py",
     "    if recipe.output_item_code:", "    if False:"),
    ("a zero/negative line is written as a ledger row anyway", "subscribers.py",
     "        if not component_code or (per_unit or 0) <= 0:",
     "        if False:"),

    # --- the API ----------------------------------------------------------
    ("a negative component quantity is accepted", "bom_routes.py",
     "    if q <= 0:", "    if False:"),
    ("a duplicated component line is accepted", "bom_routes.py",
     "        if code in seen:\n"
     "            raise HTTPException(\n"
     "                status_code=400,\n"
     '                detail=f"{where}: {code} is listed twice; combine the quantities")',
     "        if False:\n"
     "            raise HTTPException(\n"
     "                status_code=400,\n"
     '                detail=f"{where}: {code} is listed twice; combine the quantities")'),
    # NOTE — the next two SURVIVE, and that is the correct outcome rather than a
    # gap. bom_routes' handlers are HTTP routes, and main.py installs the
    # ADR-0002 scoping hook unconditionally at import, so `filter(id == bom_id)`
    # alone still returns None for another tenant's row and the handler still
    # 404s. The explicit tenant filter is defence in depth.
    #
    # This is NOT the same situation as bom.resolve, whose own tenant filter IS
    # load-bearing and IS caught above: resolve runs from an event subscriber
    # that a script or a background job can drive with no tenant bound, which is
    # the shape of the ingest defect in ADR-0011. The distinction is
    # reachability — there is no entry point that serves these HTTP routes
    # without the hook, so a test for that state would be testing nothing.
    #
    # Kept in the matrix, failing, so the reasoning is recorded where the next
    # person will look rather than rediscovered.
    ("update_bom drops its tenant filter", "bom_routes.py",
     "           .filter(models.BillOfMaterials.id == bom_id,\n"
     "                   models.BillOfMaterials.tenant_code == tenant)\n"
     "           .first())\n"
     "    if bom is None:\n"
     '        raise HTTPException(status_code=404, detail="Bill of materials not found")\n'
     "\n"
     '    if "active" in payload:',
     "           .filter(models.BillOfMaterials.id == bom_id)\n"
     "           .first())\n"
     "    if bom is None:\n"
     '        raise HTTPException(status_code=404, detail="Bill of materials not found")\n'
     "\n"
     '    if "active" in payload:'),
    ("list_bom drops its tenant filter", "bom_routes.py",
     "    boms = (db.query(models.BillOfMaterials)\n"
     "            .filter(models.BillOfMaterials.tenant_code == tenant)",
     "    boms = (db.query(models.BillOfMaterials)"),
    ("writes are opened up beyond Admin", "bom_routes.py",
     '@router.post("/bom")\ndef create_bom(payload: dict, db: Session = Depends(_get_db),\n'
     '               current_user: dict = Depends(require_roles(["Admin"]))):',
     '@router.post("/bom")\ndef create_bom(payload: dict, db: Session = Depends(_get_db),\n'
     '               current_user: dict = Depends(require_roles(["Admin", "Operator"]))):'),

    # --- scoping ----------------------------------------------------------
    ("the BOM tables leave SCOPED_MODELS", "tenancy.py",
     "    models.BillOfMaterials, models.BomComponent,\n", ""),
]


# Mutations that survive for a stated, verified reason. Listing one here is a
# claim that has to be defended in the comment beside the mutation itself — not
# a way to silence a red line.
EXPECTED_SURVIVORS = {
    "update_bom drops its tenant filter":
        "the ADR-0002 hook still 404s it; no unhooked entry point exists",
    "list_bom drops its tenant filter":
        "the ADR-0002 hook still filters it; no unhooked entry point exists",
}


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace",
                              cwd=os.path.dirname(os.path.abspath(__file__)))
        if proc.returncode != 0:
            failed.append(suite)
    return failed


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
    print("-" * 102)

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
            # Documented above: shadowed by the always-installed ADR-0002 hook,
            # with no reachable entry point that runs without it.
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
