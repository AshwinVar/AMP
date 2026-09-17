"""Changing a contract's terms needs both parties (ADR-0020).

THE PROPERTIES UNDER TEST
-------------------------
  * Either party may draft an amendment; a draft is its author's alone.
  * An amendment is proposed by its author and accepted by the OTHER party, each
    against the exact terms hash; a stale hash is refused.
  * It takes effect on a period boundary, never inside a period, and never
    before a period whose statement both parties have already agreed.
  * Only one amendment may be pending at a time.
  * The contract's clock (period length, timezone, term, currency) cannot be
    amended: a statement period must stay whole.
  * An amendment adding a machine re-runs the same coverage checks as the
    original acceptance: linked, at this factory, not covered elsewhere.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_amendments.py
"""
from datetime import timedelta

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS, check, section

SENTINEL_A = "OEM:OEM_ALPHA"


def _amend(cid, effective_from, serials=("SN-A1", "SN-A2"), tok=None, oem=True, **over):
    base = "/oem/contracts" if oem else "/service-contracts"
    return POST(f"{base}/{cid}/amendments", tok or (TOKENS["alpha"] if oem else TOKENS["fa"]),
                {"terms": H.terms(serials, **over), "effective_from": effective_from})


def _version(detail, v):
    return next((x for x in detail.body.get("versions", []) if x["version"] == v), None)


def case_an_oem_amendment_needs_the_factory():
    section("1. AN OEM AMENDMENT: DRAFTED, PROPOSED, ACCEPTED BY THE FACTORY")
    cid = H.active_contract(serials=("SN-A1",))
    S["cid"] = cid
    ps = H.periods(cid)
    S["ps"] = ps
    p1 = ps[1]["start"]

    bad = H.parse_ts(p1) + timedelta(hours=1)
    r = _amend(cid, bad.strftime("%Y-%m-%dT%H:%M:%SZ"))
    check("an amendment effective inside a period is refused", r.status == 422, r)
    before = (H.parse_ts(ps[0]["start"]) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("an amendment effective before the contract starts is refused",
          _amend(cid, before).status == 422)
    check("an amendment effective at (or after) the contract's end is refused",
          _amend(cid, ps[-1]["end"]).status == 422)
    # Far past the end, the boundary walk would run away; the answer must still
    # be the rule's 422, not a PeriodError surfacing as a 500.
    for far in ("2300-01-01T00:00:00Z", "9999-12-31T23:59:59Z"):
        status = H.status_of(lambda: _amend(cid, far))
        check(f"an amendment effective centuries after the end ({far}) is refused with 422",
              status == 422, status)
    for field, value in (("period_months", 3), ("timezone", "Europe/London"),
                         ("term_months", 24)):
        r = _amend(cid, p1, **{field: value})
        check(f"an amendment changing {field} is refused", r.status == 422, r)
    check("a viewer cannot draft an amendment",
          _amend(cid, p1, tok=TOKENS["alpha_view"]).status == 403)
    check("a service engineer cannot draft an amendment",
          _amend(cid, p1, tok=TOKENS["alpha_eng"]).status == 403)

    r = _amend(cid, p1, tok=TOKENS["alpha_mgr"])
    check("a service manager drafts an amendment adding SN-A2", r.status == 200, r)
    check("...as version 2, draft, authored by the OEM",
          r.body.get("version") == 2 and r.body.get("status") == "draft"
          and r.body.get("proposed_by_party") == "OEM", r)
    check("a second amendment while one is pending is refused",
          _amend(cid, ps[2]["start"]).status == 409)
    f = GET(f"/service-contracts/{cid}", TOKENS["fa"])
    check("the factory cannot see the OEM's draft amendment",
          f.status == 200 and _version(f, 2) is None, f)
    rows = [row[0] for row in H.audit_rows("contract_amendment_drafted") if row[3] == cid]
    check("drafting an amendment is audited on the author's side only",
          rows == [SENTINEL_A], rows)

    h2 = _version(H.contract_detail(cid), 2)["terms_hash"]
    check("a service manager cannot propose it (sign_contracts)",
          POST(f"/oem/contracts/{cid}/amendments/2/propose", TOKENS["alpha_mgr"],
               {"terms_hash": h2}).status == 403)
    check("proposing with a stale hash is refused",
          POST(f"/oem/contracts/{cid}/amendments/2/propose", TOKENS["alpha"],
               {"terms_hash": "a" * 64}).status == 409)
    check("the factory cannot propose the OEM's draft",
          POST(f"/service-contracts/{cid}/amendments/2/propose", TOKENS["fa"],
               {"terms_hash": h2}).status in (403, 404))
    p = POST(f"/oem/contracts/{cid}/amendments/2/propose", TOKENS["alpha"], {"terms_hash": h2})
    check("the OEM admin proposes it", p.status == 200 and p.body.get("status") == "proposed", p)
    check("...recording the OEM's hash", p.body.get("oem_accepted_hash") == h2, p)
    f = GET(f"/service-contracts/{cid}", TOKENS["fa"])
    check("the factory now sees version 2 and its terms",
          _version(f, 2) is not None and _version(f, 2)["terms_hash"] == h2, f)
    rows = sorted(row[0] for row in H.audit_rows("contract_amendment_proposed") if row[3] == cid)
    check("the proposal is audited for both parties", rows == ["FACTORY_A", SENTINEL_A], rows)
    notes = [n[0] for n in H.notifications() if n[1] == "amendment_proposed"]
    check("both parties are notified", sorted(notes) == ["FACTORY_A", SENTINEL_A], notes)

    check("the proposing party cannot accept its own amendment",
          POST(f"/oem/contracts/{cid}/amendments/2/accept", TOKENS["alpha"],
               {"terms_hash": h2}).status == 403)
    check("a factory Supervisor cannot accept it",
          POST(f"/service-contracts/{cid}/amendments/2/accept", TOKENS["fa_super"],
               {"terms_hash": h2}).status == 403)
    check("the factory cannot accept with a stale hash",
          POST(f"/service-contracts/{cid}/amendments/2/accept", TOKENS["fa"],
               {"terms_hash": "b" * 64}).status == 409)
    a = POST(f"/service-contracts/{cid}/amendments/2/accept", TOKENS["fa"], {"terms_hash": h2})
    check("the factory Admin accepts it", a.status == 200 and a.body.get("status") == "accepted", a)
    check("...with both hashes equal to the terms hash",
          a.body.get("oem_accepted_hash") == h2 and a.body.get("factory_accepted_hash") == h2, a)
    with H.unscoped() as db:
        v2 = (db.query(models.ServiceContractTermVersion)
                .filter_by(contract_id=cid, version=2).one())
        cover = sorted(x.serial_number for x in db.query(models.ServiceContractMachine)
                       .filter_by(term_version_id=v2.id).all())
    check("version 2's coverage is snapshotted (SN-A1, SN-A2)",
          cover == ["SN-A1", "SN-A2"], cover)
    rows = sorted(row[0] for row in H.audit_rows("contract_amendment_accepted") if row[3] == cid)
    check("acceptance is audited for both parties", rows == ["FACTORY_A", SENTINEL_A], rows)
    check("accepting it again is refused",
          POST(f"/service-contracts/{cid}/amendments/2/accept", TOKENS["fa"],
               {"terms_hash": h2}).status == 409)


def case_a_factory_amendment_needs_the_oem():
    section("2. A FACTORY AMENDMENT: THE OEM ACCEPTS")
    cid = S["cid"]
    ps = S["ps"]
    check("a factory Supervisor cannot draft an amendment",
          _amend(cid, ps[3]["start"], oem=False, tok=TOKENS["fa_super"]).status == 403)
    r = _amend(cid, ps[3]["start"], oem=False, sla_target_pct="98.00")
    check("the factory Admin drafts an amendment", r.status == 200, r)
    check("...as version 3 authored by the factory",
          r.body.get("version") == 3 and r.body.get("proposed_by_party") == "FACTORY", r)
    check("the OEM cannot see the factory's draft",
          _version(H.contract_detail(cid), 3) is None)
    h3 = r.body["terms_hash"]
    check("the OEM cannot propose the factory's draft",
          POST(f"/oem/contracts/{cid}/amendments/3/propose", TOKENS["alpha"],
               {"terms_hash": h3}).status in (403, 404))
    p = POST(f"/service-contracts/{cid}/amendments/3/propose", TOKENS["fa"], {"terms_hash": h3})
    check("the factory proposes it", p.status == 200 and p.body.get("factory_accepted_hash") == h3, p)
    check("the factory cannot accept its own amendment",
          POST(f"/service-contracts/{cid}/amendments/3/accept", TOKENS["fa"],
               {"terms_hash": h3}).status == 403)
    check("an OEM service manager cannot accept (sign_contracts)",
          POST(f"/oem/contracts/{cid}/amendments/3/accept", TOKENS["alpha_mgr"],
               {"terms_hash": h3}).status == 403)
    for bad in ("no\x00",):
        status = H.status_of(lambda: POST(f"/oem/contracts/{cid}/amendments/3/reject",
                                          TOKENS["alpha"], {"note": bad}))
        check(f"a rejection note carrying {bad[-1]!r} is refused with 422",
              status == 422, status)
    j = POST(f"/oem/contracts/{cid}/amendments/3/reject", TOKENS["alpha"], {"note": "no"})
    check("the OEM rejects it", j.status == 200 and j.body.get("status") == "rejected", j)
    rows = sorted(row[0] for row in H.audit_rows("contract_amendment_rejected") if row[3] == cid)
    check("rejection is audited for both parties", rows == ["FACTORY_A", SENTINEL_A], rows)
    check("a rejected amendment cannot be accepted",
          POST(f"/oem/contracts/{cid}/amendments/3/accept", TOKENS["alpha"],
               {"terms_hash": h3}).status == 409)

    r = _amend(cid, ps[3]["start"], oem=False, sla_target_pct="98.00")
    check("with nothing pending, a new amendment may be drafted", r.status == 200, r)
    w = POST(f"/service-contracts/{cid}/amendments/{r.body['version']}/reject", TOKENS["fa"],
             {"note": "changed our mind"})
    check("its author withdrawing it records 'withdrawn', not 'rejected'",
          w.status == 200 and w.body.get("status") == "withdrawn", w)

    # The OEM accepting the FACTORY's amendment runs the coverage checks from an
    # OEM request, whose ambient tenant is the OEM sentinel: the machine lookup
    # must still read the factory's floor, or every such acceptance fails.
    r = _amend(cid, ps[4]["start"], oem=False, sla_target_pct="98.00")
    v, h = r.body["version"], r.body["terms_hash"]
    p = POST(f"/service-contracts/{cid}/amendments/{v}/propose", TOKENS["fa"],
             {"terms_hash": h})
    a = POST(f"/oem/contracts/{cid}/amendments/{v}/accept", TOKENS["alpha"],
             {"terms_hash": h})
    check("the OEM admin accepts a factory-proposed amendment", p.status == 200
          and a.status == 200 and a.body.get("status") == "accepted"
          and a.body.get("oem_accepted_hash") == h == a.body.get("factory_accepted_hash"),
          (p, a))
    with H.unscoped() as db:
        vr = db.query(models.ServiceContractTermVersion).filter_by(contract_id=cid,
                                                                   version=v).one()
        snap = sorted((x.serial_number, x.machine_id_at_acceptance)
                      for x in db.query(models.ServiceContractMachine)
                      .filter_by(term_version_id=vr.id).all())
    check("...snapshotting the factory's machines as the OEM accepted them",
          snap == [("SN-A1", S["machines"]["A1"]), ("SN-A2", S["machines"]["A2"])], snap)


def case_an_agreed_statement_is_not_amended_away():
    section("3. NO AMENDMENT MAY PREDATE AN AGREED STATEMENT")
    cid = S["cid"]
    ps = S["ps"]
    H.measured_periods(cid)
    c = H.compute(cid, ps[0]["start"], oem=False)
    check("the first period's statement is computed", c.status == 200, c)
    st = c.body["statement"]
    a1 = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"])
    a2 = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"], oem=False)
    check("...and agreed by both parties", a1.status == 200 and a2.status == 200, (a1, a2))

    r = _amend(cid, ps[0]["start"])
    check("an amendment effective from the agreed period is refused", r.status == 422, r)
    r = _amend(cid, ps[1]["start"], sla_target_pct="95.00",
               credit_tiers=[{"below_pct": "95.00", "credit_pct": "5.00"},
                             {"below_pct": "90.00", "credit_pct": "10.00"}])
    check("CONTROL: from the next, unagreed period it is accepted as a draft", r.status == 200, r)
    v = r.body["version"]
    h = r.body["terms_hash"]
    POST(f"/oem/contracts/{cid}/amendments/{v}/propose", TOKENS["alpha"], {"terms_hash": h})

    # The period it would change becomes agreed while the amendment is pending.
    c = H.compute(cid, ps[1]["start"], oem=False)
    st = c.body["statement"]
    H.accept_statement(cid, st["id"], st["content_hash"], st["revision"])
    H.accept_statement(cid, st["id"], st["content_hash"], st["revision"], oem=False)
    a = POST(f"/service-contracts/{cid}/amendments/{v}/accept", TOKENS["fa"], {"terms_hash": h})
    check("accepting it after that period was agreed is refused", a.status == 409, a)
    check("...and it stays proposed",
          _version(H.contract_detail(cid), v)["status"] == "proposed")
    POST(f"/oem/contracts/{cid}/amendments/{v}/reject", TOKENS["alpha"], {"note": "stale"})


def case_an_added_machine_is_checked_like_the_original():
    section("4. A MACHINE ADDED BY AMENDMENT MEETS THE SAME COVERAGE CHECKS")
    other = H.active_contract(serials=("SN-A1",), start_offset=30)
    ps = H.periods(other)
    for serial, why in (("SN-A3", "unlinked"), ("SN-AB", "at another factory"),
                        ("SN-B1", "a competitor's")):
        r = _amend(other, ps[1]["start"], serials=("SN-A1", serial))
        if r.status == 200:
            v, h = r.body["version"], r.body["terms_hash"]
            POST(f"/oem/contracts/{other}/amendments/{v}/propose", TOKENS["alpha"],
                 {"terms_hash": h})
            a = POST(f"/service-contracts/{other}/amendments/{v}/accept", TOKENS["fa"],
                     {"terms_hash": h})
            check(f"adding {why} machine {serial} is refused at acceptance",
                  a.status == 409, a)
            POST(f"/oem/contracts/{other}/amendments/{v}/reject", TOKENS["alpha"],
                 {"note": "x"})
        else:
            check(f"adding {why} machine {serial} is refused at drafting",
                  r.status == 422, r)

    blocker = H.active_contract(serials=("SN-A2",), start_offset=30)
    r = _amend(other, ps[1]["start"], serials=("SN-A1", "SN-A2"))
    v, h = r.body["version"], r.body["terms_hash"]
    POST(f"/oem/contracts/{other}/amendments/{v}/propose", TOKENS["alpha"], {"terms_hash": h})
    a = POST(f"/service-contracts/{other}/amendments/{v}/accept", TOKENS["fa"],
             {"terms_hash": h})
    check("adding a machine another contract already covers is refused",
          a.status == 409 and "SN-A2" in str(a.body), (a, blocker))


def run_all():
    H.boot()
    H.seed()
    case_an_oem_amendment_needs_the_factory()
    case_a_factory_amendment_needs_the_oem()
    case_an_agreed_statement_is_not_amended_away()
    case_an_added_machine_is_checked_like_the_original()


def test_contract_amendments():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("TERMS CHANGE ONLY WHEN BOTH PARTIES ACCEPT THE SAME HASH, ON A BOUNDARY")
