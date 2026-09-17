"""Coverage ends where an installation stops pointing at the machine a contract accepted (ADR-0020, C8).

WHAT THIS PINS
--------------
A service contract snapshots, at acceptance, which factory machine each covered
installation is linked to. When that link changes — the factory unlinks or
relinks the machine, releases the equipment, or is offboarded — the evidence
AMP reads (that machine's spans and reasons) is no longer evidence about the
covered equipment. So:

  * ONE listener (contract_linkage, a before_flush hook) stamps
    service_contract_machines.coverage_ended_at (now, whole seconds) and
    coverage_end_reason on every open coverage row of that installation, and
    writes `contract_coverage_ended` audit rows for BOTH parties and a
    CoverageEnded event IN THE SAME FLUSH as the change. A failed commit
    leaves neither.
  * An edit that does not change machine_id or factory_tenant_code (site,
    status, hours, last_seen_at, or setting the same value) stamps nothing.
    An installation no contract covers is left alone. A stamp is never moved.
  * Covered time from the stamp on is UNMEASURED `installation_unlinked` in the
    statement; time before it is attributed as normal. Past periods are not
    touched (C8: releasing a machine in October does not rewrite September).
  * A bulk UPDATE never passes through before_flush. STRUCTURAL GUARD: every
    bulk UPDATE of machine_installations in application code that can change
    machine_id or factory_tenant_code either calls contract_linkage.end_coverage
    in the same function or is listed with the reason it cannot touch a covered
    installation; the guard asserts it found the writers it knows about.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_linkage.py
"""
import ast
import os
from datetime import datetime, timedelta

import contract_route_harness as H
import models

HERE = os.path.dirname(os.path.abspath(__file__))
check, section = H.check, H.section


def linkage():
    import contract_linkage
    return contract_linkage


class _Clock:
    def __init__(self, at):
        self.at = at

    def __enter__(self):
        self.original = linkage().utcnow
        linkage().utcnow = lambda: self.at
        return self

    def __exit__(self, *exc):
        linkage().utcnow = self.original


def coverage(serial):
    with H.unscoped() as db:
        return [(r.term_version_id, r.coverage_ended_at, r.coverage_end_reason)
                for r in db.query(models.ServiceContractMachine)
                .filter(models.ServiceContractMachine.installation_id == H.S["inst"][serial])
                .order_by(models.ServiceContractMachine.id.asc()).all()]


def events(kind="CoverageEnded"):
    with H.unscoped() as db:
        return [e.payload for e in db.query(models.EventLog)
                .filter(models.EventLog.event_type == kind)
                .order_by(models.EventLog.id.asc()).all()]


def edit_installation(serial, **values):
    with H.unscoped() as db:
        row = db.get(models.MachineInstallation, H.S["inst"][serial])
        for k, v in values.items():
            setattr(row, k, v)
        db.commit()


def case_unlinking_ends_coverage_in_the_same_flush():
    section("1. UNLINKING A COVERED MACHINE STAMPS COVERAGE, AUDITS BOTH PARTIES, PUBLISHES")
    cid = H.active_contract(serials=("SN-A1",))
    H.S["cid"] = cid
    check("CONTROL: coverage is open after acceptance",
          [c[1] for c in coverage("SN-A1")] == [None], str(coverage("SN-A1")))
    audits_before = len(H.audit_rows("contract_coverage_ended"))
    at = H.now_utc() + timedelta(hours=1)
    with _Clock(at):
        r = H.POST(f"/connected-equipment/{H.S['inst']['SN-A1']}/link", H.TOKENS["fa"],
                   {"machine_id": None})
    check("CONTROL: the factory unlinked SN-A1 through the real route", r.status == 200, r)
    check("coverage_ended_at is stamped with the listener's clock, reason machine_unlinked",
          [c[1:] for c in coverage("SN-A1")] == [(at, "machine_unlinked")], str(coverage("SN-A1")))
    rows = H.audit_rows("contract_coverage_ended")[audits_before:]
    check("two audit rows: the factory's tenant and the manufacturer's sentinel",
          sorted(r[0] for r in rows) == ["FACTORY_A", "OEM:OEM_ALPHA"]
          and all(r[2] == "service_contract" and r[3] == cid for r in rows), str(rows))
    check("the audit details name the serial, the stamp and the reason, and no figures",
          all("SN-A1" in r[4] and at.strftime("%Y-%m-%dT%H:%M:%SZ") in r[4]
              and "machine_unlinked" in r[4]
              for r in rows), str(rows))
    evs = events()
    check("one CoverageEnded event for the contract and installation",
          len(evs) == 1 and f'"contract_id": {cid}' in evs[0]
          and f'"installation_id": {H.S["inst"]["SN-A1"]}' in evs[0], str(evs))
    kinds = [n for n in H.notifications() if n[1] == "coverage_ended"]
    check("both parties are notified", sorted(n[0] for n in kinds) == ["FACTORY_A", "OEM:OEM_ALPHA"],
          str(kinds))

    with _Clock(at + timedelta(days=3)):
        r = H.POST(f"/connected-equipment/{H.S['inst']['SN-A1']}/link", H.TOKENS["fa"],
                   {"machine_id": H.S["machines"]["A1"]})
    check("CONTROL: relinking succeeded", r.status == 200, r)
    check("a later change never moves the first stamp",
          [c[1:] for c in coverage("SN-A1")] == [(at, "machine_unlinked")], str(coverage("SN-A1")))
    check("... and writes no second audit pair", len(H.audit_rows("contract_coverage_ended"))
          == audits_before + 2)


def case_edits_that_are_not_a_link_change():
    section("2. EDITS THAT DO NOT CHANGE THE LINK, AND UNCOVERED INSTALLATIONS, STAMP NOTHING")
    cid = H.active_contract(serials=("SN-A2",))
    audits = len(H.audit_rows("contract_coverage_ended"))
    edit_installation("SN-A2", site="Bay 7", status="Active", operating_hours=1234.5,
                      last_seen_at=datetime.utcnow())
    edit_installation("SN-A2", machine_id=H.S["machines"]["A2"],
                      factory_tenant_code="FACTORY_A")
    check("site, status, hours, last_seen_at and same-value writes do not end coverage",
          [c[1] for c in coverage("SN-A2")] == [None], str(coverage("SN-A2")))
    with H.unscoped() as db:
        # An EXPIRED attribute has no old value in its history, so a write of
        # the same value looks like a change there. The listener compares with
        # what the database holds instead.
        row = db.get(models.MachineInstallation, H.S["inst"]["SN-A2"])
        db.expire(row, ["machine_id", "factory_tenant_code"])
        row.machine_id = H.S["machines"]["A2"]
        row.factory_tenant_code = "FACTORY_A"
        db.commit()
    check("a same-value write over expired attributes does not end coverage either",
          [c[1] for c in coverage("SN-A2")] == [None], str(coverage("SN-A2")))
    edit_installation("SN-B1", machine_id=None)
    check("an installation no contract covers changes without an audit row",
          len(H.audit_rows("contract_coverage_ended")) == audits)
    import mqtt_service
    mqtt_service.SessionLocal = H.SessionLocal

    class Msg:
        topic = "flowmes/FACTORY_A/Plant/machines"
        payload = b'{"machine": "A2-SECRETNAME", "status": "Running", "utilization": 50}'
    mqtt_service.on_message(None, None, Msg())
    check("MQTT reporting (last_seen_at on the installation) does not end coverage",
          [c[1] for c in coverage("SN-A2")] == [None], str(coverage("SN-A2")))
    H.S["cid_a2"] = cid


def case_a_failed_commit_leaves_nothing():
    section("3. THE STAMP AND ITS AUDIT ROWS LIVE OR DIE WITH THE CHANGE")
    audits = len(H.audit_rows("contract_coverage_ended"))
    with H.unscoped() as db:
        row = db.get(models.MachineInstallation, H.S["inst"]["SN-A2"])
        row.machine_id = None
        db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="duplicate key"))
        failed = False
        try:
            db.commit()
        except Exception:
            db.rollback()
            failed = True
    check("CONTROL: the commit failed", failed)
    check("coverage is still open", [c[1] for c in coverage("SN-A2")] == [None], str(coverage("SN-A2")))
    check("no audit row was left behind", len(H.audit_rows("contract_coverage_ended")) == audits)


def case_release_and_offboarding():
    section("4. RELEASING THE EQUIPMENT (A BULK UPDATE) AND OFFBOARDING ALSO END COVERAGE")
    at = H.now_utc() + timedelta(days=2)
    with _Clock(at):
        r = H.POST(f"/connected-equipment/{H.S['inst']['SN-A2']}/release", H.TOKENS["fa"], {})
    check("CONTROL: FACTORY_A released SN-A2", r.status == 200, r)
    check("release stamps coverage with reason installation_released",
          [c[1:] for c in coverage("SN-A2")] == [(at, "installation_released")],
          str(coverage("SN-A2")))

    cid = H.active_contract(serials=("SN-AB",), tenant="FACTORY_B", fac_tok=H.TOKENS["fb"])
    at = H.now_utc() + timedelta(days=4, seconds=17)
    import offboard_tenant
    with _Clock(at), H.unscoped() as db:
        offboard_tenant.purge_tenant_data(db, "FACTORY_B")
    check("offboarding the factory stamps coverage with reason installation_released",
          [c[1:] for c in coverage("SN-AB")] == [(at, "installation_released")],
          str(coverage("SN-AB")))
    H.S["cid_ab"] = cid


def case_the_statement_reads_the_stamp():
    section("5. IN THE STATEMENT: ATTRIBUTED BEFORE THE STAMP, NO DATA AFTER IT, PAST PERIODS UNTOUCHED")
    # SN-A1 is linked to its machine again (case 1). Its first contract is ended
    # at its own start, so a fresh contract can cover it from four months back
    # and two closed periods exist.
    with H.unscoped() as db:
        for row in db.query(models.ServiceContract).filter(
                models.ServiceContract.id == H.S["cid"]).all():
            row.status = "terminated"
            row.termination_effective_at = row.starts_at
        db.commit()
    cid = H.active_contract(serials=("SN-A1",), start_offset=-4)
    (p0s, p0e), (p1s, p1e), _, _ = H.measured_periods(cid, gap_hours=0)
    # The ended contract's coverage row is reopened, as if it had never been
    # stamped: a contract whose effective end has passed must not be stamped,
    # audited or notified, because no period after the change exists for it.
    with H.unscoped() as db:
        old_row = (db.query(models.ServiceContractMachine)
                     .join(models.ServiceContractTermVersion,
                           models.ServiceContractTermVersion.id
                           == models.ServiceContractMachine.term_version_id)
                     .filter(models.ServiceContractTermVersion.contract_id == H.S["cid"]).one())
        old_row.coverage_ended_at = None
        old_row.coverage_end_reason = None
        old_row_id = old_row.id
        db.commit()
    audits = [r for r in H.audit_rows("contract_coverage_ended") if r[3] == H.S["cid"]]
    stamp = p1s + timedelta(days=5)
    with _Clock(stamp):
        edit_installation("SN-A1", machine_id=None)
    with H.unscoped() as db:
        reopened = db.get(models.ServiceContractMachine, old_row_id)
        check("a contract that has already ended is not stamped",
              reopened.coverage_ended_at is None, str(reopened.coverage_ended_at))
    check("... nor audited again",
          [r for r in H.audit_rows("contract_coverage_ended") if r[3] == H.S["cid"]] == audits)
    first = H.compute(cid, H.periods(cid)[0]["start"])
    second = H.compute(cid, H.periods(cid)[1]["start"])
    check("CONTROL: both closed periods compute", first.status == 200 and second.status == 200,
          f"{first} {second}")
    s0 = H.GET(f"/oem/contracts/{cid}/statements/{first.body['statement']['id']}", H.TOKENS["alpha"])
    s1 = H.GET(f"/oem/contracts/{cid}/statements/{second.body['statement']['id']}", H.TOKENS["alpha"])
    m0 = s0.body["content"]["machines"][0]
    m1 = s1.body["content"]["machines"][0]
    check("the period before the stamp has no coverage_end and no unlinked time",
          m0["coverage_end"] is None and m0["totals"]["unmeasured_seconds"] == 0, str(m0["totals"]))
    unlinked = [i for i in m1["intervals"] if i["cause"] == "installation_unlinked"]
    check("the stamped period is UNMEASURED installation_unlinked from the stamp to its end",
          len(unlinked) == 1 and unlinked[0]["start"] == stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
          and unlinked[0]["end"] == p1e.strftime("%Y-%m-%dT%H:%M:%SZ")
          and unlinked[0]["bucket"] == "UNMEASURED", str(unlinked))
    before = [i for i in m1["intervals"] if i["end"] <= stamp.strftime("%Y-%m-%dT%H:%M:%SZ")]
    check("... and attributed as AVAILABLE before it",
          before and all(i["bucket"] == "AVAILABLE" for i in before), str(before))


# ── Structural guard ────────────────────────────────────────────────────────

SKIP_PREFIXES = ("test_", "mutate_", "audit_", "verify_pg_")
SKIP_FILES = {"oem_perf.py", "contract_route_harness.py", "conftest.py"}

# Bulk UPDATEs of machine_installations that cannot reach a covered installation,
# each with the reason. Anything else that can change the link must call
# contract_linkage.end_coverage in the same function.
NO_COVERAGE_POSSIBLE = {
    ("oem_claims.py", "accept"): (
        "its WHERE requires factory_tenant_code IS NULL: an installation with no "
        "factory cannot be covered (coverage needs an accepted factory link), and "
        "one that lost its factory already had its coverage ended when it did"),
}
LINK_COLUMNS = {"machine_id", "factory_tenant_code"}


def _app_files():
    for name in sorted(os.listdir(HERE)):
        if (name.endswith(".py") and not name.startswith(SKIP_PREFIXES)
                and name not in SKIP_FILES):
            yield name


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _mentions_installation(node):
    return any(isinstance(n, ast.Attribute) and n.attr == "MachineInstallation"
               for n in ast.walk(node))


def _bulk_updates(fn):
    """(call, keys) for every `.update({...})` / `update(MachineInstallation)` on
    machine_installations in this function."""
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute) and n.func.attr == "update" \
                and n.args and isinstance(n.args[0], ast.Dict) and _mentions_installation(n.func.value):
            keys = {k.value for k in n.args[0].keys if isinstance(k, ast.Constant)}
            out.append((n, keys))
        elif isinstance(n.func, ast.Name) and n.func.id == "update" and n.args \
                and _mentions_installation(n.args[0]):
            keys = set()
            parent_values = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                             and isinstance(c.func, ast.Attribute) and c.func.attr == "values"
                             and any(inner is n for inner in ast.walk(c.func.value))]
            for v in parent_values:
                keys |= {kw.arg for kw in v.keywords}
            out.append((n, keys or {"<unknown>"}))
    return out


def _calls(fn, attr):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == attr for n in ast.walk(fn))


def _instance_link_writes(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Attribute) and t.attr in LINK_COLUMNS
            and isinstance(t.value, ast.Name) and t.value.id in ("inst", "row", "installation")]


def case_structural_guard():
    section("6. STRUCTURAL: NO BULK INSTALLATION WRITE BYPASSES THE LISTENER")
    bulk, instance = {}, {}
    offenders = []
    for name in _app_files():
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:
                continue
        for fn in _functions(tree):
            for call, keys in _bulk_updates(fn):
                bulk[(name, fn.name)] = keys
                if not (keys & LINK_COLUMNS or "<unknown>" in keys):
                    continue
                if (name, fn.name) in NO_COVERAGE_POSSIBLE:
                    continue
                if not _calls(fn, "end_coverage"):
                    offenders.append(f"{name}:{fn.name}() line {call.lineno} sets {sorted(keys)}")
            if _instance_link_writes(fn) and _mentions_installation(fn):
                instance[(name, fn.name)] = True
    check("found the release route's bulk UPDATE",
          ("connected_equipment_routes.py", "release_installation") in bulk, str(sorted(bulk)))
    check("found the claim's bulk UPDATE", ("oem_claims.py", "accept") in bulk, str(sorted(bulk)))
    check("found the link route's instance write (the listener covers it)",
          ("connected_equipment_routes.py", "link_installation") in instance, str(sorted(instance)))
    check("found offboarding's instance writes (the listener covers them)",
          ("offboard_tenant.py", "_unlink_oem_installations") in instance, str(sorted(instance)))
    check("every link-changing bulk UPDATE calls end_coverage or is argued safe",
          not offenders, "; ".join(offenders))
    for key, why in NO_COVERAGE_POSSIBLE.items():
        check(f"the exemption for {key[0]}:{key[1]} still names a real bulk UPDATE",
              key in bulk and bool(why))


def run_all():
    H.boot()
    H.seed()
    case_unlinking_ends_coverage_in_the_same_flush()
    case_edits_that_are_not_a_link_change()
    case_a_failed_commit_leaves_nothing()
    case_release_and_offboarding()
    case_the_statement_reads_the_stamp()
    case_structural_guard()


def test_contract_linkage():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("COVERAGE ENDS WHERE THE LINK CHANGES, IN THE SAME FLUSH, AND NOTHING BYPASSES IT")
