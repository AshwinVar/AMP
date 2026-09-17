"""Computing and accepting a downtime attribution statement (ADR-0020).

THE PROPERTIES UNDER TEST
-------------------------
  * A statement exists only for a whole, CLOSED period of an accepted contract.
  * Computing is idempotent: the same evidence gives the same hash, no new
    revision and no new audit row. An OEM-triggered compute reads the factory's
    evidence exactly as a factory-triggered one does.
  * An acceptance names the exact (content_hash, revision) the party saw. A
    stale value is refused with 409 and the CURRENT values, and nothing is
    inserted. Evidence that changed since the party looked is a stale value.
  * Open disputes block acceptance.
  * Both valid acceptances make the statement agreed; an agreed statement is
    frozen.
  * Only a signing role accepts: OEM_ADMIN on one side, the factory Admin on the
    other.
  * Once evidence has passed retention, acceptance still works against the
    stored revision (C11).
  * The download is the exact bytes whose SHA-256 is the content hash.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_acceptance.py
"""
import hashlib
from datetime import timedelta

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS, check, section

SENTINEL_A = "OEM:OEM_ALPHA"


def _statement(cid, sid, tok=None, oem=True):
    base = "/oem/contracts" if oem else "/service-contracts"
    return GET(f"{base}/{cid}/statements/{sid}", tok or (TOKENS["alpha"] if oem else TOKENS["fa"]))


def _acceptance_count(sid):
    with H.unscoped() as db:
        return (db.query(models.ContractStatementAcceptance)
                  .filter_by(statement_id=sid).count())


def case_computing_is_bounded_and_idempotent():
    section("1. A STATEMENT IS COMPUTED FOR A CLOSED PERIOD, IDEMPOTENTLY")
    cid = H.active_contract(serials=("SN-A1",), start_offset=-4)
    S["cid"] = cid
    ps = H.periods(cid)
    S["ps"] = ps
    (p0s, _), (p1s, _), gap_s, gap_e = H.measured_periods(cid)
    S["gap"] = (gap_s, gap_e)

    current = next(p for p in ps if H.parse_ts(p["start"]) <= H.now_utc() < H.parse_ts(p["end"]))
    r = H.compute(cid, current["start"])
    check("the current, unclosed period cannot be computed (409)", r.status == 409, r)
    r = H.compute(cid, (H.parse_ts(ps[0]["start"]) + timedelta(hours=1))
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
    check("a period_start that is not a period of the contract is refused", r.status == 422, r)
    check("an OEM viewer cannot compute",
          H.compute(cid, ps[0]["start"], tok=TOKENS["alpha_view"]).status == 403)
    check("an OEM service engineer cannot compute",
          H.compute(cid, ps[0]["start"], tok=TOKENS["alpha_eng"]).status == 403)
    check("a factory Supervisor cannot compute",
          H.compute(cid, ps[0]["start"], tok=TOKENS["fa_super"], oem=False).status == 403)

    audits_before = len(H.audit_rows("contract_statement_computed"))
    r = H.compute(cid, ps[1]["start"], tok=TOKENS["alpha_mgr"])
    check("an OEM service manager computes the second period", r.status == 200, r)
    st = r.body.get("statement", {})
    check("...revision 1, changed", st.get("revision") == 1 and r.body.get("changed") is True, r)
    body = _statement(cid, st.get("id"), oem=False).body
    totals = (body.get("content") or {}).get("totals", {})
    check("...and the OEM-triggered compute READ the factory's evidence (time is AVAILABLE,"
          " not all UNMEASURED)", totals.get("available_seconds", 0) > 0, totals)
    again = H.compute(cid, ps[1]["start"], oem=False)
    check("the factory recomputing the same evidence changes nothing",
          again.status == 200 and again.body.get("changed") is False
          and again.body["statement"]["content_hash"] == st.get("content_hash")
          and again.body["statement"]["revision"] == 1, again)
    rows = H.audit_rows("contract_statement_computed")
    check("the first compute was audited once per party, the no-op not at all",
          len(rows) - audits_before == 2
          and sorted(x[0] for x in rows[audits_before:]) == ["FACTORY_A", SENTINEL_A], rows)
    check("...naming the OEM's service manager the way every contract audit row does",
          {x[5] for x in rows[audits_before:]} == {"oem:OEM_ALPHA:alpha_mgr"},
          [x[5] for x in rows[audits_before:]])
    notes = [n[0] for n in H.notifications() if n[1] == "statement_computed"]
    check("both parties are told a statement was computed",
          sorted(notes) == ["FACTORY_A", SENTINEL_A], notes)

    r = H.compute(cid, ps[0]["start"], oem=False)
    check("the factory computes the first period", r.status == 200, r)
    S["s0"] = r.body["statement"]


def case_an_acceptance_names_what_the_party_saw():
    section("2. AN ACCEPTANCE NAMES THE EXACT HASH AND REVISION THE PARTY SAW")
    cid, s0 = S["cid"], S["s0"]
    sid, h, rev = s0["id"], s0["content_hash"], s0["revision"]

    for tok, label in ((TOKENS["alpha_view"], "an OEM viewer"),
                       (TOKENS["alpha_mgr"], "an OEM service manager"),
                       (TOKENS["alpha_eng"], "an OEM service engineer")):
        check(f"{label} cannot accept", H.accept_statement(cid, sid, h, rev, tok=tok).status == 403)
    check("a factory Supervisor cannot accept",
          H.accept_statement(cid, sid, h, rev, tok=TOKENS["fa_super"], oem=False).status == 403)

    r = H.accept_statement(cid, sid, "0" * 64, rev)
    check("a stale hash is refused with 409", r.status == 409, r)
    detail = r.body.get("detail") if isinstance(r.body.get("detail"), dict) else {}
    check("...naming the current hash and revision",
          detail.get("content_hash") == h and detail.get("revision") == rev, r)
    r = H.accept_statement(cid, sid, h, rev + 1)
    check("a revision that is not the current one is refused with 409", r.status == 409, r)
    r = POST(f"/oem/contracts/{cid}/statements/{sid}/accept", TOKENS["alpha"],
             {"content_hash": h, "revision": "1"})
    check("a revision sent as a string is refused", r.status == 422, r)
    check("...none of the refusals inserted an acceptance", _acceptance_count(sid) == 0)

    gap_s, gap_e = S["gap"]
    H.add_span("FACTORY_A", S["machines"]["A1"], gap_s, gap_e)
    r = H.accept_statement(cid, sid, h, rev)
    check("evidence that arrived after the party looked makes its acceptance stale (409)",
          r.status == 409, r)
    detail = r.body.get("detail") if isinstance(r.body.get("detail"), dict) else {}
    check("...and the refusal carries the NEW revision and hash",
          detail.get("revision") == rev + 1 and detail.get("content_hash") not in (None, h), r)
    check("...still no acceptance row", _acceptance_count(sid) == 0)

    h2, rev2 = detail.get("content_hash"), detail.get("revision")
    r = H.accept_statement(cid, sid, h2, rev2)
    check("the OEM admin accepts the current revision", r.status == 200, r)
    body = _statement(cid, sid).body
    check("...a valid acceptance, but not yet agreed",
          body.get("agreed") is False
          and any(a["party"] == "OEM" and a["valid"] for a in body.get("acceptances", [])), body)
    dup = H.accept_statement(cid, sid, h2, rev2)
    check("accepting the same revision twice is refused with 409", dup.status == 409, dup)
    check("...and inserted nothing", _acceptance_count(sid) == 1)

    f = H.accept_statement(cid, sid, h2, rev2, oem=False)
    check("the factory Admin accepts the same revision", f.status == 200, f)
    body = _statement(cid, sid, oem=False).body
    check("...and the statement is agreed", body.get("agreed") is True, body)
    rows = sorted(x[0] for x in H.audit_rows("contract_statement_accepted") if x[3] == sid)
    check("each acceptance is audited once per party (4 rows, 2 per tenant)",
          rows == ["FACTORY_A", "FACTORY_A", SENTINEL_A, SENTINEL_A], rows)
    audit_details = " ".join(x[4] or "" for x in H.audit_rows("contract_statement_accepted"))
    check("...naming the hash and revision accepted",
          h2 in audit_details and f"revision={rev2}" in audit_details, audit_details)
    notes = [n[0] for n in H.notifications() if n[1] == "statement_agreed"]
    check("both parties are told the statement is agreed",
          sorted(notes) == ["FACTORY_A", SENTINEL_A], notes)

    H.add_span("FACTORY_A", S["machines"]["A1"], H.parse_ts(S["ps"][0]["start"]) + timedelta(days=3),
               H.parse_ts(S["ps"][0]["start"]) + timedelta(days=3, hours=2), status="Breakdown")
    c = H.compute(cid, S["ps"][0]["start"])
    check("an agreed statement is frozen: a recompute changes nothing",
          c.status == 200 and c.body.get("frozen") is True
          and c.body["statement"]["content_hash"] == h2
          and c.body["statement"]["revision"] == rev2, c)
    check("accepting a frozen statement again is refused",
          H.accept_statement(cid, sid, h2, rev2, oem=False).status == 409)

    v = GET(f"/oem/contracts/{cid}/statements/{sid}/verify", TOKENS["alpha"])
    check("verify reports the stored bytes consistent with the stored hash",
          v.status == 200 and v.body.get("consistency") == "consistent"
          and v.body.get("stored_hash") == h2, v)
    d = GET(f"/oem/contracts/{cid}/statements/{sid}/download", TOKENS["alpha"])
    check("the download's bytes hash to the content hash",
          d.status == 200 and hashlib.sha256(d.raw).hexdigest() == h2, d.status)
    check("...served as JSON with the hash and revision in headers",
          d.headers.get("content-type", "").startswith("application/json")
          and d.headers.get("x-content-sha256") == h2
          and d.headers.get("x-statement-revision") == str(rev2), d.headers)
    fd = GET(f"/service-contracts/{cid}/statements/{sid}/download", TOKENS["fa_super"])
    check("the factory downloads the identical bytes", fd.status == 200 and fd.raw == d.raw)


def case_open_disputes_block_acceptance():
    section("3. AN OPEN DISPUTE BLOCKS ACCEPTANCE")
    cid, ps = S["cid"], S["ps"]
    r = H.compute(cid, ps[1]["start"], oem=False)
    st = r.body["statement"]
    start = H.parse_ts(ps[1]["start"]) + timedelta(days=4)
    d = POST(f"/service-contracts/{cid}/statements/{st['id']}/disputes", TOKENS["fa"],
             {"installation_id": S["inst"]["SN-A1"],
              "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "window_end": (start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "reason": "planned stop, not a fault", "proposed_bucket": "FACTORY"})
    check("the factory raises a dispute", d.status == 200, d)
    cur = _statement(cid, st["id"]).body
    r = H.accept_statement(cid, st["id"], cur["content_hash"], cur["revision"])
    check("accepting the current revision with the dispute open is refused (409)",
          r.status == 409, r)
    check("...and nothing was inserted", _acceptance_count(st["id"]) == 0)


def case_expired_evidence_does_not_block_acceptance():
    section("4. PAST RETENTION, ACCEPTANCE USES THE STORED REVISION (C11)")
    cid, ps = S["cid"], S["ps"]
    r = H.compute(cid, ps[2]["start"], oem=False)
    check("the third period is computed while evidence is live", r.status == 200, r)
    st = r.body["statement"]
    late = H.parse_ts(ps[2]["end"]) + timedelta(days=500)
    with H.clock(late):
        c = H.compute(cid, ps[2]["start"])
        check("recomputing after retention is refused (409, evidence expired)",
              c.status == 409 and "expired" in str(c.body).lower(), c)
        a = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"])
        check("...but the OEM accepts the stored revision", a.status == 200, a)
        f = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"], oem=False)
        check("...and so does the factory", f.status == 200, f)
    check("...and the statement is agreed", _statement(cid, st["id"]).body.get("agreed") is True)


def case_rule_disputed_time_is_cleared_by_a_dispute():
    section("5. TIME THE RULES MADE DISPUTED BLOCKS ACCEPTANCE UNTIL A DISPUTE SETTLES IT (C12)")
    cid = H.active_contract(serials=("SN-A2",), start_offset=-4)
    ps = H.periods(cid)
    p0s, p0e = H.parse_ts(ps[0]["start"]), H.parse_ts(ps[0]["end"])
    off_s = p0s + timedelta(days=5)
    off_e = off_s + timedelta(hours=1)
    mid = S["machines"]["A2"]
    H.add_span("FACTORY_A", mid, p0s, off_s)
    # "Offline" defaults to DISPUTED in the terms: nobody's fault until agreed.
    H.add_span("FACTORY_A", mid, off_s, off_e, status="Offline")
    H.add_span("FACTORY_A", mid, off_e, p0e + timedelta(days=1))
    r = H.compute(cid, ps[0]["start"], oem=False)
    st = r.body["statement"]
    body = _statement(cid, st["id"], oem=False).body
    sla = (body.get("content") or {}).get("sla", {})
    check("an hour Offline makes the statement pending_disputes",
          sla.get("state") == "pending_disputes", sla)
    a = H.accept_statement(cid, st["id"], st["content_hash"], st["revision"])
    check("a pending_disputes statement cannot be accepted, though no dispute is open",
          a.status == 409 and "DISPUTED" in str(a.body), a)
    check("...and nothing was inserted", _acceptance_count(st["id"]) == 0)

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    d = POST(f"/service-contracts/{cid}/statements/{st['id']}/disputes", TOKENS["fa"],
             {"installation_id": S["inst"]["SN-A2"], "window_start": off_s.strftime(fmt),
              "window_end": off_e.strftime(fmt), "reason": "planned power shutdown",
              "proposed_bucket": "FACTORY"})
    check("the factory raises a dispute over exactly that hour", d.status == 200, d)
    did = d.body["dispute"]["id"]
    p = POST(f"/service-contracts/{cid}/disputes/{did}/propose-resolution", TOKENS["fa"],
             {"resolution_bucket": "FACTORY", "note": "our shutdown"})
    ok = POST(f"/oem/contracts/{cid}/disputes/{did}/accept-resolution", TOKENS["alpha"],
              {"resolution_bucket": "FACTORY"})
    check("the factory proposes FACTORY and the OEM accepts", p.status == 200
          and ok.status == 200, (p, ok))
    body = _statement(cid, st["id"], oem=False).body
    sla = body["content"]["sla"]
    check("the recomputed statement is no longer pending_disputes",
          sla.get("state") in ("met", "breached"), sla)
    a = H.accept_statement(cid, st["id"], body["content_hash"], body["revision"])
    f = H.accept_statement(cid, st["id"], body["content_hash"], body["revision"], oem=False)
    check("...and both parties can now accept it", a.status == 200 and f.status == 200
          and f.body.get("agreed") is True, (a, f))


def run_all():
    H.boot()
    H.seed()
    case_computing_is_bounded_and_idempotent()
    case_an_acceptance_names_what_the_party_saw()
    case_open_disputes_block_acceptance()
    case_expired_evidence_does_not_block_acceptance()
    case_rule_disputed_time_is_cleared_by_a_dispute()


def test_contract_acceptance():
    H.failures.clear()
    try:
        run_all()
    finally:
        H.uninstall()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A STATEMENT IS AGREED ONLY WHEN BOTH PARTIES ACCEPT THE SAME HASH AT THE SAME REVISION")
