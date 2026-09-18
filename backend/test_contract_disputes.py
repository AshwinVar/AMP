"""Disputing a downtime attribution statement (ADR-0021).

THE PROPERTIES UNDER TEST
-------------------------
  * Raising a dispute moves the statement to a new revision, which cancels every
    acceptance, and marks exactly the disputed window DISPUTED with the dispute
    named as its cause.
  * A dispute must settle something real: its window lies inside the statement
    period AND inside the covered hours AND before the machine's coverage ended
    (its installation unlinked, C8), on an installation the statement's terms
    cover, and it may not overlap another live or resolved dispute on the same
    machine. Its buckets are AVAILABLE, OEM, FACTORY or UNMEASURED, never
    DISPUTED: a resolution to "disputed" would settle nothing.
  * A resolution needs BOTH parties: the party that proposed it cannot accept
    it, and the acceptor names the bucket it saw. Once resolved, the recomputed
    statement attributes the window to the agreed bucket.
  * Only the raising party withdraws. Withdrawing restores the attribution
    under a NEW revision; an acceptance of the original bytes at the original
    revision stays cancelled, and re-accepting the new revision works (C1,
    through the real routes).
  * An agreed statement is final: it cannot be disputed.
  * Past retention a dispute cannot be raised (the statement cannot show it),
    but one can still be withdrawn.
  * Every step is audited for both parties, and dispute rows carry no
    usernames.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_disputes.py
"""
import json
from datetime import timedelta

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS, check, section

SENTINEL_A = "OEM:OEM_ALPHA"
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _t(dt):
    return dt.strftime(FMT)


def _base(oem):
    return "/oem/contracts" if oem else "/service-contracts"


def _statement(cid, sid, oem=False, tok=None):
    return GET(f"{_base(oem)}/{cid}/statements/{sid}",
               tok or (TOKENS["alpha"] if oem else TOKENS["fa"]))


def _dispute(cid, sid, serial, start, end, bucket="FACTORY", oem=False, tok=None,
             reason="planned stop, not a machine fault"):
    return POST(f"{_base(oem)}/{cid}/statements/{sid}/disputes",
                tok or (TOKENS["alpha"] if oem else TOKENS["fa"]),
                {"installation_id": S["inst"][serial] if isinstance(serial, str) else serial,
                 "window_start": _t(start), "window_end": _t(end),
                 "reason": reason, "proposed_bucket": bucket})


def _interval(body, serial, start, end):
    content = body.get("content") or {}
    for m in content.get("machines", []):
        if m.get("installation_id") == S["inst"][serial]:
            for iv in m.get("intervals", []):
                if iv.get("start") == _t(start) and iv.get("end") == _t(end):
                    return iv
    return None


def _dispute_count():
    with H.unscoped() as db:
        return db.query(models.ContractDispute).count()


def _audited_both(action, entity_id):
    rows = sorted(r[0] for r in H.audit_rows(action) if r[3] == entity_id)
    return rows == ["FACTORY_A", SENTINEL_A], rows


def case_raising_cancels_acceptances_and_marks_the_window():
    section("1. RAISING A DISPUTE CANCELS ACCEPTANCES AND MARKS THE WINDOW DISPUTED")
    cid = H.active_contract(serials=("SN-A1", "SN-A2"), start_offset=-4)
    S["cid"] = cid
    ps = H.periods(cid)
    S["ps"] = ps
    H.measured_periods(cid)
    c = H.compute(cid, ps[1]["start"], oem=False)
    check("the second period's statement is computed", c.status == 200, c)
    st = c.body["statement"]
    S["s1"] = st["id"]
    a = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"])
    check("CONTROL: the OEM accepts revision 1", a.status == 200, a)

    ws = H.parse_ts(ps[1]["start"]) + timedelta(days=4)
    we = ws + timedelta(hours=3)
    S["w1"] = (ws, we)
    d = _dispute(cid, st["id"], "SN-A1", ws, we)
    check("the factory Admin raises a dispute", d.status == 200, d)
    did = (d.body.get("dispute") or {}).get("id")
    S["d1"] = did
    new = d.body.get("statement") or {}
    check("...which moves the statement to a new revision with a new hash",
          new.get("revision") == st["revision"] + 1
          and new.get("content_hash") not in (None, st["content_hash"]), d)
    body = _statement(cid, st["id"], oem=True).body
    oem_acc = [x for x in body.get("acceptances", []) if x["party"] == "OEM"]
    check("...and the OEM's acceptance of revision 1 no longer counts",
          len(oem_acc) == 1 and oem_acc[0]["valid"] is False and body.get("agreed") is False,
          body.get("acceptances"))
    iv = _interval(body, "SN-A1", ws, we)
    check("exactly the disputed window is DISPUTED, caused by this dispute",
          iv is not None and iv["bucket"] == "DISPUTED"
          and iv["cause"] == f"open_dispute:#{did}", iv)
    check("...a dispute row starts open, raised by the factory, with its reason",
          d.body["dispute"]["status"] == "open"
          and d.body["dispute"]["raised_by_party"] == "FACTORY"
          and d.body["dispute"]["reason"] == "planned stop, not a machine fault", d)
    ok, rows = _audited_both("contract_dispute_raised", did)
    check("raising is audited once for each party", ok, rows)
    notes = sorted(n[0] for n in H.notifications() if n[1] == "dispute_raised")
    check("both parties are notified", notes == ["FACTORY_A", SENTINEL_A], notes)
    r = H.accept_statement(cid, st["id"], new["content_hash"], new["revision"], oem=False)
    check("the factory cannot accept the new revision while its dispute is open",
          r.status == 409, r)


def case_a_dispute_must_settle_something_real():
    section("2. A DISPUTE MUST BE INSIDE THE PERIOD, THE COVERED HOURS AND THE TERMS")
    cid, sid, ps = S["cid"], S["s1"], S["ps"]
    p1s, p1e = H.parse_ts(ps[1]["start"]), H.parse_ts(ps[1]["end"])
    ws, we = S["w1"]
    before_n = _dispute_count()
    before_rev = _statement(cid, sid).body.get("revision")

    def refused(label, status, resp):
        check(label, resp.status == status, resp)

    refused("a window starting before the period is refused", 422,
            _dispute(cid, sid, "SN-A1", p1s - timedelta(hours=1), p1s + timedelta(hours=1)))
    refused("a window ending after the period is refused", 422,
            _dispute(cid, sid, "SN-A1", p1e - timedelta(hours=1), p1e + timedelta(hours=1)))
    refused("an empty window is refused", 422, _dispute(cid, sid, "SN-A1", ws, ws))
    refused("a window overlapping another dispute on the same machine is refused", 409,
            _dispute(cid, sid, "SN-A1", we - timedelta(hours=1), we + timedelta(hours=1)))
    refused("DISPUTED is not a bucket a dispute can propose", 422,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), bucket="DISPUTED"))
    refused("an unknown bucket is refused", 422,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), bucket="NOBODY"))
    refused("an installation the terms do not cover is refused", 422,
            _dispute(cid, sid, "SN-A3", we, we + timedelta(hours=1)))
    refused("another manufacturer's installation is refused", 422,
            _dispute(cid, sid, "SN-B1", we, we + timedelta(hours=1)))
    refused("a reason over 1000 characters is refused", 422,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), reason="x" * 1001))
    refused("a blank reason is refused", 422,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), reason="   "))
    for bad in ("stop\x00",):
        status = H.status_of(lambda: _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1),
                                              reason=bad))
        check(f"a reason carrying {bad[-1]!r} is refused with 422", status == 422, status)
    r = POST(f"/service-contracts/{cid}/statements/{sid}/disputes", TOKENS["fa"],
             {"installation_id": S["inst"]["SN-A1"], "window_start": "2026-01-01 00:00:00",
              "window_end": _t(we), "reason": "x", "proposed_bucket": "OEM"})
    refused("a window that is not a UTC instant is refused", 422, r)
    refused("a factory Supervisor cannot raise a dispute", 403,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), tok=TOKENS["fa_super"]))
    refused("an OEM viewer cannot raise a dispute", 403,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), oem=True,
                     tok=TOKENS["alpha_view"]))
    refused("an OEM service engineer cannot raise a dispute", 403,
            _dispute(cid, sid, "SN-A1", we, we + timedelta(hours=1), oem=True,
                     tok=TOKENS["alpha_eng"]))
    check("none of the refusals created a dispute", _dispute_count() == before_n)
    check("...or revised the statement",
          _statement(cid, sid).body.get("revision") == before_rev)

    ok = _dispute(cid, sid, "SN-A2", ws, we, bucket="UNMEASURED", oem=True,
                  tok=TOKENS["alpha_mgr"], reason="our gateway was down")
    check("CONTROL: the same window on ANOTHER covered machine may be disputed "
          "(by an OEM service manager)", ok.status == 200, ok)
    S["d_a2"] = ok.body.get("dispute", {}).get("id")

    # Covered hours: a weekly-coverage contract on a different, earlier term.
    weekly = {"mode": "weekly", "windows": [{"days": [0, 1, 2, 3, 4],
                                             "start": "08:00", "end": "20:00"}]}
    wid = H.active_contract(serials=("SN-A1",), start_offset=-12, term_months=6,
                            coverage=weekly)
    wps = H.periods(wid)
    c = H.compute(wid, wps[0]["start"], oem=False)
    check("a weekly-coverage statement is computed", c.status == 200, c)
    wsid = c.body["statement"]["id"]
    p0 = H.parse_ts(wps[0]["start"])      # local midnight, Asia/Kolkata (UTC+5:30)
    # First local Wednesday of the period: 10:00-11:00 local is covered,
    # 02:00-03:00 local and the Sunday are not.
    wed = next(p0 + timedelta(days=k) for k in range(7)
               if (p0 + timedelta(days=k, hours=5, minutes=30)).weekday() == 2)
    covered = (wed + timedelta(hours=10), wed + timedelta(hours=11))
    night = (wed + timedelta(hours=2), wed + timedelta(hours=3))
    sunday = wed + timedelta(days=4)
    refused("a window in the night, outside the covered hours, is refused", 422,
            _dispute(wid, wsid, "SN-A1", *night))
    refused("a window on an uncovered Sunday is refused", 422,
            _dispute(wid, wsid, "SN-A1", sunday + timedelta(hours=10),
                     sunday + timedelta(hours=11)))
    refused("a window spanning two covered days across the uncovered night is refused",
            422, _dispute(wid, wsid, "SN-A1", covered[0], covered[0] + timedelta(days=1)))
    ok = _dispute(wid, wsid, "SN-A1", *covered)
    check("CONTROL: 10:00-11:00 local on the Wednesday is inside the covered hours",
          ok.status == 200, ok)


def case_a_resolution_needs_both_parties():
    section("3. A RESOLUTION NEEDS BOTH PARTIES, AND THE RECOMPUTE APPLIES IT")
    cid, sid, did = S["cid"], S["s1"], S["d1"]
    ws, we = S["w1"]
    base = f"/oem/contracts/{cid}/disputes/{did}"
    fbase = f"/service-contracts/{cid}/disputes/{did}"

    r = POST(f"{base}/propose-resolution", TOKENS["alpha"],
             {"resolution_bucket": "DISPUTED", "note": "x"})
    check("a resolution to DISPUTED is refused", r.status == 422, r)
    check("an OEM viewer cannot propose a resolution",
          POST(f"{base}/propose-resolution", TOKENS["alpha_view"],
               {"resolution_bucket": "FACTORY"}).status == 403)
    for bad in ("agreed\x00",):
        status = H.status_of(lambda: POST(f"{base}/propose-resolution", TOKENS["alpha_mgr"],
                                          {"resolution_bucket": "FACTORY", "note": bad}))
        check(f"a resolution note carrying {bad[-1]!r} is refused with 422",
              status == 422, status)
    with H.unscoped() as db:
        still = db.query(models.ContractDispute).filter_by(id=did).one().status
    check("...and the dispute is still open", still == "open", still)
    p = POST(f"{base}/propose-resolution", TOKENS["alpha_mgr"],
             {"resolution_bucket": "FACTORY", "note": "agreed: a planned stop"})
    check("an OEM service manager proposes FACTORY", p.status == 200
          and p.body["dispute"]["status"] == "resolution_proposed"
          and p.body["dispute"]["resolution_proposed_by_party"] == "OEM", p)
    body = _statement(cid, sid).body
    iv = _interval(body, "SN-A1", ws, we)
    check("...while awaiting acceptance the window is still DISPUTED",
          iv is not None and iv["bucket"] == "DISPUTED", iv)
    r = H.accept_statement(cid, sid, body["content_hash"], body["revision"], oem=False)
    check("...and a pending resolution still blocks acceptance", r.status == 409, r)

    check("the party that proposed the resolution cannot accept it",
          POST(f"{base}/accept-resolution", TOKENS["alpha"],
               {"resolution_bucket": "FACTORY"}).status == 403)
    check("an OEM service manager cannot accept a resolution (sign_contracts)",
          POST(f"{base}/accept-resolution", TOKENS["alpha_mgr"],
               {"resolution_bucket": "FACTORY"}).status == 403)
    check("a factory Supervisor cannot accept it",
          POST(f"{fbase}/accept-resolution", TOKENS["fa_super"],
               {"resolution_bucket": "FACTORY"}).status == 403)
    r = POST(f"{fbase}/accept-resolution", TOKENS["fa"], {"resolution_bucket": "OEM"})
    check("accepting a bucket that is not the one on offer is refused", r.status == 409, r)
    rev_before = _statement(cid, sid).body["revision"]
    a = POST(f"{fbase}/accept-resolution", TOKENS["fa"], {"resolution_bucket": "FACTORY"})
    check("the factory Admin accepts the resolution", a.status == 200
          and a.body["dispute"]["status"] == "resolved"
          and a.body["dispute"]["resolution_bucket"] == "FACTORY", a)
    body = _statement(cid, sid).body
    iv = _interval(body, "SN-A1", ws, we)
    check("the recomputed statement attributes the window to FACTORY, by agreement",
          iv is not None and iv["bucket"] == "FACTORY"
          and iv["cause"] == f"agreed_override:#{did}", iv)
    check("...at a new revision", body["revision"] > rev_before, body["revision"])
    for action in ("contract_dispute_resolution_proposed", "contract_dispute_resolved"):
        ok, rows = _audited_both(action, did)
        check(f"{action} is audited for both parties", ok, rows)
    notes = sorted(n[0] for n in H.notifications() if n[1] == "dispute_resolved")
    check("both parties are told it is resolved", notes == ["FACTORY_A", SENTINEL_A], notes)

    check("a resolved dispute cannot be withdrawn",
          POST(f"{fbase}/withdraw", TOKENS["fa"], {}).status == 409)
    check("...or re-proposed",
          POST(f"{base}/propose-resolution", TOKENS["alpha"],
               {"resolution_bucket": "OEM"}).status == 409)
    r = _dispute(cid, sid, "SN-A1", ws + timedelta(minutes=30), we + timedelta(hours=1))
    check("a new dispute overlapping a RESOLVED window is refused", r.status == 409, r)


def case_withdrawal_restores_under_a_new_revision():
    section("4. WITHDRAWING RESTORES THE ATTRIBUTION UNDER A NEW REVISION (C1)")
    cid, ps = S["cid"], S["ps"]
    c = H.compute(cid, ps[0]["start"], oem=False)
    st = c.body["statement"]
    sid, h1, r1 = st["id"], st["content_hash"], st["revision"]
    a = H.accept_statement(cid, sid, h1, r1)
    check("the OEM accepts the first period at revision 1", a.status == 200, a)

    ws = H.parse_ts(ps[0]["start"]) + timedelta(days=2)
    we = ws + timedelta(hours=2)
    d = _dispute(cid, sid, "SN-A1", ws, we, bucket="OEM", oem=True,
                 reason="compressor tripped; this was a machine fault")
    check("the OEM raises a dispute of its own", d.status == 200, d)
    did = d.body["dispute"]["id"]
    check("the factory cannot withdraw the OEM's dispute",
          POST(f"/service-contracts/{cid}/disputes/{did}/withdraw", TOKENS["fa"], {}).status
          == 403)
    check("an OEM viewer cannot withdraw it",
          POST(f"/oem/contracts/{cid}/disputes/{did}/withdraw", TOKENS["alpha_view"],
               {}).status == 403)
    w = POST(f"/oem/contracts/{cid}/disputes/{did}/withdraw", TOKENS["alpha_mgr"], {})
    check("an OEM service manager withdraws it", w.status == 200
          and w.body["dispute"]["status"] == "withdrawn", w)
    new = w.body.get("statement") or {}
    check("the content returns to the ORIGINAL bytes (same hash)...",
          new.get("content_hash") == h1, new)
    check("...under a NEW revision (3), never the old one",
          new.get("revision") == r1 + 2, new)
    body = _statement(cid, sid, oem=True).body
    old = [x for x in body.get("acceptances", []) if x["party"] == "OEM"]
    check("the acceptance of revision 1 stays cancelled although the bytes match",
          len(old) == 1 and old[0]["valid"] is False and body.get("agreed") is False, old)
    r = H.accept_statement(cid, sid, h1, r1)
    check("accepting the original hash at revision 1 again is refused (409)",
          r.status == 409 and (r.body.get("detail") or {}).get("revision") == r1 + 2, r)
    r = H.accept_statement(cid, sid, h1, r1 + 2)
    check("re-accepting the same hash at revision 3 succeeds", r.status == 200, r)
    f = H.accept_statement(cid, sid, h1, r1 + 2, oem=False)
    check("...and with the factory's acceptance the statement is agreed",
          f.status == 200 and f.body.get("agreed") is True, f)
    ok, rows = _audited_both("contract_dispute_withdrawn", did)
    check("withdrawal is audited for both parties", ok, rows)
    check("a withdrawn dispute cannot be withdrawn again",
          POST(f"/oem/contracts/{cid}/disputes/{did}/withdraw", TOKENS["alpha"], {}).status
          == 409)
    r = _dispute(cid, sid, "SN-A1", ws + timedelta(hours=5), we + timedelta(hours=6))
    check("an AGREED statement cannot be disputed", r.status == 409, r)

    for tok, oem in ((TOKENS["alpha"], True), (TOKENS["fa_super"], False)):
        lst = GET(f"{_base(oem)}/{cid}/disputes", tok)
        text = json.dumps(lst.body)
        check(f"the {'OEM' if oem else 'factory Supervisor'} lists every dispute",
              lst.status == 200 and len(lst.body.get("disputes", [])) >= 3, lst)
        check("...and dispute rows carry no usernames",
              not any(u in text for u in ("factory_a_admin", "alpha_admin", "alpha_mgr")),
              text[:300])


def case_nothing_to_dispute_after_coverage_ends():
    section("4b. AFTER AN INSTALLATION IS UNLINKED THERE IS NO COVERED TIME TO DISPUTE (C8)")
    # FACTORY_B's own machine, so this case shares no installation with the rest.
    fb = TOKENS["fb"]
    cid = H.active_contract(serials=("SN-AB",), tenant="FACTORY_B", start_offset=-4,
                            fac_tok=fb)
    ps = H.periods(cid)
    c = H.compute(cid, ps[1]["start"], tok=fb, oem=False)
    check("FACTORY_B's statement for the second period is computed", c.status == 200, c)
    sid = c.body["statement"]["id"]
    stamp = H.parse_ts(ps[1]["start"]) + timedelta(days=10)
    # What the linkage listener records when the installation is re-pointed.
    with H.unscoped() as db:
        row = (db.query(models.ServiceContractMachine)
                 .filter_by(installation_id=S["inst"]["SN-AB"]).one())
        row.coverage_ended_at = stamp
        row.coverage_end_reason = "machine_id changed"
        db.commit()
    n = _dispute_count()

    def raised(start, end):
        return _dispute(cid, sid, "SN-AB", start, end, tok=fb)

    r = raised(stamp + timedelta(hours=1), stamp + timedelta(hours=2))
    check("a window after the machine's coverage ended is refused with 422",
          r.status == 422 and "coverage" in json.dumps(r.body).lower(), r)
    r = raised(stamp - timedelta(hours=1), stamp + timedelta(hours=1))
    check("a window straddling the coverage end is refused with 422", r.status == 422, r)
    check("...and neither created a dispute", _dispute_count() == n)
    r = raised(stamp - timedelta(hours=1), stamp)
    check("CONTROL: a window ending exactly at the coverage end is accepted",
          r.status == 200, r)


def case_expired_evidence():
    section("5. PAST RETENTION: NO NEW DISPUTE, BUT A LIVE ONE CAN BE WITHDRAWN")
    cid, ps = S["cid"], S["ps"]
    c = H.compute(cid, ps[2]["start"], oem=False)
    st = c.body["statement"]
    ws = H.parse_ts(ps[2]["start"]) + timedelta(days=1)
    d = _dispute(cid, st["id"], "SN-A1", ws, ws + timedelta(hours=1))
    check("a dispute is raised while the evidence is live", d.status == 200, d)
    did = d.body["dispute"]["id"]
    rev = d.body["statement"]["revision"]
    n = _dispute_count()
    late = H.parse_ts(ps[2]["end"]) + timedelta(days=500)
    with H.clock(late):
        r = _dispute(cid, st["id"], "SN-A1", ws + timedelta(hours=2), ws + timedelta(hours=3))
        check("raising a dispute once evidence has expired is refused (409)",
              r.status == 409 and "expired" in json.dumps(r.body).lower(), r)
        check("...and creates nothing", _dispute_count() == n)
        w = POST(f"/service-contracts/{cid}/disputes/{did}/withdraw", TOKENS["fa"], {})
        check("the live dispute can still be withdrawn", w.status == 200
              and w.body["dispute"]["status"] == "withdrawn"
              and w.body.get("statement") is None, w)
    check("...leaving the stored revision as it was (it could not be recomputed)",
          _statement(cid, st["id"]).body.get("revision") == rev)
    ok, rows = _audited_both("contract_dispute_withdrawn", did)
    check("...and the withdrawal is audited for both parties", ok, rows)


def run_all():
    H.boot()
    H.seed()
    case_raising_cancels_acceptances_and_marks_the_window()
    case_a_dispute_must_settle_something_real()
    case_a_resolution_needs_both_parties()
    case_withdrawal_restores_under_a_new_revision()
    case_nothing_to_dispute_after_coverage_ends()
    case_expired_evidence()


def test_contract_disputes():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A DISPUTE SETTLES A REAL WINDOW, NEEDS BOTH PARTIES, AND NEVER REVIVES AN ACCEPTANCE")
