"""Mutation harness for the service-contract routes and their domain service (ADR-0020).

Each mutation is a plausible edit that would let a stranger see a contract, let
a manufacturer read a factory's downtime after consent was withdrawn, bind a
party to terms or a statement it never saw, let one party change what both
agreed, or let a transition escape its audit. Every one must turn a suite red,
or be listed in EXPECTED_SURVIVORS with the reason another guard shadows it.

HOW IT RUNS
-----------
It never edits the working tree. The backend is copied to a temporary
directory, each mutation is applied to the COPY, and the seven route suites run
there IN PARALLEL (each with its own SQLite file and CONTRACT_FAIL_FAST=1, so
the first red check ends a suite). The copy is restored byte for byte after each
mutation and deleted at the end. Another builder running tests in the real tree
meanwhile sees nothing.

A mutation whose pattern does not match exactly once is reported SKIP and
counts as a failure of the harness: a guard that cannot find its target proves
nothing.

Run: python backend/mutate_service_contracts.py
     python backend/mutate_service_contracts.py "consent"     (labels containing)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = ["test_contract_route_guards.py", "test_contract_lifecycle.py",
          "test_contract_acceptance.py", "test_contract_amendments.py",
          "test_contract_disputes.py", "test_contract_consent.py",
          "test_contract_tenancy.py"]

SVC = "service_contracts.py"
OEM = "oem_contract_routes.py"
FAC = "service_contract_routes.py"
SCHEMAS = "schemas_contracts.py"


def _one(path, old, new):
    return [(path, old, new)]


MUTATIONS = [
    # --- who sees a contract ---------------------------------------------------
    ("an OEM sees every manufacturer's contracts", _one(SVC,
        "        return q.filter(models.ServiceContract.oem_code == party.oem_code)",
        "        return q")),
    ("a factory sees contracts addressed to other factories", _one(SVC,
        "    return q.filter(models.ServiceContract.factory_tenant_code == party.tenant_code,\n",
        "    return q.filter(\n")),
    ("a factory sees drafts that were never proposed", _one(SVC,
        "                    models.ServiceContract.proposed_at.isnot(None),\n"
        "                    models.ServiceContract.status != DRAFT)",
        "                    )")),
    ("a founder switcher aimed at an OEM sentinel acts as a factory", _one(SVC,
        "    if not tenant or oem_auth.is_sentinel(tenant):",
        "    if not tenant:")),
    ("the other party sees an unproposed draft version", _one(SVC,
        "    return version.proposed_at is not None or version.proposed_by_party == party.side",
        "    return True")),

    # --- consent ---------------------------------------------------------------
    ("statement content and actions ignore SHARE_DOWNTIME", _one(SVC,
        "    if party.side == OEM and not oem_sharing.contract_statement_visible(db, contract):\n"
        "        raise Refused(403, WITHHELD)",
        "    if False:\n        raise Refused(403, WITHHELD)")),
    ("a withdrawn dispute's reply shows the OEM the statement hash without consent",
     _one(SVC, "    if (ref is not None and party.side == OEM",
          "    if (False and ref is not None and party.side == OEM")),

    # --- the factory's explicit acceptance --------------------------------------
    ("a contract is accepted without grant_downtime_sharing", _one(SVC,
        "    if grant is not True:\n        raise Refused(422, GRANT_REQUIRED)",
        "    if False:\n        raise Refused(422, GRANT_REQUIRED)")),
    ("any truthy value counts as the grant", [
        (SCHEMAS, "    grant_downtime_sharing: Optional[StrictBool] = None",
         "    grant_downtime_sharing: Optional[object] = None"),
        (SVC, "    if grant is not True:", "    if not grant:")]),
    ("the factory accepts terms whose hash it did not send", [
        (SVC, "    if v1.status != PROPOSED or v1.terms_hash != terms_hash \\\n"
              "            or v1.oem_accepted_hash != terms_hash:",
         "    if v1.status != PROPOSED:"),
        (SVC, "                   models.ServiceContractTermVersion.status == PROPOSED,\n"
              "                   models.ServiceContractTermVersion.terms_hash == terms_hash,\n"
              "                   models.ServiceContractTermVersion.oem_accepted_hash == terms_hash)\n"
              "            .values(status=ACCEPTED, factory_accepted_by=party.actor,",
         "                   models.ServiceContractTermVersion.status == PROPOSED)\n"
         "            .values(status=ACCEPTED, factory_accepted_by=party.actor,")]),
    ("accepting a contract does not grant SHARE_DOWNTIME", _one(SVC,
        "    oem_sharing.widen_grants(db, contract.oem_code, contract.factory_tenant_code,",
        "    (lambda *a, **k: None)(db, contract.oem_code, contract.factory_tenant_code,")),
    ("the accept UPDATE forgets the status it read (accept can beat withdraw)", _one(SVC,
        "        .where(models.ServiceContract.id == contract.id,\n"
        "               models.ServiceContract.status == PROPOSED,\n"
        "               models.ServiceContract.factory_tenant_code == party.tenant_code)",
        "        .where(models.ServiceContract.id == contract.id,\n"
        "               models.ServiceContract.factory_tenant_code == party.tenant_code)")),

    # --- coverage --------------------------------------------------------------
    ("one machine may be covered by two contracts at once", _one(SVC,
        "    clash = _covered_elsewhere(db, contract, ids, start, end)",
        "    clash = set()")),
    ("a machine not linked to the shop floor can be covered", _one(SVC,
        "            if machine is None:\n                problems.append(",
        "            if False:\n                problems.append(")),
    ("the coverage check reads machines under the request's own binding", _one(SVC,
        "    with oem_sharing.bound_factory_read(contract.factory_tenant_code):\n"
        "        for c in terms.covered_installations:",
        "    with open(__file__):\n        for c in terms.covered_installations:")),
    ("the machine lookup drops its explicit factory filter", _one(SVC,
        "                             .filter(models.Machine.id == r.machine_id,\n"
        "                                     models.Machine.tenant_code\n"
        "                                     == contract.factory_tenant_code).first())",
        "                             .filter(models.Machine.id == r.machine_id).first())")),
    ("an out-of-range id reaches the database", _one(SVC,
        "    return type(value) is int and 1 <= value <= MAX_ROW_ID",
        "    return type(value) is int")),
    ("a competitor's or another factory's installation can be named", _one(SVC,
        "        if not rows or rows[0].serial_number != c.serial_number:",
        "        if False:")),
    ("an installation id may be paired with another serial", _one(SVC,
        "        if not rows or rows[0].serial_number != c.serial_number:",
        "        if not rows:")),
    ("a contract may be addressed to the OEM sentinel", _one(SVC,
        "    if not tenant or tenancy.is_reserved_tenant_code(tenant):",
        "    if not tenant:")),

    # --- propose, withdraw, reject, terminate -----------------------------------
    ("the OEM proposes terms whose hash it did not send", [
        (SVC, "    if v1.terms_hash != terms_hash:\n"
              "        raise Refused(409, {\"message\": \"These are not the terms you are proposing; \"",
         "    if False:\n"
         "        raise Refused(409, {\"message\": \"These are not the terms you are proposing; \""),
        (SVC, "               models.ServiceContractTermVersion.status == DRAFT,\n"
              "               models.ServiceContractTermVersion.terms_hash == terms_hash)\n"
              "        .values(status=PROPOSED, proposed_at=now, proposed_by=party.actor,\n"
              "                oem_accepted_by=party.actor,",
         "               models.ServiceContractTermVersion.status == DRAFT)\n"
         "        .values(status=PROPOSED, proposed_at=now, proposed_by=party.actor,\n"
         "                oem_accepted_by=party.actor,")]),
    ("an accepted contract can be withdrawn by the OEM", _one(SVC,
        "    if before not in (DRAFT, PROPOSED):",
        "    if before not in (DRAFT, PROPOSED, ACCEPTED):")),
    ("a draft's withdrawal is audited to the factory that never saw it", _one(SVC,
        "           sides=(OEM,) if before == DRAFT else SIDES)",
        "           sides=SIDES)")),
    ("drafting is audited to the factory that never saw it", _one(SVC,
        "           f\"ref={ref} factory={tenant} version=1 terms_hash={version.terms_hash}\",\n"
        "           sides=(OEM,))",
        "           f\"ref={ref} factory={tenant} version=1 terms_hash={version.terms_hash}\",\n"
        "           sides=SIDES)")),
    ("termination takes effect mid-period, not on a boundary", _one(SVC,
        "    effective = contract_periods.boundary_at_or_after(\n"
        "        grid, contract.starts_at, now + timedelta(days=notice_days))",
        "    effective = now + timedelta(days=notice_days)")),
    ("termination ignores the notice period", _one(SVC,
        "        grid, contract.starts_at, now + timedelta(days=notice_days))",
        "        grid, contract.starts_at, now)")),
    ("a termination at or past the contract's end is recorded", _one(SVC,
        "    if effective >= contract.ends_at:", "    if False:")),

    # --- amendments ------------------------------------------------------------
    ("two amendments may be pending at once", _one(SVC,
        "    if any(v.status in (DRAFT, PROPOSED) for v in versions):", "    if False:")),
    ("the party that proposed an amendment accepts it too", _one(SVC,
        "    if version.proposed_by_party == party.side:\n"
        "        raise Refused(403, \"The party that proposed an amendment cannot also accept it\")",
        "    if False:\n"
        "        raise Refused(403, \"The party that proposed an amendment cannot also accept it\")")),
    ("an amendment reaches back over an agreed statement", _one(SVC,
        "    if effective_from < floor:", "    if False:")),
    ("an amendment changes the period grid", _one(SVC,
        "        if getattr(terms, f) != getattr(grid, f):", "        if False:")),
    ("an amendment takes effect inside a period", _one(SVC,
        "    if not contract_periods.is_boundary(grid, contract.starts_at, effective_from) \\\n"
        "            or effective_from >= end:",
        "    if effective_from >= end:")),
    ("a machine added by amendment skips the coverage checks", _one(SVC,
        "    installations = _lock_and_check_coverage(\n"
        "        db, contract, terms, version.effective_from,\n"
        "        contract_periods.contract_effective_end(contract))",
        "    installations = {r.id: r for r in db.query(models.MachineInstallation).filter(\n"
        "        models.MachineInstallation.id.in_(\n"
        "            [c.installation_id for c in terms.covered_installations])).all()}")),

    # --- statements ------------------------------------------------------------
    ("acceptance does not recompute first (stale evidence is accepted)", _one(SVC,
        "    result = _run_compute(db, party, contract, st.period_start, now, allow_expired=True)\n"
        "    if result is not None:\n        st = result.statement",
        "    result = None")),
    ("past retention, a statement can never be accepted (C11)", _one(SVC,
        "    result = _run_compute(db, party, contract, st.period_start, now, allow_expired=True)\n"
        "    if result is not None:\n        st = result.statement",
        "    result = _run_compute(db, party, contract, st.period_start, now)\n"
        "    if result is not None:\n        st = result.statement")),
    ("an acceptance binds the hash but not the revision (C1)", [
        (SVC, "    if content_hash != st.content_hash or revision != st.revision:",
         "    if content_hash != st.content_hash:"),
        (SVC, "               models.ContractStatement.content_hash == content_hash,\n"
              "               models.ContractStatement.revision == revision)",
         "               models.ContractStatement.content_hash == content_hash)")]),
    ("an acceptance binds the revision but not the hash", [
        (SVC, "    if content_hash != st.content_hash or revision != st.revision:",
         "    if revision != st.revision:"),
        (SVC, "               models.ContractStatement.content_hash == content_hash,\n"
              "               models.ContractStatement.revision == revision)",
         "               models.ContractStatement.revision == revision)")]),
    ("open disputes do not block acceptance", _one(SVC,
        "    if live:\n        db.commit()", "    if False:\n        db.commit()")),
    ("a pending_disputes statement can be accepted (C12)", _one(SVC,
        "    if sla_state == \"pending_disputes\":", "    if False:")),
    ("the acceptance is recorded for the other party", _one(SVC,
        "        statement_id=st.id, party=party.side, actor=party.actor, accepted_at=now,",
        "        statement_id=st.id, party=other_side(party.side), actor=party.actor,"
        " accepted_at=now,")),
    ("an OEM-triggered compute reads under the OEM sentinel", _one(SVC,
        "        with oem_sharing.bound_factory_read(contract.factory_tenant_code):\n"
        "            result = engine.compute_statement(",
        "        with oem_sharing.bound_factory_read(tenancy.current_tenant()):\n"
        "            result = engine.compute_statement(")),
    ("the engine audits a compute under a bare username, not the party label", _one(SVC,
        "                                              party=party.for_engine(), now=now)",
        "                                              party=party, now=now)")),
    ("compute accepts an instant that starts no period", _one(SVC,
        "    if period is None:\n        raise Refused(422, {\"field\": \"period_start\",",
        "    if False:\n        raise Refused(422, {\"field\": \"period_start\",")),
    ("a statement id is not tied to its contract", _one(SVC,
        "            .filter(models.ContractStatement.id == statement_id,\n"
        "                    models.ContractStatement.contract_id == contract.id).first())",
        "            .filter(models.ContractStatement.id == statement_id).first())")),
    ("the download re-serialises instead of serving the stored bytes", _one(SVC,
        "    return st, st.canonical_json.encode(\"utf-8\")",
        "    return st, json.dumps(json.loads(st.canonical_json)).encode(\"utf-8\")")),

    # --- disputes --------------------------------------------------------------
    ("overlapping disputes on one machine are allowed", _one(SVC,
        "    if overlap is not None:", "    if False:")),
    ("a dispute window may lie outside the covered hours", _one(SVC,
        "    if not any(s <= window_start and window_end <= e for s, e in intervals):",
        "    if False:")),
    ("DISPUTED is accepted as a dispute or resolution bucket", _one(SVC,
        "    if value not in contract_terms.RESOLUTION_BUCKETS:",
        "    if value not in contract_terms.BUCKETS:")),
    ("the resolution's proposer accepts it too", _one(SVC,
        "    if dispute.resolution_proposed_by_party == party.side:", "    if False:")),
    ("a resolution is accepted without naming the bucket on offer", _one(SVC,
        "    if body.resolution_bucket != dispute.resolution_bucket:", "    if False:")),
    ("either party withdraws a dispute", _one(SVC,
        "    if dispute.raised_by_party != party.side:", "    if False:")),
    ("an agreed statement can be disputed", [
        (SVC, "    if engine.acceptance_state(db, st)[\"agreed\"]:\n"
              "        raise Refused(409, \"An agreed statement is final and cannot be disputed\")",
         "    if False:\n"
         "        raise Refused(409, \"An agreed statement is final and cannot be disputed\")"),
        (SVC, "    if not result.changed:\n        # Raising a dispute must move",
         "    if False:\n        # Raising a dispute must move")]),
    ("a dispute transition leaves the loaded row stale for the recompute", _one(SVC,
        "    db.expire(dispute)\n", "    pass\n")),
    ("an installation the terms do not cover can be disputed", _one(SVC,
        "    if covered is None:", "    if False:")),
    ("a dispute id is not tied to its contract", _one(SVC,
        "           .filter(models.ContractDispute.id == dispute_id,\n"
        "                   models.ContractDispute.contract_id == contract.id).first())",
        "           .filter(models.ContractDispute.id == dispute_id).first())")),

    # --- history and the reason vocabulary ---------------------------------------
    ("history reaches back before the contract existed", _one(SVC,
        "                      models.AuditLog.created_at >= contract.created_at,\n", "")),
    ("history includes other entities with the same id", _one(SVC,
        "                      models.AuditLog.entity_type.in_(HISTORY_ENTITY_TYPES),\n"
        "                      or_(*entity),",
        "                      models.AuditLog.entity_id == contract.id,")),
    ("the reason vocabulary has no lower time bound", _one(SVC,
        "                          models.DowntimeLog.created_at >= since,\n", "")),
    ("the reason vocabulary has no upper time bound", _one(SVC,
        "                          models.DowntimeLog.created_at < until).all())",
        "                          ).all())")),
    ("the reason vocabulary reads machines the terms do not cover", _one(SVC,
        "        .filter(models.MachineInstallation.id.in_(ids),\n"
        "                models.MachineInstallation.oem_code == contract.oem_code,",
        "        .filter(models.MachineInstallation.oem_code == contract.oem_code,")),

    # --- audit and events --------------------------------------------------------
    ("contract acceptance is audited for the factory only", _one(SVC,
        "           f\"installations={len(installations)} grant=SHARE_DOWNTIME\")",
        "           f\"installations={len(installations)} grant=SHARE_DOWNTIME\","
        " sides=(FACTORY,))")),
    ("the audit commits on its own, outside the business transaction", _one(SVC,
        "                                  entity_id, details, tenant_code=tenant,\n"
        "                                  commit=False)",
        "                                  entity_id, details, tenant_code=tenant,\n"
        "                                  commit=True)")),
    ("accepting a contract publishes no event", _one(SVC,
        "    _publish(db, oem_events.ContractAccepted(",
        "    (lambda *a: None)(db, oem_events.ContractAccepted(")),
    ("the ContractAccepted event is filed under the OEM sentinel", _one(SVC,
        "    _publish(db, oem_events.ContractAccepted(\n"
        "        tenant_code=contract.factory_tenant_code,",
        "    _publish(db, oem_events.ContractAccepted(\n"
        "        tenant_code=oem_auth.sentinel_tenant(contract.oem_code),")),

    # --- route gates -------------------------------------------------------------
    ("an OEM service manager may accept a statement", _one(OEM,
        "                     principal: dict = Depends(oem_auth.require_oem(SIGN))):\n"
        "    return svc.accept_statement(",
        "                     principal: dict = Depends(oem_auth.require_oem(MANAGE))):\n"
        "    return svc.accept_statement(")),
    ("an OEM service manager may propose a contract", _one(OEM,
        "            principal: dict = Depends(oem_auth.require_oem(SIGN))):\n"
        "    return svc.propose_contract(",
        "            principal: dict = Depends(oem_auth.require_oem(MANAGE))):\n"
        "    return svc.propose_contract(")),
    ("an OEM viewer may compute", _one(OEM,
        "            principal: dict = Depends(oem_auth.require_oem(MANAGE))):\n"
        "    return svc.compute(",
        "            principal: dict = Depends(oem_auth.require_oem(READ))):\n"
        "    return svc.compute(")),
    ("a factory Supervisor may accept a contract", _one(FAC,
        "           current_user: dict = Depends(require_roles(SIGNERS))):\n"
        "    return svc.accept_contract(",
        "           current_user: dict = Depends(require_roles(READERS))):\n"
        "    return svc.accept_contract(")),
    ("a factory Supervisor may raise a dispute", _one(FAC,
        "                  current_user: dict = Depends(require_roles(SIGNERS))):\n"
        "    return svc.raise_dispute(",
        "                  current_user: dict = Depends(require_roles(READERS))):\n"
        "    return svc.raise_dispute(")),
    ("a factory Operator may read contracts", _one(FAC,
        'READERS = ["Admin", "Supervisor"]', 'READERS = ["Admin", "Supervisor", "Operator"]')),
    ("a request body may carry fields it does not name (oem_code)", _one(SCHEMAS,
        '    model_config = ConfigDict(extra="forbid")', '    model_config = ConfigDict()')),
]

# Mutations another guard makes unobservable, each with the reason. A mutation
# listed here that IS caught is still reported as caught.
EXPECTED_SURVIVORS = {
    "the machine lookup drops its explicit factory filter":
        "SHADOWED BY THE ADR-0002 HOOK. The lookup runs inside "
        "bound_factory_read(contract.factory_tenant_code), so the ORM filter already "
        "limits Machine to that factory (the planted cross-factory link is still "
        "refused). Kept as the explicit, auditable statement of the rule.",
    "a contract may be addressed to the OEM sentinel":
        "SHADOWED BY THE OWNERSHIP CHECK. No installation can be at a sentinel "
        "tenant (claims refuse it), so installations_for finds nothing and the draft "
        "is refused 422 anyway. Kept: it names the rule instead of relying on data.",
}


def _apply(source_bytes, old, new):
    text = source_bytes.decode("utf-8")
    if "\r\n" in text:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = text.count(old)
    if count != 1:
        return None, count
    return text.replace(old, new, 1).encode("utf-8"), 1


def _copy_backend():
    root = tempfile.mkdtemp(prefix="amp_mutate_contracts_")
    dest = os.path.join(root, "backend")
    shutil.copytree(HERE, dest, ignore=shutil.ignore_patterns(
        "__pycache__", "*.db", ".pytest_cache", "*.log", "node_modules"))
    return root, dest


def _run_suite(copy, suite):
    name = suite.replace(".py", "")
    env = dict(os.environ, DATABASE_URL=f"sqlite:///./mut_{name}.db",
               PYTHONIOENCODING="utf-8", CONTRACT_FAIL_FAST="1")
    try:
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=copy, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return suite, False, "timeout"
    first = next((ln.strip() for ln in proc.stdout.splitlines()
                  if ln.startswith("FAIL-FAST:") or ln.strip().startswith("FAIL ")), "")
    if not first and proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["?"])[-1]
    return suite, proc.returncode == 0, first


def run_suites(copy):
    with ThreadPoolExecutor(max_workers=len(SUITES)) as pool:
        return list(pool.map(lambda s: _run_suite(copy, s), SUITES))


def main(argv):
    wanted = argv[1] if len(argv) > 1 else None
    chosen = [(label, edits) for label, edits in MUTATIONS
              if wanted is None or wanted in label]
    root, copy = _copy_backend()
    try:
        paths = sorted({p for _, edits in chosen for p, _, _ in edits})
        originals = {}
        for p in paths:
            with open(os.path.join(copy, p), "rb") as fh:
                originals[p] = fh.read()

        baseline = [r for r in run_suites(copy) if not r[1]]
        if baseline:
            print("ABORT: suites already failing before any mutation:")
            for suite, _, why in baseline:
                print(f"   {suite}: {why}")
            return 2
        print(f"baseline: all {len(SUITES)} suites green in a copy of backend/ ({copy})\n")
        print(f"{'mutation':<76} {'verdict':<9} caught by")
        print("-" * 124)

        problems = []
        for label, edits in chosen:
            mutated = dict(originals)
            skip = None
            for path, old, new in edits:
                result, count = _apply(mutated[path], old, new)
                if result is None:
                    skip = f"pattern hits {count} in {path}"
                    break
                mutated[path] = result
            if skip:
                print(f"{label:<76} {'SKIP':<9} {skip}")
                problems.append(f"{label} ({skip})")
                continue
            touched = {p for p, _, _ in edits}
            try:
                for p in touched:
                    with open(os.path.join(copy, p), "wb") as fh:
                        fh.write(mutated[p])
                results = run_suites(copy)
            finally:
                for p in touched:
                    with open(os.path.join(copy, p), "wb") as fh:
                        fh.write(originals[p])
            red = [(s, why) for s, ok, why in results if not ok]
            if red:
                verdict = "caught"
                note = ", ".join(s.replace("test_contract_", "").replace(".py", "")
                                 for s, _ in red)
            elif label in EXPECTED_SURVIVORS:
                verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:44] + "..."
            else:
                verdict, note = "SURVIVED", "-- nothing --"
                problems.append(label)
            print(f"{label:<76} {verdict:<9} {note}")
            sys.stdout.flush()

        dirty = []
        for p in paths:
            with open(os.path.join(copy, p), "rb") as fh:
                if fh.read() != originals[p]:
                    dirty.append(p)
        if dirty:
            print(f"\nERROR: copy not restored byte for byte: {dirty}")
            return 3
        print()
        if problems:
            print(f"{len(problems)} mutation(s) not caught or not applied:")
            for s in problems:
                print("   *", s)
            return 1
        print(f"ALL {len(chosen)} MUTATIONS CAUGHT OR SHADOWED WITH A STATED REASON "
              "(the working tree was never edited)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
