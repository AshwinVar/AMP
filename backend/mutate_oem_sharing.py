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
          "test_oem_foundation.py", "test_oem_service_consent.py",
          "test_oem_lifecycle_writes.py"]

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
    # Anchored on the line BELOW as well: `if SHARE_OPERATING_HOURS in grants:`
    # now appears in both fleet_row and service_view, and an ambiguous pattern
    # makes this harness print SKIP — which reads like a pass at a glance.
    ("operating hours are returned without their grant", "oem_sharing.py",
     "    if SHARE_OPERATING_HOURS in grants:\n"
     '        row["operating_hours"] = installation.operating_hours',
     "    if True:\n"
     '        row["operating_hours"] = installation.operating_hours'),
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
    # Anchored on the line BELOW. Six handlers raise this identical 404, so the
    # bare pattern was ambiguous and this mutation printed SKIP on every run
    # since it was written — never once executed, while reading like a pass in a
    # column of "caught". `machine_detail` is the one that matters: it is the
    # read a competitor would probe ids against.
    ("a competitor's machine is a 403 (an existence oracle)", "oem_routes.py",
     '        raise HTTPException(status_code=404, detail="Machine not found")\n'
     '    catalogue = _models_by_id(db, principal["oem"])',
     '        raise HTTPException(status_code=403, detail="Not your machine")\n'
     '    catalogue = _models_by_id(db, principal["oem"])'),
    ("the model catalogue is not filtered by manufacturer", "oem_routes.py",
     "    rows = (db.query(models.MachineModel)\n"
     "              .filter(models.MachineModel.oem_code == principal[\"oem\"])\n"
     "              .order_by(models.MachineModel.model_code.asc()).all())",
     "    rows = (db.query(models.MachineModel)\n"
     "              .order_by(models.MachineModel.model_code.asc()).all())"),

    # --- service intelligence leaks past the policy -------------------------
    #
    # The class of defect this whole block exists for: `/oem/fleet` withheld the
    # hour meter while `/oem/service` printed it in prose, for a manufacturer
    # whose customer had switched that grant off.
    ("the service verdict is disclosed without SHARE_SERVICE_STATUS",
     "oem_sharing.py",
     "    if SHARE_SERVICE_STATUS not in grants:", "    if False:"),
    ("the hour FIGURES ride along with the service verdict", "oem_sharing.py",
     "    if SHARE_OPERATING_HOURS in grants:\n"
     '        out["hours_remaining"] = state.get("hours_remaining")',
     "    if True:\n"
     '        out["hours_remaining"] = state.get("hours_remaining")'),
    # An OMITTED key is a disclosure too: service_state drops `interval_hours`
    # on the branch where the machine has never reported hours, so copying it
    # through carried one bit of the withheld meter.
    ("the service interval is copied from the arithmetic, not the model",
     "oem_sharing.py",
     '    interval = getattr(model, "service_interval_hours", None)',
     '    interval = state.get("interval_hours")'),
    ("the service reason keeps the numbers in its PROSE", "oem_sharing.py",
     '    out["reason"] = _service_reason_without_hours(state.get("state"), interval)',
     '    out["reason"] = state.get("reason")'),
    ("a service recommendation keeps its hour arithmetic", "oem_sharing.py",
     '        if (rec.get("kind") in ("service_due", "service_projection")\n'
     "                and SHARE_OPERATING_HOURS not in grants):",
     "        if False:"),
    ("a recommendation is shown without the grant it needs", "oem_sharing.py",
     "        if needed and needed not in grants:\n            continue",
     "        if False:\n            continue"),
    ("the not_reporting item escapes the connectivity grant", "oem_sharing.py",
     '    "not_reporting": SHARE_MACHINE_HEALTH,\n', ""),
    ("the commissioning report is returned ungated", "oem_sharing.py",
     "    if SHARE_MACHINE_HEALTH in grants:\n        return report",
     "    if True:\n        return report"),
    ("an unshared commissioning check reads as FAILED", "oem_sharing.py",
     '            c = {**c, "passed": None,', '            c = {**c, "passed": False,'),
    ("readiness stays a confident yes when a check is unshared",
     "oem_sharing.py",
     '    return {"ready": None, "checks": checks}',
     '    return {"ready": report.get("ready"), "checks": checks}'),
    # The bug a single-customer fixture cannot see: the FIRST customer's consent
    # applied to every customer in the loop.
    ("one customer's grants are reused for the whole fleet", "oem_sharing.py",
     "        tenant = inst.factory_tenant_code\n"
     "        if tenant not in cache:\n"
     "            cache[tenant] = grants_for(db, oem_code, tenant)",
     '        tenant = "every customer"\n'
     "        if tenant not in cache:\n"
     "            cache[tenant] = grants_for(db, oem_code,\n"
     "                                      installations[0].factory_tenant_code)"),

    # --- the routes stop asking ---------------------------------------------
    ("the queue is assembled without consulting consent", "oem_routes.py",
     '    recs = oem_sharing.fleet_recommendations(db, principal["oem"], rows, catalogue)',
     "    recs = oem_service.by_severity(\n"
     "        [r for i in rows\n"
     "         for r in oem_service.recommendations(i, catalogue.get(i.model_id))])"),
    ("the per-machine service view is returned ungated", "oem_routes.py",
     '        "service": oem_sharing.service_view(\n'
     "            oem_service.service_state(inst, model), grants, model),",
     '        "service": oem_service.service_state(inst, model),'),
    # The bisection. Withholding the FIGURES does nothing about it, because the
    # leak is the comparator: the caller supplies the number the verdict is
    # computed against, and reads back which side of the threshold it landed on.
    ("the caller may choose the figure the verdict is measured against",
     "oem_routes.py",
     "    if supplied and oem_sharing.SHARE_OPERATING_HOURS not in grants:",
     "    if False:"),
    ("the empty-body service WRITE hands back the hour meter", "oem_routes.py",
     "    disclosable = (inst.last_service_hours\n"
     "                   if supplied or oem_sharing.SHARE_OPERATING_HOURS in grants\n"
     "                   else None)",
     "    disclosable = inst.last_service_hours"),

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
