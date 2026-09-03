"""Mutation harness for the factory-controlled machine claim (ADR-0019).

Every mutation below is a plausible refactor that quietly turns a bearer
credential into a permanent one, or hands a machine to whoever asks. None of them
looks like an attack in a diff — which is the point.

RUN IT ALONE. Like every mutate_* harness this one EDITS THE WORKING TREE and
restores it afterwards, so anything else reading those files meanwhile sees a
deliberately broken codebase.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_claim.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_machine_claim.py", "test_oem_routes.py",
          "test_connected_equipment.py", "test_oem_lifecycle_writes.py",
          # REQUIRED, not optional. Four of the guards below exist only for the
          # case where two transactions run at once, and SQLite serialises
          # writers — a race there proves nothing, because the second
          # transaction simply waits and then sees the first one's result. Those
          # four mutations SURVIVED every SQLite suite until this harness was
          # added to the list. It needs a PostgreSQL on 5432.
          "verify_pg_claim.py"]

MUTATIONS = [
    # --- the credential stops being one ------------------------------------
    ("expiry is never checked, so a code works forever", "oem_claims.py",
     "    return claim.expires_at is not None and claim.expires_at <= now",
     "    return False"),
    ("a spent code can be used again (the one-time check is dropped)",
     "oem_claims.py",
     "    if claim.status != PENDING or is_expired(claim, now):\n        return False",
     "    if False:\n        return False"),
    ("a revoked code still works", "oem_claims.py",
     '    if claim.status != PENDING or is_expired(claim, now):',
     '    if claim.status in ("Revoked",) and False:'),
    ("the raw code is stored beside its hash", "oem_claims.py",
     "        token_hash=hash_code(code), code_hint=code_hint(code),",
     "        token_hash=hash_code(code), code_hint=code,"),

    # --- the race is decided by Python instead of the database -------------
    ("accept becomes read-modify-write, so two factories can both win",
     "oem_claims.py",
     "    won = (db.query(models.MachineClaim)\n"
     "             .filter(models.MachineClaim.id == claim.id,\n"
     "                     models.MachineClaim.status == PENDING)",
     "    won = (db.query(models.MachineClaim)\n"
     "             .filter(models.MachineClaim.id == claim.id)"),
    ("the machine can be taken even if it already has a factory",
     "oem_claims.py",
     "              .filter(models.MachineInstallation.id == installation.id,\n"
     "                      models.MachineInstallation.factory_tenant_code.is_(None),\n"
     "                      not_terminal_clause())",
     "              .filter(models.MachineInstallation.id == installation.id)"),
    ("a lost race is reported as a win", "oem_claims.py",
     "    if won != 1:\n        return False", "    if False:\n        return False"),
    # BOTH LAYERS AT ONCE. Each conditional UPDATE alone is shadowed by the
    # other — remove the claim's `status='Pending'` predicate and the
    # installation's `IS NULL` still refuses the loser, and vice versa. That
    # layering is deliberate, but "each is covered by the other" is only a real
    # answer if removing BOTH is caught. This mutation is what proves it.
    ("BOTH conditional updates become unconditional (the layering removed)",
     "oem_claims.py",
     [("             .filter(models.MachineClaim.id == claim.id,\n"
       "                     models.MachineClaim.status == PENDING)",
       "             .filter(models.MachineClaim.id == claim.id)"),
      ("              .filter(models.MachineInstallation.id == installation.id,\n"
       "                      models.MachineInstallation.factory_tenant_code.is_(None),\n"
       "                      not_terminal_clause())",
       "              .filter(models.MachineInstallation.id == installation.id)")],
     None),

    # --- the machine is claimable by the wrong party -------------------------
    ("a machine that already belongs to a factory can be claimed again",
     "oem_claims.py",
     "    if installation.factory_tenant_code:\n        return False",
     "    if False:\n        return False"),
    ("an intended customer is ignored, so anyone may take a targeted claim",
     "oem_claims.py",
     "    if claim.intended_tenant_code and claim.intended_tenant_code != tenant_code:\n"
     "        return False",
     "    if False:\n        return False"),

    # --- the factory stops being the one who decides -------------------------
    ("any signed-in factory user can accept, not just an Admin",
     "connected_equipment_routes.py",
     "def accept_claim(code: str, payload: ClaimAcceptance,\n"
     "                 db: Session = Depends(_get_db),\n"
     "                 current_user: dict = Depends(require_roles([\"Admin\"]))):",
     "def accept_claim(code: str, payload: ClaimAcceptance,\n"
     "                 db: Session = Depends(_get_db),\n"
     "                 current_user: dict = Depends(get_current_user)):"),
    ("previewing a claim needs no authentication at all",
     "connected_equipment_routes.py",
     "def preview_claim(code: str, db: Session = Depends(_get_db),\n"
     "                  current_user: dict = Depends(require_roles([\"Admin\"]))):",
     "def preview_claim(code: str, db: Session = Depends(_get_db),\n"
     "                  current_user: dict = Depends(get_current_user)):"),
    # TWO edits: Pydantic drops unknown fields, so reading `payload.tenant` is a
    # no-op unless the MODEL declares it. The one-edit version survived for that
    # reason and not because the guard works.
    ("the machine is assigned to a tenant from the REQUEST rather than the token",
     "connected_equipment_routes.py",
     [("class ClaimAcceptance(BaseModel):",
       "class ClaimAcceptance(BaseModel):\n    tenant: str | None = None"),
      ("    tenant = request_tenant(current_user)\n"
       "    actor = current_user.get(\"sub\") or current_user.get(\"username\") or \"?\"\n"
       "\n"
       "    unknown = [g for g in payload.grants if g not in oem_sharing.ALL_GRANTS]",
       "    tenant = payload.tenant or request_tenant(current_user)\n"
       "    actor = current_user.get(\"sub\") or current_user.get(\"username\") or \"?\"\n"
       "\n"
       "    unknown = [g for g in payload.grants if g not in oem_sharing.ALL_GRANTS]")],
     None),

    # --- consent -------------------------------------------------------------
    ("accepting a machine OVERWRITES the standing agreement instead of widening it",
     "connected_equipment_routes.py",
     "    policy.grants = \",\".join(sorted(existing | set(payload.grants)))",
     "    policy.grants = \",\".join(sorted(set(payload.grants)))"),
    # Anchored to the ACCEPT path: the same two lines appear in update_sharing,
    # so the unanchored pattern matched twice and was skipped.
    ("an unknown grant is silently dropped rather than refused",
     "connected_equipment_routes.py",
     "    unknown = [g for g in payload.grants if g not in oem_sharing.ALL_GRANTS]\n"
     "    if unknown:\n"
     "        raise HTTPException(\n"
     "            status_code=400,\n"
     '            detail=f"Unknown sharing grants: {\', \'.join(sorted(unknown))}")\n'
     "\n"
     "    claim = oem_claims.find_by_code(db, code)",
     "    unknown = []\n"
     "    if unknown:\n"
     "        raise HTTPException(\n"
     "            status_code=400,\n"
     '            detail=f"Unknown sharing grants: {\', \'.join(sorted(unknown))}")\n'
     "\n"
     "    payload.grants = [g for g in payload.grants\n"
     "                      if g in oem_sharing.ALL_GRANTS]\n"
     "    claim = oem_claims.find_by_code(db, code)"),

    # --- the refusal becomes an oracle ---------------------------------------
    # Aimed at the PREVIEW path — the endpoint somebody probing codes actually
    # uses, because it has no side effects. The first version of this mutation
    # hit only preview and survived, because every refusal was asserted on POST.
    ("a refusal explains which reason applied (preview)",
     "connected_equipment_routes.py",
     "        raise HTTPException(status_code=404, detail=oem_claims.REFUSAL)\n"
     "    return _claim_preview(db, claim, inst, tenant)",
     "        raise HTTPException(\n"
     "            status_code=404,\n"
     "            detail=('No such claim' if claim is None else\n"
     "                    f'Claim is {claim.status}'))\n"
     "    return _claim_preview(db, claim, inst, tenant)"),
    ("a refusal explains which reason applied (accept)",
     "connected_equipment_routes.py",
     "        log_audit(db, actor, \"claim_rejected\", \"machine_claim\", None,\n"
     "                  f\"tenant={tenant} refused\")\n"
     "        db.commit()\n"
     "        raise HTTPException(status_code=404, detail=oem_claims.REFUSAL)",
     "        log_audit(db, actor, \"claim_rejected\", \"machine_claim\", None,\n"
     "                  f\"tenant={tenant} refused\")\n"
     "        db.commit()\n"
     "        raise HTTPException(\n"
     "            status_code=404,\n"
     "            detail=('No such claim' if claim is None else\n"
     "                    f'Claim is {claim.status}'))"),

    # --- transfer ------------------------------------------------------------
    # The trailing `before = inst.status` is load-bearing in this pattern, not
    # decoration: the link route added by ADR-0019 opens with a byte-identical
    # lookup, so without a distinguishing tail this matched twice and silently
    # became a SKIP — a mutation that tests nothing while still reading as one.
    ("a factory can release somebody ELSE's installation",
     "connected_equipment_routes.py",
     "    inst = (db.query(models.MachineInstallation)\n"
     "              .filter(models.MachineInstallation.id == installation_id,\n"
     "                      models.MachineInstallation.factory_tenant_code == tenant)\n"
     "              .first())\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Equipment not found\")\n"
     "\n"
     "    before = inst.status",
     "    inst = (db.query(models.MachineInstallation)\n"
     "              .filter(models.MachineInstallation.id == installation_id)\n"
     "              .first())\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Equipment not found\")\n"
     "\n"
     "    before = inst.status"),

    # --- registration --------------------------------------------------------
    ("an OEM can register a machine against a COMPETITOR's model", "oem_routes.py",
     "    model = (db.query(models.MachineModel)\n"
     "               .filter(models.MachineModel.oem_code == principal[\"oem\"],\n"
     "                       models.MachineModel.id == payload.model_id).first())",
     "    model = (db.query(models.MachineModel)\n"
     "               .filter(models.MachineModel.id == payload.model_id).first())"),
    ("one OEM can register the same serial twice", "oem_routes.py",
     "    if clash is not None:", "    if False:"),
    ("an OEM can issue a claim for a competitor's machine", "oem_routes.py",
     "    inst = oem_sharing.get_installation(db, principal[\"oem\"], installation_id)\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Machine not found\")\n"
     "\n"
     "    try:\n"
     "        claim, code = oem_claims.create(",
     "    inst = db.query(models.MachineInstallation).filter(\n"
     "        models.MachineInstallation.id == installation_id).first()\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Machine not found\")\n"
     "\n"
     "    try:\n"
     "        claim, code = oem_claims.create("),
    ("an OEM can revoke a COMPETITOR's claim", "oem_routes.py",
     "    claim = (db.query(models.MachineClaim)\n"
     "               .filter(models.MachineClaim.oem_code == principal[\"oem\"],\n"
     "                       models.MachineClaim.id == claim_id).first())",
     "    claim = (db.query(models.MachineClaim)\n"
     "               .filter(models.MachineClaim.id == claim_id).first())"),

    # --- linking a serial to a machine on the floor --------------------------
    ("a factory can point its equipment at ANOTHER factory's machine",
     "connected_equipment_routes.py",
     "        machine = (db.query(models.Machine)\n"
     "                     .filter(models.Machine.id == payload.machine_id,\n"
     "                             models.Machine.tenant_code == tenant)\n"
     "                     .first())",
     "        machine = (db.query(models.Machine)\n"
     "                     .filter(models.Machine.id == payload.machine_id)\n"
     "                     .first())"),
    ("the refusal names WHY, turning 404 into an existence oracle",
     "connected_equipment_routes.py",
     "        machine = (db.query(models.Machine)\n"
     "                     .filter(models.Machine.id == payload.machine_id,\n"
     "                             models.Machine.tenant_code == tenant)\n"
     "                     .first())\n"
     "        if machine is None:\n"
     "            raise HTTPException(status_code=404, detail=\"Machine not found\")",
     "        machine = (db.query(models.Machine)\n"
     "                     .filter(models.Machine.id == payload.machine_id)\n"
     "                     .first())\n"
     "        if machine is None:\n"
     "            raise HTTPException(status_code=404, detail=\"Machine not found\")\n"
     "        if machine.tenant_code != tenant:\n"
     "            raise HTTPException(status_code=403,\n"
     "                                detail=\"That machine is another workspace's\")"),
    ("a factory can link equipment that is not its own",
     "connected_equipment_routes.py",
     "    inst = (db.query(models.MachineInstallation)\n"
     "              .filter(models.MachineInstallation.id == installation_id,\n"
     "                      models.MachineInstallation.factory_tenant_code == tenant)\n"
     "              .first())\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Equipment not found\")\n"
     "\n"
     "    machine = None",
     "    inst = (db.query(models.MachineInstallation)\n"
     "              .filter(models.MachineInstallation.id == installation_id)\n"
     "              .first())\n"
     "    if inst is None:\n"
     "        raise HTTPException(status_code=404, detail=\"Equipment not found\")\n"
     "\n"
     "    machine = None"),
    ("two serials can name the same machine (both OEMs read one row)",
     "connected_equipment_routes.py",
     "        if taken is not None:", "        if False:"),
    ("re-linking the SAME serial collides with itself",
     "connected_equipment_routes.py",
     "                           models.MachineInstallation.id != inst.id)",
     "                           models.MachineInstallation.id >= 0)"),
    ("anyone signed in at the factory may link, not only an Admin",
     "connected_equipment_routes.py",
     "def link_installation(installation_id: int, payload: MachineLink,\n"
     "                      db: Session = Depends(_get_db),\n"
     "                      current_user: dict = Depends(require_roles([\"Admin\"]))):",
     "def link_installation(installation_id: int, payload: MachineLink,\n"
     "                      db: Session = Depends(_get_db),\n"
     "                      current_user: dict = Depends(get_current_user)):"),

    # --- notifications -------------------------------------------------------
    ("the manufacturer's notification lands in the FACTORY's tenant",
     "oem_subscribers.py",
     "        db, oem_auth.sentinel_tenant(event.oem_code), \"installation_accepted\",",
     "        db, event.tenant_code, \"installation_accepted\","),
]


def run_suites():
    here = os.path.dirname(os.path.abspath(__file__))
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


# A mutation another guard already covers. Each needs a reason that survives
# reading — "it seemed fine" is how a real gap gets filed as expected.
EXPECTED_SURVIVORS = {
    "accept becomes read-modify-write, so two factories can both win":
        "SHADOWED BY THE SECOND CONDITIONAL UPDATE, and verified rather than "
        "assumed: with the claim predicate gone both threads take the claim, but "
        "the installation UPDATE still carries `factory_tenant_code IS NULL`, so "
        "the loser matches zero rows, rolls back and returns False. The mutation "
        "'BOTH conditional updates become unconditional' removes the layering "
        "and IS caught -- which is what makes this a layer rather than a gap.",
    "the machine can be taken even if it already has a factory":
        "The mirror image of the above: with the installation predicate gone the "
        "claim's `status = Pending` predicate still lets exactly one thread "
        "through. Removing both is caught.",
    "a lost race is reported as a win":
        "A loser that skips the `won != 1` early return proceeds to the "
        "installation UPDATE, which matches zero rows, rolls back and returns "
        "False anyway. The early return is a clarity and cost optimisation on "
        "top of a guard that already holds.",
    "a machine that already belongs to a factory can be claimed again":
        "usable()'s check is the polite one -- it produces the refusal before any "
        "write is attempted. With it removed the accept still fails, because the "
        "installation UPDATE requires `factory_tenant_code IS NULL`, so the route "
        "returns the same 404. Asserted by the SN-TAKEN case in test_machine_claim.",
    "a factory can release somebody ELSE's installation":
        "shadowed by the conditional UPDATE. Removing the tenant filter from the "
        "LOOKUP lets a foreign row be found, but the release itself is "
        "`UPDATE ... WHERE id=? AND factory_tenant_code=?`, which then matches "
        "zero rows and the handler 404s anyway. Two independent filters, and the "
        "SECOND is the one that decides — deliberately, because it is also what "
        "makes a release safe against a concurrent transfer. The lookup filter is "
        "defence in depth, and a mutation of BOTH is caught by the same test.",
    "a factory can point its equipment at ANOTHER factory's machine":
        "SHADOWED BY ADR-0002 ITSELF. models.Machine is the first entry in "
        "tenancy.SCOPED_MODELS, so the do_orm_execute hook has already added "
        "`tenant_code = <bound tenant>` to this query before the route's own "
        "filter is considered; removing the route filter therefore changes no "
        "SQL that matters and the foreign machine stays invisible. The filter is "
        "kept because it states the intent where the decision is made and would "
        "be the only guard if this route were ever given an unscoped session — "
        "and the protection itself IS asserted, by the passing case 'a factory "
        "cannot point its equipment at another factory's machine'.",
    "the refusal names WHY, turning 404 into an existence oracle":
        "UNREACHABLE, for the same reason: the injected 403 branch can only fire "
        "for a machine the query returned, and ADR-0002 never returns another "
        "tenant's. That the oracle CANNOT be built here is the finding — the "
        "test that would catch it ('...INDISTINGUISHABLE from no such machine') "
        "passes because both paths 404, not because the branch was removed.",
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
    print(f"{'mutation':<70} {'verdict':<10} caught by")
    print("-" * 112)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        edits = old if isinstance(old, list) else [(old, new)]
        counts = [source.count(o) for o, _ in edits]
        if any(c != 1 for c in counts):
            print(f"{label:<70} {'SKIP':<10} pattern hits {counts} in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        mutated = source
        for o, n in edits:
            mutated = mutated.replace(o, n, 1)
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(mutated)
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:22] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:60] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<70} {verdict:<10} {note}")
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
