"""The three consent helpers service contracts call, and the refactors onto them (ADR-0021).

WHAT THIS PINS
--------------
One rule, one implementation. Service contracts needed three things the OEM
consent layer already did in-line, so they were extracted rather than copied:

  contract_statement_visible(db, contract)
      SHARE_DOWNTIME in grants_for(contract.oem_code, contract.factory_tenant_code),
      read at call time. Nothing else counts: another grant, another factory's
      grant, another manufacturer's grant.

  bound_factory_read(tenant_code)
      The brief, explicit factory binding visible_machine already used, as a
      context manager. It restores the previous binding on exit (also on an
      exception) and REFUSES a blank tenant or an OEM sentinel: binding None
      turns the ADR-0002 filter off for every scoped table, which is the one
      thing this helper must never do. visible_machine now calls it.

  widen_grants(db, oem_code, tenant_code, grants, actor, *, context)
      The union the claim handler wrote in-line: add grants, never remove one,
      create the policy when there is none, refuse an unknown grant before
      writing anything, and write the `oem_sharing_changed` audit row in the
      factory's tenant inside the caller's transaction (log_audit commit=False).
      The claim handler now calls it, and the audit rows a claim leaves are
      pinned here to exactly what the in-line code wrote.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_sharing_helpers.py
"""
import ast
import os
import sys

import contract_route_harness as H
import models
import tenancy

HERE = os.path.dirname(os.path.abspath(__file__))
check, section = H.check, H.section


def _oem_sharing():
    import oem_sharing
    return oem_sharing


def _contract(oem="OEM_ALPHA", tenant="FACTORY_A"):
    # The helpers read only these two attributes; a row is not needed.
    return models.ServiceContract(oem_code=oem, factory_tenant_code=tenant)


def _set_grants(oem, tenant, grants):
    with H.unscoped() as db:
        row = (db.query(models.OemDataSharingPolicy)
                 .filter(models.OemDataSharingPolicy.oem_code == oem,
                         models.OemDataSharingPolicy.tenant_code == tenant).first())
        if row is None:
            row = models.OemDataSharingPolicy(oem_code=oem, tenant_code=tenant)
            db.add(row)
        row.grants = grants
        row.updated_by = "test"
        db.commit()


def case_contract_statement_visible():
    section("1. contract_statement_visible: SHARE_DOWNTIME for THIS pair, read now")
    sharing = _oem_sharing()
    check("the helper exists", hasattr(sharing, "contract_statement_visible"))
    if not hasattr(sharing, "contract_statement_visible"):
        return
    with H.unscoped() as db:
        check("CONTROL: FACTORY_A shares only alarms with ALPHA: not visible",
              sharing.contract_statement_visible(db, _contract()) is False)
    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS,SHARE_DOWNTIME")
    with H.unscoped() as db:
        check("SHARE_DOWNTIME granted: visible",
              sharing.contract_statement_visible(db, _contract()) is True)
        check("... not for another factory of the same manufacturer",
              sharing.contract_statement_visible(db, _contract(tenant="FACTORY_B")) is False)
        check("... not for another manufacturer at the same factory",
              sharing.contract_statement_visible(db, _contract(oem="OEM_BETA")) is False)
    _set_grants("OEM_ALPHA", "FACTORY_B", ",".join(
        g for g in sharing.ALL_GRANTS if g != sharing.SHARE_DOWNTIME))
    with H.unscoped() as db:
        check("every OTHER grant is not SHARE_DOWNTIME",
              sharing.contract_statement_visible(db, _contract(tenant="FACTORY_B")) is False)
    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS")
    with H.unscoped() as db:
        check("withdrawal takes effect on the next call (never cached)",
              sharing.contract_statement_visible(db, _contract()) is False)
    with H.unscoped() as db:
        check("a sentinel binding does not hide the policy (explicit key, no hook)",
              _with_binding("OEM:OEM_ALPHA", lambda: sharing.contract_statement_visible(
                  db, _contract(tenant="FACTORY_B"))) is False)
    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS,SHARE_DOWNTIME")
    with H.unscoped() as db:
        check("... and a granted pair is still visible under the OEM's sentinel",
              _with_binding("OEM:OEM_ALPHA", lambda: sharing.contract_statement_visible(
                  db, _contract())) is True)
    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS")
    _set_grants("OEM_ALPHA", "FACTORY_B", "")


def _with_binding(tenant, fn):
    token = tenancy.set_current_tenant(tenant)
    try:
        return fn()
    finally:
        tenancy.reset_current_tenant(token)


def case_bound_factory_read():
    section("2. bound_factory_read binds one factory, restores, and refuses to unbind")
    sharing = _oem_sharing()
    check("the helper exists", hasattr(sharing, "bound_factory_read"))
    if not hasattr(sharing, "bound_factory_read"):
        return
    outer = tenancy.set_current_tenant("OEM:OEM_ALPHA")
    try:
        with sharing.bound_factory_read("FACTORY_A"):
            inside = tenancy.current_tenant()
            with H.SessionLocal() as db:
                names = sorted(m.name for m in db.query(models.Machine).all())
        check("inside, the factory is bound", inside == "FACTORY_A", inside)
        check("... and the ADR-0002 filter reads that factory's rows only",
              names and all(n.startswith(("A1", "A2", "A3", "B1")) for n in names), str(names))
        check("on exit the OEM sentinel is bound again",
              tenancy.current_tenant() == "OEM:OEM_ALPHA", tenancy.current_tenant())
        try:
            with sharing.bound_factory_read("FACTORY_B"):
                raise KeyError("boom")
        except KeyError:
            pass
        check("an exception inside still restores the previous binding",
              tenancy.current_tenant() == "OEM:OEM_ALPHA", tenancy.current_tenant())
        for bad in (None, "", "   ", "OEM:OEM_ALPHA"):
            refused = False
            try:
                with sharing.bound_factory_read(bad):
                    pass
            except ValueError:
                refused = True
            check(f"refuses to bind {bad!r} (None would switch the tenant filter off)",
                  refused and tenancy.current_tenant() == "OEM:OEM_ALPHA")
    finally:
        tenancy.reset_current_tenant(outer)


def _function(path, name):
    with open(os.path.join(HERE, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _calls(fn, attr):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr == attr)
                 or (isinstance(n.func, ast.Name) and n.func.id == attr))]


def case_visible_machine_uses_the_helper():
    section("3. visible_machine binds through bound_factory_read (one implementation)")
    fn = _function("oem_sharing.py", "visible_machine")
    check("found oem_sharing.visible_machine", fn is not None)
    if fn is None:
        return
    check("visible_machine calls bound_factory_read", len(_calls(fn, "bound_factory_read")) == 1)
    check("visible_machine no longer sets the tenant by hand",
          not _calls(fn, "set_current_tenant") and not _calls(fn, "reset_current_tenant"))


def case_widen_grants():
    section("4. widen_grants adds, never removes, and audits in the caller's transaction")
    sharing = _oem_sharing()
    check("the helper exists", hasattr(sharing, "widen_grants"))
    if not hasattr(sharing, "widen_grants"):
        return
    before_rows = len(H.audit_rows("oem_sharing_changed"))
    with H.unscoped() as db:
        # A spy, not a second session: on the in-memory database every session
        # shares one connection, so a second session's close would roll this
        # transaction back and prove nothing.
        calls = []
        real_commit, real_rollback = db.commit, db.rollback
        db.commit = lambda: calls.append("commit")
        db.rollback = lambda: calls.append("rollback")
        policy = sharing.widen_grants(db, "OEM_ALPHA", "FACTORY_A", ["SHARE_DOWNTIME"],
                                      "factory_a_admin", context="unit test")
        db.commit, db.rollback = real_commit, real_rollback
        check("the helper neither commits nor rolls back: the caller's transaction decides",
              calls == [], str(calls))
        pending = [o for o in db.new if isinstance(o, models.AuditLog)]
        check("... and its audit row is pending in that transaction, in the factory's tenant",
              len(pending) == 1 and pending[0].tenant_code == "FACTORY_A", str(pending))
        db.commit()
        pid = policy.id
    check("the existing grant is kept and the new one added",
          H.grants() == {"SHARE_ALARMS", "SHARE_DOWNTIME"}, str(H.grants()))
    rows = H.audit_rows("oem_sharing_changed")[before_rows:]
    check("one audit row in the factory's tenant with the historic wording",
          rows == [("FACTORY_A", "oem_sharing_changed", "oem_data_sharing_policy", pid,
                    "oem=OEM_ALPHA before='SHARE_ALARMS' "
                    "after='SHARE_ALARMS,SHARE_DOWNTIME' (unit test)", "factory_a_admin")],
          str(rows))

    with H.unscoped() as db:
        sharing.widen_grants(db, "OEM_ALPHA", "FACTORY_A", [], "factory_a_admin",
                             context="nothing asked")
        db.commit()
    check("an empty request removes nothing", H.grants() == {"SHARE_ALARMS", "SHARE_DOWNTIME"})

    with H.unscoped() as db:
        policy = sharing.widen_grants(db, "OEM_BETA", "FACTORY_C", ["SHARE_DOWNTIME"],
                                      "factory_c_admin", context="first grant")
        db.commit()
        pid = policy.id
    check("with no policy one is created holding exactly the grant",
          H.grants("OEM_BETA", "FACTORY_C") == {"SHARE_DOWNTIME"})
    check("... and audited from '(no policy)'",
          H.audit_rows("oem_sharing_changed")[-1][4]
          == "oem=OEM_BETA before='(no policy)' after='SHARE_DOWNTIME' (first grant)"
          and H.audit_rows("oem_sharing_changed")[-1][3] == pid,
          str(H.audit_rows("oem_sharing_changed")[-1]))

    count = len(H.audit_rows())
    for bad in (["SHARE_EVERYTHING"], ["SHARE_DOWNTIME", "share_alarms"]):
        refused = False
        with H.unscoped() as db:
            try:
                sharing.widen_grants(db, "OEM_ALPHA", "FACTORY_B", bad, "x", context="bad")
            except ValueError:
                refused = True
            db.commit()
        check(f"an unknown grant {bad!r} is refused before anything is written",
              refused and len(H.audit_rows()) == count and H.grants(tenant="FACTORY_B") == set())

    with H.unscoped() as db:
        sharing.widen_grants(db, "OEM_ALPHA", "FACTORY_B", ["SHARE_DOWNTIME"], "fb",
                             context="rolled back")
        db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="duplicate"))  # fails at commit
        try:
            db.commit()
            committed = True
        except Exception:
            db.rollback()
            committed = False
    check("CONTROL: the caller's commit failed", committed is False)
    check("... and neither the grant nor its audit row survived",
          H.grants(tenant="FACTORY_B") == set() and len(H.audit_rows()) == count)


def case_claim_path_audit_rows_are_unchanged():
    section("5. Accepting a claim leaves exactly the audit rows it always did")
    handler = _function("connected_equipment_routes.py", "accept_claim")
    check("found connected_equipment_routes.accept_claim", handler is not None)
    if handler is not None:
        check("the claim handler widens consent through oem_sharing.widen_grants",
              len(_calls(handler, "widen_grants")) == 1)
        check("... and no longer writes OemDataSharingPolicy itself",
              not any(isinstance(n, ast.Attribute) and n.attr == "OemDataSharingPolicy"
                      for n in ast.walk(handler)))

    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS,SHARE_DOWNTIME")
    with H.unscoped() as db:
        check("CONTROL: FACTORY_C has no policy with OEM_ALPHA",
              db.query(models.OemDataSharingPolicy)
              .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
                      models.OemDataSharingPolicy.tenant_code == "FACTORY_C").count() == 0)
        for serial, oem in (("SN-CLAIM-1", "OEM_ALPHA"), ("SN-CLAIM-2", "OEM_ALPHA")):
            model = (db.query(models.MachineModel)
                       .filter(models.MachineModel.oem_code == oem).first())
            db.add(models.MachineInstallation(oem_code=oem, serial_number=serial,
                                              model_id=model.id, status="Manufactured",
                                              site=""))
        db.commit()
        ids = {r.serial_number: r.id for r in db.query(models.MachineInstallation).all()}
        policy_ids_before = {(p.oem_code, p.tenant_code): p.id
                             for p in db.query(models.OemDataSharingPolicy).all()}

    for serial, tenant, tok, grants_asked, before, after in (
            ("SN-CLAIM-1", "FACTORY_A", "fa", ["SHARE_OPERATING_HOURS"],
             "'SHARE_ALARMS,SHARE_DOWNTIME'",
             "'SHARE_ALARMS,SHARE_DOWNTIME,SHARE_OPERATING_HOURS'"),
            ("SN-CLAIM-2", "FACTORY_C", "fc", [], "'(no policy)'", "''")):
        issued = H.POST(f"/oem/machines/{ids[serial]}/claim", H.TOKENS["alpha"], {})
        check(f"CONTROL: a claim code is issued for {serial}", issued.status == 200, issued)
        mark = len(H.audit_rows())
        accepted = H.POST(f"/connected-equipment/claim/{issued.body.get('claim_code')}",
                          H.TOKENS[tok], {"grants": grants_asked})
        check(f"{tenant} accepts {serial}", accepted.status == 200, accepted)
        with H.unscoped() as db:
            policy = (db.query(models.OemDataSharingPolicy)
                        .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
                                models.OemDataSharingPolicy.tenant_code == tenant).first())
        actor = f"factory_{tenant[-1].lower()}_admin"
        expected = [
            (tenant, "claim_accepted", "machine_installation", ids[serial],
             f"oem=OEM_ALPHA serial={serial} hint={issued.body.get('code_hint')} "
             f"Manufactured -> Assigned tenant={tenant}", actor),
            (tenant, "oem_sharing_changed", "oem_data_sharing_policy", policy.id,
             f"oem=OEM_ALPHA before={before} after={after} (at claim)", actor),
        ]
        rows = H.audit_rows()[mark:]
        check(f"the audit rows for {serial} are exactly the historic two, in order",
              rows == expected, f"{rows} != {expected}")
        if ("OEM_ALPHA", tenant) in policy_ids_before:
            check("... against the existing policy row",
                  policy.id == policy_ids_before[("OEM_ALPHA", tenant)])


def run_all():
    H.wire()
    H.seed()
    _set_grants("OEM_ALPHA", "FACTORY_A", "SHARE_ALARMS")
    case_contract_statement_visible()
    case_bound_factory_read()
    case_visible_machine_uses_the_helper()
    case_widen_grants()
    case_claim_path_audit_rows_are_unchanged()


def test_oem_sharing_helpers():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("THE CONSENT HELPERS ARE ONE IMPLEMENTATION EACH, AND THE CLAIM PATH IS UNCHANGED")
