"""Mutation harness for the OEM sharing policy and fleet API (ADR-0017).

The rule under test is the one the whole expansion rests on: an OEM relationship
is NOT consent. Two independent things must both hold — the OEM installed the
machine, and the factory granted that class of data — and every mutation here
tries to collapse them into one.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_sharing.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_sharing.py", "test_oem_routes.py", "test_oem_authorization.py",
          "test_oem_foundation.py"]

MUTATIONS = [
    # --- default deny --------------------------------------------------------
    ("no policy row means EVERYTHING is shared", "oem_sharing.py",
     "    if not raw:\n        return set()",
     "    if not raw:\n        return set(ALL_GRANTS)"),
    ("an unknown grant token is honoured", "oem_sharing.py",
     '    return {g.strip() for g in str(raw).split(",")\n'
     "            if g.strip() in ALL_GRANTS}",
     '    return {g.strip() for g in str(raw).split(",") if g.strip()}'),
    ("a missing tenant resolves to a shared policy", "oem_sharing.py",
     "    if not oem_code or not tenant_code:\n        return set()",
     "    if not oem_code or not tenant_code:\n        return set(ALL_GRANTS)"),

    # --- the grant is not read per (oem, factory) ---------------------------
    ("the policy is looked up by factory only, ignoring the OEM",
     "oem_sharing.py",
     "             .filter(models.OemDataSharingPolicy.oem_code == oem_code,\n"
     "                     models.OemDataSharingPolicy.tenant_code == tenant_code)",
     "             .filter(models.OemDataSharingPolicy.tenant_code == tenant_code)"),
    ("the policy is looked up by OEM only, ignoring the factory",
     "oem_sharing.py",
     "             .filter(models.OemDataSharingPolicy.oem_code == oem_code,\n"
     "                     models.OemDataSharingPolicy.tenant_code == tenant_code)",
     "             .filter(models.OemDataSharingPolicy.oem_code == oem_code)"),

    # --- the fleet is not filtered by manufacturer --------------------------
    ("the fleet query drops the oem_code filter", "oem_sharing.py",
     "    q = (db.query(models.MachineInstallation)\n"
     "           .filter(models.MachineInstallation.oem_code == oem_code))",
     "    q = db.query(models.MachineInstallation)"),

    # --- fields leak without their grant ------------------------------------
    ("operating hours are returned without their grant", "oem_sharing.py",
     "    if SHARE_OPERATING_HOURS in grants:",
     "    if True:"),
    ("machine health is returned without its grant", "oem_sharing.py",
     "    if SHARE_MACHINE_HEALTH not in grants:\n        return None",
     "    if False:\n        return None"),
    ("the machine row is trusted without checking its own tenant",
     "oem_sharing.py",
     '    if (machine.tenant_code or "") != installation.factory_tenant_code:\n'
     "        return None",
     "    if False:\n        return None"),

    # --- the API forgets the principal --------------------------------------
    ("the fleet route takes the OEM from a query parameter", "oem_routes.py",
     '    rows = oem_sharing.installations_for(db, principal["oem"], tenant_code=customer)',
     "    rows = oem_sharing.installations_for(db, customer or principal[\"oem\"])"),
    ("a competitor's machine is a 403 (an existence oracle)", "oem_routes.py",
     '        raise HTTPException(status_code=404, detail="Machine not found")',
     '        raise HTTPException(status_code=403, detail="Not your machine")'),
    ("the model catalogue is not filtered by manufacturer", "oem_routes.py",
     "    rows = (db.query(models.MachineModel)\n"
     "              .filter(models.MachineModel.oem_code == principal[\"oem\"])\n"
     "              .order_by(models.MachineModel.model_code.asc()).all())",
     "    rows = (db.query(models.MachineModel)\n"
     "              .order_by(models.MachineModel.model_code.asc()).all())"),

    # --- the factory-route rejection ----------------------------------------
    ("an OEM token is accepted on factory routes", "auth.py",
     '    if claims.get("principal") == "oem":\n'
     "        raise HTTPException(\n"
     "            status_code=403,\n"
     '            detail="This is an OEM session; use the /oem portal")',
     "    if False:\n"
     "        raise HTTPException(\n"
     "            status_code=403,\n"
     '            detail="This is an OEM session; use the /oem portal")'),
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


# A mutation here is one a DIFFERENT guard already covers. Each needs a reason
# that survives reading.
EXPECTED_SURVIVORS = {
    "the machine row is trusted without checking its own tenant":
        "shadowed by the ADR-0002 hook. visible_machine binds the "
        "installation's factory tenant before the query, so a machine_id "
        "pointing at ANOTHER factory is already filtered out by the hook and "
        "the re-check never fires. It is kept as defence in depth for the one "
        "case the hook cannot cover: if Machine ever left SCOPED_MODELS, or if "
        "this function stopped binding a tenant, the hook would go quiet and "
        "this comparison would become the only thing standing between an "
        "inconsistent row and another factory's machine.",
    "an OEM token is accepted on factory routes":
        "shadowed by the sentinel, which is the POINT: with the rejection "
        "removed a factory route still returns an empty result, because the "
        "OEM's bound tenant matches no factory row. The rejection exists for "
        "the reads the ORM hook cannot see (raw SQL, unscoped models), which no "
        "current handler performs — so today it is defence in depth, and the "
        "test asserting the 403 pins the message rather than the isolation.",
}


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
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:20] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
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
