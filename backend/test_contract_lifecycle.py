"""A service contract's life: draft, propose, explicit acceptance, termination (ADR-0021).

THE PROPERTIES UNDER TEST
-------------------------
  * A draft is the manufacturer's alone. The factory cannot list it, fetch it,
    or read about it in its audit log, even after the draft is withdrawn.
  * A contract names only the manufacturer's OWN equipment at THAT factory. A
    contract that could name a competitor's machine, or be addressed to a
    factory where the manufacturer has no equipment, would put a stranger's
    paperwork on a factory's screen — the ADR-0019 threat, by another door.
  * Each party is bound to the exact terms hash it saw. Proposing records the
    OEM's hash; accepting records the factory's; a stale hash is refused.
  * The factory's acceptance is explicit twice over: it must carry the terms
    hash AND `grant_downtime_sharing: true`, and the grant WIDENS the existing
    sharing policy rather than replacing it (C7).
  * A machine is covered by one contract at a time, and only while it is linked
    to a machine on the shop floor.
  * Every transition is decided by a conditional UPDATE's row count, writes
    exactly two audit rows (one per party) in the same transaction, and leaves
    nothing behind when that transaction fails.
  * Termination takes effect on a period boundary after the notice period.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_lifecycle.py
"""
import threading
from datetime import timedelta

from sqlalchemy.orm import Session

import canonical
import contract_route_harness as H
import models
from contract_route_harness import GET, POST, PUT, S, TOKENS, check, section

SENTINEL_A = "OEM:OEM_ALPHA"


def _count(model, **filters):
    with H.unscoped() as db:
        q = db.query(model)
        for k, v in filters.items():
            q = q.filter(getattr(model, k) == v)
        return q.count()


def case_a_draft_is_the_manufacturers_alone():
    section("1. A DRAFT IS THE MANUFACTURER'S ALONE")
    r = H.draft()
    check("an OEM admin drafts a contract", r.status == 200, r)
    cid = r.body.get("id")
    check("...stored as a draft", r.body.get("status") == "draft", r)
    versions = r.body.get("versions") or [{}]
    check("...with version 1 carrying a canonical terms hash",
          len(versions) == 1 and canonical.is_sha256_hex(versions[0].get("terms_hash")), r)
    check("...starting at local midnight on the 1st (Asia/Kolkata is 18:30Z the day before)",
          str(r.body.get("starts_at", "")).endswith("T18:30:00Z"), r)
    start, end = H.parse_ts(r.body["starts_at"]), H.parse_ts(r.body["ends_at"])
    check("...and ending term_months (12) later, on a boundary",
          363 <= (end - start).days <= 367 and r.body["ends_at"].endswith("T18:30:00Z"), r)

    check("a service manager may draft (manage_contracts)",
          H.draft(tok=TOKENS["alpha_mgr"]).status == 200)
    check("a viewer may not draft", H.draft(tok=TOKENS["alpha_view"]).status == 403)
    check("a service engineer may not draft",
          H.draft(tok=TOKENS["alpha_eng"]).status == 403)

    lst = GET("/service-contracts", TOKENS["fa"])
    check("the factory's list does not include the draft",
          lst.status == 200 and all(c["id"] != cid for c in lst.body.get("contracts", [])),
          lst)
    check("the factory gets 404 on a draft addressed to it",
          GET(f"/service-contracts/{cid}", TOKENS["fa"]).status == 404)
    check("the OEM lists its own draft",
          any(c["id"] == cid for c in GET("/oem/contracts", TOKENS["alpha"]).body.get("contracts", [])))
    rows = [row for row in H.audit_rows("contract_drafted") if row[3] == cid]
    check("drafting is audited once, in the OEM's sentinel tenant only",
          [row[0] for row in rows] == [SENTINEL_A], rows)

    # A draft is edited in place, and only while it is a draft.
    before = r.body["versions"][0]["terms_hash"]
    body = {"contract_ref": r.body["contract_ref"], "title": "AMC with SLA",
            "contract_type": "UPTIME_CLAUSE", "factory_tenant_code": "FACTORY_A",
            "start_month": H.month_label(-3),
            # A lower target needs tiers at or below it: contract_terms refuses
            # a tier that would credit a period which MET the SLA.
            "terms": H.terms(("SN-A1", "SN-A2"), sla_target_pct="96.00",
                             credit_tiers=[{"below_pct": "96.00", "credit_pct": "5.00"},
                                           {"below_pct": "95.00", "credit_pct": "10.00"}])}
    e = PUT(f"/oem/contracts/{cid}/draft", TOKENS["alpha"], body)
    check("the OEM edits its draft", e.status == 200 and e.body.get("title") == "AMC with SLA", e)
    after = (e.body.get("versions") or [{}])[0].get("terms_hash")
    check("...and the terms hash follows the new terms", after and after != before, e)
    check("a viewer cannot edit a draft",
          PUT(f"/oem/contracts/{cid}/draft", TOKENS["alpha_view"], body).status == 403)
    rows = [row for row in H.audit_rows("contract_draft_edited") if row[3] == cid]
    check("an edit is audited on the OEM side only",
          [row[0] for row in rows] == [SENTINEL_A], rows)

    # Withdrawing a draft must not make it visible: it was never proposed.
    w = H.draft()
    wid = w.body["id"]
    ww = POST(f"/oem/contracts/{wid}/withdraw", TOKENS["alpha"], {})
    check("an OEM admin withdraws a draft", ww.status == 200 and ww.body.get("status") == "withdrawn", ww)
    check("a withdrawn DRAFT stays invisible to the factory (it was never proposed)",
          GET(f"/service-contracts/{wid}", TOKENS["fa"]).status == 404)
    check("...and is not in its list",
          all(c["id"] != wid for c in GET("/service-contracts", TOKENS["fa"]).body.get("contracts", [])))
    rows = [row for row in H.audit_rows("contract_withdrawn") if row[3] == wid]
    check("...and its withdrawal is audited on the OEM side only",
          [row[0] for row in rows] == [SENTINEL_A], rows)
    S["edited_draft"] = cid


def case_a_contract_names_only_the_manufacturers_equipment_at_that_factory():
    section("2. A CONTRACT NAMES ONLY THE OEM'S OWN EQUIPMENT AT THAT FACTORY")
    before = _count(models.ServiceContract)

    def refused(label, status, **kw):
        r = H.draft(**kw)
        check(label, r.status == status, r)

    refused("a competitor's machine at the same factory is refused", 422, serials=("SN-B1",))
    refused("the OEM's own machine at ANOTHER factory is refused", 422,
            serials=("SN-AB",), tenant="FACTORY_A")
    refused("unassigned stock is refused", 422, serials=("SN-A9",))
    refused("a factory where the OEM has no equipment is refused", 422,
            serials=("SN-A1",), tenant="FACTORY_C")
    refused("the reserved sentinel namespace is refused as a factory", 422,
            serials=("SN-A1",), tenant="OEM:OEM_ALPHA")

    mismatch = H.terms(("SN-A1",))
    mismatch["covered_installations"][0]["serial_number"] = "SN-A2"
    r = POST("/oem/contracts", TOKENS["alpha"],
             {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
              "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(1),
              "terms": mismatch})
    check("an installation id paired with another serial is refused", r.status == 422, r)

    empty = H.terms(("SN-A1",))
    empty["covered_installations"] = []
    r = POST("/oem/contracts", TOKENS["alpha"],
             {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
              "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(1),
              "terms": empty})
    check("a contract covering nothing is refused", r.status == 422, r)

    for bad in ("2026-13", "2026-1", "26-01", "2026-00", ""):
        r = POST("/oem/contracts", TOKENS["alpha"],
                 {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
                  "factory_tenant_code": "FACTORY_A", "start_month": bad,
                  "terms": H.terms(("SN-A1",))})
        check(f"start_month {bad!r} is refused", r.status == 422, r)
    # A month whose term would end past the calendar (year 9999) is a bad
    # request, not a server error: the refusal must be the rule's 422, never an
    # unhandled ValueError surfacing as a 500.
    for far in ("9999-06", "9999-12"):
        status = H.status_of(lambda: POST(
            "/oem/contracts", TOKENS["alpha"],
            {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
             "factory_tenant_code": "FACTORY_A", "start_month": far,
             "terms": H.terms(("SN-A1",))}))
        check(f"start_month {far!r}, whose term ends past the calendar, is refused with 422",
              status == 422, status)
    # Free text a database cannot store (PostgreSQL refuses NUL in text) or that
    # a one-line label must not carry. A 422 naming the field, never a 500 and
    # never a stored row. (A lone surrogate never reaches the service: request
    # validation refuses it as string_unicode.)
    for field, value in (("contract_ref", "AMC-\x00"), ("contract_ref", "AMC\n2"),
                         ("contract_ref", "AMC\t2"), ("title", "t\x00"),
                         ("title", "two\nlines"), ("factory_tenant_code", "FACTORY_A\x00")):
        body = {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
                "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(1),
                "terms": H.terms(("SN-A1",))}
        body[field] = value
        status = H.status_of(lambda: POST("/oem/contracts", TOKENS["alpha"], body))
        check(f"a {field} of {value!r} is refused with 422", status == 422, status)

    r = H.draft(period_months=2)
    check("invalid terms are refused with the field named",
          r.status == 422 and "period_months" in str(r.body), r)
    r = POST("/oem/contracts", TOKENS["alpha"],
             {"contract_ref": H.new_ref(), "title": "t", "contract_type": "LEASE",
              "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(1),
              "terms": H.terms(("SN-A1",))})
    check("an unknown contract type is refused", r.status == 422, r)
    r = POST("/oem/contracts", TOKENS["alpha"],
             {"contract_ref": H.new_ref(), "title": "t", "contract_type": "AMC",
              "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(1),
              "terms": H.terms(("SN-A1",)), "oem_code": "OEM_BETA"})
    check("a body naming an oem_code is refused, not honoured", r.status == 422, r)
    check("none of the refusals created a contract", _count(models.ServiceContract) == before)

    ok = H.draft(ref="AMC-DUP")
    check("CONTROL: a valid draft with that reference is created", ok.status == 200, ok)
    dup = H.draft(ref="AMC-DUP")
    check("a second draft with the same reference is refused with 409", dup.status == 409, dup)
    other = H.draft(tok=TOKENS["beta"], serials=("SN-B1",), ref="AMC-DUP")
    check("another manufacturer may use the same reference", other.status == 200, other)


def case_proposing_binds_the_oem_to_the_hash_it_saw():
    section("3. PROPOSING BINDS THE OEM TO THE HASH IT SAW")
    cid = S["edited_draft"]
    detail = H.contract_detail(cid)
    current = detail.body["versions"][0]["terms_hash"]

    r = POST(f"/oem/contracts/{cid}/propose", TOKENS["alpha"], {"terms_hash": "0" * 64})
    check("proposing with a stale hash is refused with 409", r.status == 409, r)
    check("...and the contract is still a draft",
          H.contract_detail(cid).body.get("status") == "draft")
    r = POST(f"/oem/contracts/{cid}/propose", TOKENS["alpha_mgr"], {"terms_hash": current})
    check("a service manager cannot propose (sign_contracts)", r.status == 403, r)
    r = POST(f"/oem/contracts/{cid}/propose", TOKENS["alpha"], {"terms_hash": current})
    check("an OEM admin proposes with the current hash", r.status == 200, r)
    check("...status proposed", r.body.get("status") == "proposed", r)
    v = (r.body.get("versions") or [{}])[0]
    check("...version 1 proposed, carrying the OEM's acceptance of exactly that hash",
          v.get("status") == "proposed" and v.get("oem_accepted_hash") == current
          and v.get("oem_accepted_by") == "alpha_admin", v)
    check("proposing twice is refused",
          POST(f"/oem/contracts/{cid}/propose", TOKENS["alpha"],
               {"terms_hash": current}).status == 409)
    body = {"contract_ref": detail.body["contract_ref"], "title": "changed",
            "contract_type": "AMC", "factory_tenant_code": "FACTORY_A",
            "start_month": H.month_label(-3), "terms": H.terms(("SN-A1",))}
    check("a proposed contract cannot be edited as a draft",
          PUT(f"/oem/contracts/{cid}/draft", TOKENS["alpha"], body).status == 409)

    f = GET(f"/service-contracts/{cid}", TOKENS["fa"])
    check("the factory now sees the proposal and its terms",
          f.status == 200 and f.body.get("status") == "proposed"
          and f.body["versions"][0]["terms_hash"] == current
          and f.body["versions"][0]["terms"]["sla_target_pct"] == "96.00", f)
    check("a factory Supervisor may read it",
          GET(f"/service-contracts/{cid}", TOKENS["fa_super"]).status == 200)
    check("a factory Operator may not",
          GET(f"/service-contracts/{cid}", TOKENS["fa_op"]).status == 403)
    rows = sorted(row[0] for row in H.audit_rows("contract_proposed") if row[3] == cid)
    check("the proposal is audited once for each party",
          rows == ["FACTORY_A", SENTINEL_A], rows)
    notes = [n for n in H.notifications() if n[1] == "contract_proposed"]
    check("both parties are notified of the proposal",
          sorted(n[0] for n in notes) == ["FACTORY_A", SENTINEL_A], notes)
    S["proposed"] = cid


def case_the_factory_accepts_explicitly():
    section("4. THE FACTORY ACCEPTS EXPLICITLY: THE HASH AND THE GRANT")
    cid = S["proposed"]
    h = H.version_hash(cid)
    check("a factory Supervisor cannot accept",
          H.factory_accept(cid, tok=TOKENS["fa_super"]).status == 403)
    r = H.factory_accept(cid, grant=None)
    check("accept without grant_downtime_sharing is refused with 422", r.status == 422, r)
    r = H.factory_accept(cid, grant=False)
    check("accept with grant_downtime_sharing false is refused with 422", r.status == 422, r)
    r = POST(f"/service-contracts/{cid}/accept", TOKENS["fa"],
             {"terms_hash": h, "grant_downtime_sharing": "true"})
    check("the STRING 'true' is not consent", r.status == 422, r)
    r = H.factory_accept(cid, terms_hash="f" * 64)
    check("accept with the wrong terms hash is refused with 409", r.status == 409, r)
    r = POST(f"/service-contracts/{cid}/accept", TOKENS["alpha"],
             {"terms_hash": h, "grant_downtime_sharing": True})
    check("an OEM token cannot accept on the factory's behalf", r.status == 403, r)
    check("another factory gets 404", H.factory_accept(cid, tok=TOKENS["fb"]).status == 404)
    check("nothing so far changed the sharing policy",
          H.grants() == {"SHARE_ALARMS"}, H.grants())
    check("...nor created coverage rows", _count(models.ServiceContractMachine) == 0)

    r = H.factory_accept(cid)
    check("the factory Admin accepts with the hash and the grant", r.status == 200, r)
    check("...status accepted, accepted by that admin",
          r.body.get("status") == "accepted"
          and r.body.get("factory_accepted_by") == "factory_a_admin", r)
    v = (r.body.get("versions") or [{}])[0]
    check("...version 1 accepted with BOTH hashes equal to the terms hash",
          v.get("status") == "accepted" and v.get("factory_accepted_hash") == h
          and v.get("oem_accepted_hash") == h, v)
    check("the grant WIDENED the policy: alarms kept, downtime added",
          H.grants() == {"SHARE_ALARMS", "SHARE_DOWNTIME"}, H.grants())
    with H.unscoped() as db:
        rows = (db.query(models.ServiceContractMachine)
                  .order_by(models.ServiceContractMachine.installation_id).all())
        snap = [(x.installation_id, x.machine_id_at_acceptance,
                 x.factory_tenant_at_acceptance, x.serial_number) for x in rows]
    expected = [(S["inst"]["SN-A1"], S["machines"]["A1"], "FACTORY_A", "SN-A1"),
                (S["inst"]["SN-A2"], S["machines"]["A2"], "FACTORY_A", "SN-A2")]
    check("coverage is snapshotted: installation, machine and factory at acceptance",
          snap == expected, snap)
    rows = sorted(row[0] for row in H.audit_rows("contract_accepted") if row[3] == cid)
    check("acceptance is audited exactly once for each party",
          rows == ["FACTORY_A", SENTINEL_A], rows)
    sharing = [row for row in H.audit_rows("oem_sharing_changed")]
    check("the widened grant is audited in the factory's tenant",
          any(row[0] == "FACTORY_A" and "SHARE_DOWNTIME" in (row[4] or "") for row in sharing),
          sharing)
    with H.unscoped() as db:
        events = [(e.tenant_code, e.event_type) for e in db.query(models.EventLog).all()]
    check("a ContractAccepted event is filed under the factory",
          ("FACTORY_A", "ContractAccepted") in events, events)
    notes = [n for n in H.notifications() if n[1] == "contract_accepted"]
    check("both parties are notified",
          sorted(n[0] for n in notes) == ["FACTORY_A", SENTINEL_A], notes)

    check("accepting twice is refused", H.factory_accept(cid, terms_hash=h).status == 409)
    w = POST(f"/oem/contracts/{cid}/withdraw", TOKENS["alpha"], {})
    check("the OEM cannot withdraw a contract the factory has accepted", w.status == 409, w)
    check("...it is still accepted",
          H.contract_detail(cid).body.get("status") == "accepted")
    S["accepted"] = cid


def case_an_unlinked_machine_cannot_be_covered():
    section("5. A MACHINE NOT LINKED TO THE SHOP FLOOR CANNOT BE COVERED")
    r = H.draft(serials=("SN-A3",))
    cid = r.body["id"]
    check("a draft may name the unlinked machine", r.status == 200, r)
    check("...and propose it", H.propose(cid).status == 200)
    before = H.grants()
    a = H.factory_accept(cid)
    check("accepting coverage of an unlinked machine is refused with 409",
          a.status == 409 and "SN-A3" in str(a.body), a)
    check("...the contract stays proposed",
          H.contract_detail(cid).body.get("status") == "proposed")
    check("...no coverage rows were written",
          _count(models.ServiceContractMachine) == 2)
    check("...and consent was not touched", H.grants() == before)

    # Bad data: the installation claims a machine that belongs to ANOTHER
    # factory. The link is not this factory's machine, so it covers nothing.
    with H.unscoped() as db:
        inst = db.query(models.MachineInstallation).filter_by(id=S["inst"]["SN-A3"]).one()
        inst.machine_id = S["machines"]["AB"]
        db.commit()
    try:
        r = H.draft(serials=("SN-A3",), start_offset=-3)
        H.propose(r.body["id"])
        a = H.factory_accept(r.body["id"])
        check("an installation pointing at another factory's machine is not linked (409)",
              a.status == 409 and "SN-A3" in str(a.body), a)
        check("...and no coverage rows were written",
              _count(models.ServiceContractMachine) == 2)
    finally:
        with H.unscoped() as db:
            inst = db.query(models.MachineInstallation).filter_by(id=S["inst"]["SN-A3"]).one()
            inst.machine_id = None
            db.commit()


def case_one_machine_one_contract_at_a_time():
    section("6. ONE MACHINE, ONE CONTRACT AT A TIME")
    r = H.draft(serials=("SN-A1",))
    cid = r.body["id"]
    H.propose(cid)
    a = H.factory_accept(cid)
    check("a second contract covering SN-A1 over the same months is refused",
          a.status == 409 and "SN-A1" in str(a.body), a)
    check("...and stays proposed", H.contract_detail(cid).body.get("status") == "proposed")

    later = H.draft(serials=("SN-A1",), start_offset=9)
    lid = later.body["id"]
    H.propose(lid)
    a = H.factory_accept(lid)
    check("CONTROL: a contract for SN-A1 starting when the first one ends is accepted",
          a.status == 200, a)

    # The other manufacturer's machine at the same factory is a separate question.
    b = H.draft(tok=TOKENS["beta"], serials=("SN-B1",))
    bid = b.body["id"]
    POST(f"/oem/contracts/{bid}/propose", TOKENS["beta"],
         {"terms_hash": H.version_hash(bid, tok=TOKENS["beta"])})
    a = H.factory_accept(bid)
    check("another manufacturer's contract on its own machine is unaffected",
          a.status == 200, a)
    check("...and that factory now shares downtime with BOTH, separately",
          H.grants("OEM_BETA", "FACTORY_A") == {"SHARE_DOWNTIME"},
          H.grants("OEM_BETA", "FACTORY_A"))


def case_rejection_and_withdrawal():
    section("7. REJECTION AND WITHDRAWAL ARE DECIDED BY ROW COUNT")
    r = H.draft(serials=("SN-A2",), start_offset=20)
    cid = r.body["id"]
    H.propose(cid)
    check("a Supervisor cannot reject",
          POST(f"/service-contracts/{cid}/reject", TOKENS["fa_super"], {"note": "no"}).status == 403)
    for bad in ("no\x00",):
        status = H.status_of(lambda: POST(f"/service-contracts/{cid}/reject", TOKENS["fa"],
                                          {"note": bad}))
        check(f"a rejection note carrying {bad[-1]!r} is refused with 422", status == 422, status)
    check("...and the contract is still proposed",
          H.contract_detail(cid).body.get("status") == "proposed")
    j = POST(f"/service-contracts/{cid}/reject", TOKENS["fa"], {"note": "price too high"})
    check("the factory Admin rejects with a note",
          j.status == 200 and j.body.get("status") == "rejected", j)
    check("...the version records the decision note",
          j.body["versions"][0].get("status") == "rejected"
          and j.body["versions"][0].get("decision_note") == "price too high", j)
    check("the OEM sees the rejection",
          H.contract_detail(cid).body.get("status") == "rejected")
    check("a rejected contract cannot then be accepted",
          H.factory_accept(cid).status == 409)
    check("...nor rejected twice",
          POST(f"/service-contracts/{cid}/reject", TOKENS["fa"], {"note": "x"}).status == 409)
    rows = sorted(row[0] for row in H.audit_rows("contract_rejected") if row[3] == cid)
    check("rejection is audited for both parties", rows == ["FACTORY_A", SENTINEL_A], rows)
    long = POST(f"/service-contracts/{cid}/reject", TOKENS["fa"], {"note": "x" * 1001})
    check("a note over 1000 characters is refused", long.status == 422, long)

    r = H.draft(serials=("SN-A2",), start_offset=40)
    wid = r.body["id"]
    H.propose(wid)
    check("a service manager cannot withdraw (sign_contracts)",
          POST(f"/oem/contracts/{wid}/withdraw", TOKENS["alpha_mgr"], {}).status == 403)
    w = POST(f"/oem/contracts/{wid}/withdraw", TOKENS["alpha"], {})
    check("the OEM withdraws a proposal", w.status == 200 and w.body.get("status") == "withdrawn", w)
    check("the factory can no longer accept it", H.factory_accept(wid).status == 409)
    f = GET(f"/service-contracts/{wid}", TOKENS["fa"])
    check("the factory still sees the withdrawn proposal it was shown",
          f.status == 200 and f.body.get("status") == "withdrawn", f)
    rows = sorted(row[0] for row in H.audit_rows("contract_withdrawn") if row[3] == wid)
    check("withdrawing a PROPOSAL is audited for both parties",
          rows == ["FACTORY_A", SENTINEL_A], rows)


def case_termination_lands_on_a_boundary():
    section("8. TERMINATION TAKES EFFECT ON A PERIOD BOUNDARY AFTER NOTICE")
    cid = H.active_contract(serials=("SN-A2",), start_offset=60)  # far from others
    # Terms start in the future: pin the clock inside the contract instead.
    ps = H.periods(cid)
    inside = H.parse_ts(ps[2]["start"]) + timedelta(days=3, hours=5)
    notice = timedelta(days=30)
    expected = next(p["start"] for p in ps if H.parse_ts(p["start"]) >= inside + notice)
    with H.clock(inside):
        check("a Supervisor cannot terminate",
              POST(f"/service-contracts/{cid}/terminate", TOKENS["fa_super"],
                   {"reason": "x"}).status == 403)
        check("a service manager cannot terminate",
              POST(f"/oem/contracts/{cid}/terminate", TOKENS["alpha_mgr"],
                   {"reason": "x"}).status == 403)
        blank = POST(f"/service-contracts/{cid}/terminate", TOKENS["fa"], {"reason": "   "})
        check("a termination whose reason is only whitespace is refused with 422",
              blank.status == 422, blank)
        for bad in ("moving\x00",):
            status = H.status_of(lambda: POST(f"/service-contracts/{cid}/terminate",
                                              TOKENS["fa"], {"reason": bad}))
            check(f"a termination reason carrying {bad[-1]!r} is refused with 422",
                  status == 422, status)
        check("...and the contract is not terminated",
              H.contract_detail(cid).body.get("status") == "accepted")
        t = POST(f"/service-contracts/{cid}/terminate", TOKENS["fa"],
                 {"reason": "moving the line"})
        check("the factory Admin terminates", t.status == 200, t)
        check("...effective at the first boundary on or after now + 30 days",
              t.body.get("termination_effective_at") == expected,
              f"{t.body.get('termination_effective_at')} != {expected}")
        check("...recorded as terminated by the factory, still active until then",
              t.body.get("status") == "terminated"
              and t.body.get("terminated_by_party") == "FACTORY"
              and t.body.get("state") == "active", t)
        again = POST(f"/oem/contracts/{cid}/terminate", TOKENS["alpha"], {"reason": "y"})
        check("a second termination is refused", again.status == 409, again)
        after = H.periods(cid)
        check("the contract's periods now stop at the termination",
              after and after[-1]["end"] == expected, after[-3:])
    rows = sorted(row[0] for row in H.audit_rows("contract_terminated") if row[3] == cid)
    check("termination is audited for both parties", rows == ["FACTORY_A", SENTINEL_A], rows)

    late = H.active_contract(serials=("SN-A2",), start_offset=80)
    lp = H.periods(late)
    near_end = H.parse_ts(lp[-1]["start"]) + timedelta(days=5)
    with H.clock(near_end):
        t = POST(f"/oem/contracts/{late}/terminate", TOKENS["alpha"], {"reason": "x"})
        check("termination whose notice runs past the contract's end is refused",
              t.status == 409, t)
    draft = H.draft(serials=("SN-A2",), start_offset=100)
    t = POST(f"/oem/contracts/{draft.body['id']}/terminate", TOKENS["alpha"], {"reason": "x"})
    check("a contract that was never accepted cannot be terminated", t.status == 409, t)


def case_every_transition_is_atomic_with_its_audit():
    section("9. A FAILED COMMIT LEAVES NEITHER THE TRANSITION NOR ITS AUDIT")
    r = H.draft(serials=("SN-A2",), start_offset=120)
    cid = r.body["id"]
    H.propose(cid)
    original = Session.commit

    def failing(self):
        pending = list(self.new) + list(self.identity_map.values())
        if any(isinstance(o, models.AuditLog) and o.action == "contract_accepted"
               for o in pending):
            raise RuntimeError("injected commit failure")
        return original(self)

    Session.commit = failing
    try:
        try:
            resp = H.factory_accept(cid)
            status = resp.status
        except RuntimeError:
            status = 500
    finally:
        Session.commit = original
    check("the injected failure surfaces as an error", status == 500, status)
    check("...the contract is still proposed",
          H.contract_detail(cid).body.get("status") == "proposed")
    check("...no acceptance audit row survived",
          not [row for row in H.audit_rows("contract_accepted") if row[3] == cid])
    check("...no coverage rows survived",
          _count(models.ServiceContractMachine,
                 installation_id=S["inst"]["SN-A2"]) == _count_a2_before[0])
    check("CONTROL: the same acceptance then succeeds", H.factory_accept(cid).status == 200)


_count_a2_before = [0]


def case_concurrent_acceptances():
    section("10. TWO ACCEPTANCES OF ONE MACHINE AT ONCE: EXACTLY ONE WINS")
    rounds = 5 if H.ON_POSTGRES else 1
    for n in range(rounds):
        offset = 200 + n * 13
        ids = []
        for _ in range(2):
            r = H.draft(serials=("SN-A1",), start_offset=offset)
            H.propose(r.body["id"])
            ids.append(r.body["id"])
        results = {}
        if H.ON_POSTGRES:
            barrier = threading.Barrier(2)

            def go(cid):
                barrier.wait()
                results[cid] = H.factory_accept(cid).status

            threads = [threading.Thread(target=go, args=(cid,)) for cid in ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for cid in ids:
                results[cid] = H.factory_accept(cid).status
        statuses = sorted(results.values())
        check(f"round {n + 1}: exactly one acceptance wins ({'threads on PostgreSQL' if H.ON_POSTGRES else 'sequential on SQLite'})",
              statuses == [200, 409], statuses)

    r = H.draft(serials=("SN-A1",), start_offset=300)
    cid = r.body["id"]
    H.propose(cid)
    h = H.version_hash(cid)
    out = {}
    if H.ON_POSTGRES:
        barrier = threading.Barrier(2)

        def accept():
            barrier.wait()
            out["accept"] = H.factory_accept(cid, terms_hash=h).status

        def withdraw():
            barrier.wait()
            out["withdraw"] = POST(f"/oem/contracts/{cid}/withdraw", TOKENS["alpha"], {}).status

        threads = [threading.Thread(target=accept), threading.Thread(target=withdraw)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        out["withdraw"] = POST(f"/oem/contracts/{cid}/withdraw", TOKENS["alpha"], {}).status
        out["accept"] = H.factory_accept(cid, terms_hash=h).status
    check("accept racing withdraw: exactly one of them wins",
          sorted(out.values()) == [200, 409], out)
    final = H.contract_detail(cid).body.get("status")
    check("...and the stored status is the winner's",
          (final == "accepted") == (out["accept"] == 200), f"{final} {out}")


def run_all():
    H.boot()
    H.seed()
    case_a_draft_is_the_manufacturers_alone()
    case_a_contract_names_only_the_manufacturers_equipment_at_that_factory()
    case_proposing_binds_the_oem_to_the_hash_it_saw()
    case_the_factory_accepts_explicitly()
    case_an_unlinked_machine_cannot_be_covered()
    case_one_machine_one_contract_at_a_time()
    case_rejection_and_withdrawal()
    case_termination_lands_on_a_boundary()
    _count_a2_before[0] = _count(models.ServiceContractMachine,
                                 installation_id=S["inst"]["SN-A2"])
    case_every_transition_is_atomic_with_its_audit()
    case_concurrent_acceptances()


def test_contract_lifecycle():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A CONTRACT BINDS ONLY WHAT BOTH PARTIES SAW, AND ONLY WHEN THE FACTORY SAYS SO")
