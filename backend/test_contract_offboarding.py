"""Offboarding a factory closes its service contracts and removes its data from them (ADR-0021).

WHAT THIS PINS
--------------
The generic purge (offboard_tenant.purge_tenant_data) cannot see the contract
tables: they carry `factory_tenant_code`, never `tenant_code`, so a sweep cannot
delete a manufacturer's copy of a contract it signed. `_close_service_contracts`
owes the correct behaviour instead, before the sweep:

  * a contract still on offer (proposed) is withdrawn; a draft is the
    manufacturer's own unshared work and is left alone;
  * an accepted contract is terminated by the system at the first period
    boundary at or after now (never mid-period, never past its end), with the
    reason "factory offboarded";
  * each statement keeps its revision, content hash and every acceptance — the
    record of what both parties agreed — while its canonical content, its
    attribution records and its disputes (the factory's data and free text)
    are removed;
  * the contract, its term versions and its coverage rows stay;
  * a CONTROL contract of another factory is untouched;
  * the factory's sharing policy is purged with it, so the manufacturer's
    statement routes are withheld from then on (consent does not outlive the
    party that gave it); the contract, its terms and its periods stay visible;
  * the engine never recomputes over what was kept: an agreed statement stays
    frozen, an unagreed one is ContentPurged, verify reports content_purged —
    nothing is rebuilt from an empty database into "no data" over a kept hash;
  * one audit row per party records the closure.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_offboarding.py
"""
from datetime import timedelta

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS, check, section

FMT = "%Y-%m-%dT%H:%M:%SZ"


def case_offboarding_closes_contracts():
    section("1. A FACTORY LEAVES: ITS CONTRACTS CLOSE, ITS DATA GOES, THE AGREED HASHES STAY")
    # FACTORY_B: an accepted contract with an agreed statement, a second
    # statement with a dispute, and a contract still on offer.
    cid = H.active_contract(serials=("SN-AB",), tenant="FACTORY_B", fac_tok=TOKENS["fb"],
                            start_offset=-4)
    H.measured_periods(cid, machine_key="AB", tenant="FACTORY_B", gap_hours=2)
    ps = H.periods(cid)
    first = H.compute(cid, ps[0]["start"])
    second = H.compute(cid, ps[1]["start"])
    check("CONTROL: two closed periods computed", first.status == 200 and second.status == 200,
          f"{first} {second}")
    s0, s1 = first.body["statement"], second.body["statement"]
    a = H.accept_statement(cid, s0["id"], s0["content_hash"], s0["revision"])
    f = H.accept_statement(cid, s0["id"], s0["content_hash"], s0["revision"], tok=TOKENS["fb"],
                           oem=False)
    check("CONTROL: both parties accepted the first statement",
          a.status == 200 and f.status == 200 and f.body.get("agreed") is True, f"{a} {f}")
    start = H.parse_ts(ps[1]["start"]) + timedelta(days=1)
    d = POST(f"/service-contracts/{cid}/statements/{s1['id']}/disputes", TOKENS["fb"],
             {"installation_id": S["inst"]["SN-AB"], "window_start": start.strftime(FMT),
              "window_end": (start + timedelta(hours=1)).strftime(FMT),
              "reason": "SECRET-NOTE planned stop", "proposed_bucket": "FACTORY"})
    check("CONTROL: the factory raised a dispute on the second statement", d.status == 200, d)
    offer = H.draft(tenant="FACTORY_B", serials=("SN-AB",), start_offset=14)
    check("CONTROL: a second contract drafted", offer.status == 200, offer)
    H.propose(offer.body["id"])
    other = H.active_contract(serials=("SN-A1",))

    with H.unscoped() as db:
        kept_hash = db.get(models.ContractStatement, s0["id"]).content_hash
        kept_acceptances = sorted((x.party, x.content_hash, x.revision)
                                  for x in db.query(models.ContractStatementAcceptance)
                                  .filter(models.ContractStatementAcceptance.statement_id
                                          == s0["id"]).all())
        other_before = [(s.id, s.canonical_json) for s in db.query(models.ContractStatement)
                        .filter(models.ContractStatement.contract_id == other).all()]
    now = H.now_utc()
    import offboard_tenant
    with H.unscoped() as db:
        counts = offboard_tenant.purge_tenant_data(db, "FACTORY_B")
    check("the purge reports what it closed and removed",
          counts.get("service_contracts_closed") == 2
          and counts.get("contract_statement_contents_removed") == 2
          and counts.get("contract_disputes_removed") == 1
          and counts.get("contract_attribution_records_removed", 0) > 0, str(counts))

    with H.unscoped() as db:
        c = db.get(models.ServiceContract, cid)
        grid_boundary = None
        import contract_periods
        import contract_terms
        v1 = (db.query(models.ServiceContractTermVersion)
                .filter(models.ServiceContractTermVersion.contract_id == cid,
                        models.ServiceContractTermVersion.version == 1).one())
        grid_boundary = contract_periods.boundary_at_or_after(
            contract_terms.parse(v1.terms_json), c.starts_at, now)
        check("the accepted contract is terminated by the system at the next period boundary",
              c.status == "terminated" and c.termination_effective_at == min(grid_boundary, c.ends_at)
              and c.terminated_by_party == "SYSTEM" and c.termination_reason == "factory offboarded",
              f"{c.status} {c.termination_effective_at} {grid_boundary} {c.terminated_by_party} "
              f"{c.termination_reason}")
        o = db.get(models.ServiceContract, offer.body["id"])
        check("the contract still on offer is withdrawn", o.status == "withdrawn", o.status)
        st = db.get(models.ContractStatement, s0["id"])
        check("the agreed statement keeps its hash and revision; its content is removed",
              st.content_hash == kept_hash and st.revision == s0["revision"]
              and st.canonical_json is None, f"{st.content_hash} {st.canonical_json!r:.40}")
        check("... and every acceptance is kept",
              sorted((x.party, x.content_hash, x.revision)
                     for x in db.query(models.ContractStatementAcceptance)
                     .filter(models.ContractStatementAcceptance.statement_id == s0["id"]).all())
              == kept_acceptances and len(kept_acceptances) == 2)
        check("attribution records of the factory's statements are removed",
              db.query(models.ContractAttributionRecord)
              .filter(models.ContractAttributionRecord.statement_id.in_([s0["id"], s1["id"]]))
              .count() == 0)
        check("its disputes (the factory's own free text) are removed",
              db.query(models.ContractDispute)
              .filter(models.ContractDispute.contract_id == cid).count() == 0)
        check("the contract, its term versions and coverage rows stay",
              db.query(models.ServiceContractTermVersion)
              .filter(models.ServiceContractTermVersion.contract_id == cid).count() == 1
              and db.query(models.ServiceContractMachine)
              .filter(models.ServiceContractMachine.installation_id == S["inst"]["SN-AB"])
              .count() >= 1)
        check("CONTROL: FACTORY_A's contract and statements are untouched",
              db.get(models.ServiceContract, other).status == "accepted"
              and [(s.id, s.canonical_json) for s in db.query(models.ContractStatement)
                   .filter(models.ContractStatement.contract_id == other).all()] == other_before)
        check("no span of FACTORY_B is left",
              db.query(models.MachineTelemetrySpan)
              .filter(models.MachineTelemetrySpan.tenant_code == "FACTORY_B").count() == 0)
    rows = [r for r in H.audit_rows("contract_closed_at_offboarding") if r[3] == cid]
    check("the closure is audited for both parties",
          sorted(r[0] for r in rows) == ["FACTORY_B", "OEM:OEM_ALPHA"], str(rows))

    section("2. AFTERWARDS: CONSENT LEFT WITH THE FACTORY, AND NOTHING IS RECOMPUTED OVER THE KEPT HASH")
    check("the factory's sharing policy went with it", H.grants(tenant="FACTORY_B") == set())
    for path in (f"/oem/contracts/{cid}/statements/{s0['id']}",
                 f"/oem/contracts/{cid}/statements/{s0['id']}/verify",
                 f"/oem/contracts/{cid}/statements/{s0['id']}/download"):
        r = GET(path, TOKENS["alpha"])
        check(f"the OEM's {path.rsplit('/', 1)[-1]} is withheld (no SHARE_DOWNTIME any more)",
              r.status == 403 and "SECRET-NOTE" not in r.raw.decode("utf-8"), r)
    p = GET(f"/oem/contracts/{cid}/periods", TOKENS["alpha"])
    listed = {x["start"]: x["statement"] for x in p.body.get("periods", [])}
    check("the contract's periods still list both statements, the first agreed",
          p.status == 200 and listed[ps[0]["start"]]["agreed"] is True
          and listed[ps[1]["start"]]["agreed"] is False, p)
    c = GET(f"/oem/contracts/{cid}", TOKENS["alpha"])
    check("the OEM still sees the terminated contract and its terms",
          c.status == 200 and c.body["status"] == "terminated" and c.body["versions"], c)

    import contract_statements as cs
    import oem_sharing
    from types import SimpleNamespace
    party = SimpleNamespace(side="OEM", actor="oem:OEM_ALPHA:alpha_admin")
    later = H.now_utc() + timedelta(days=1)
    with H.unscoped() as db, oem_sharing.bound_factory_read("FACTORY_B"):
        contract = db.get(models.ServiceContract, cid)
        agreed = cs.compute_statement(db, contract, H.parse_ts(ps[0]["start"]), party=party,
                                      now=later)
        check("the engine leaves the agreed statement frozen at its kept hash",
              agreed.frozen is True and agreed.statement.content_hash == kept_hash)
        purged = None
        try:
            cs.compute_statement(db, contract, H.parse_ts(ps[1]["start"]), party=party, now=later)
        except cs.ContentPurged as e:
            purged = e
        check("the engine refuses to recompute the purged, unagreed statement (ContentPurged)",
              purged is not None)
        report = cs.verify_statement(db, db.get(models.ContractStatement, s0["id"]), later)
        check("verify reports content_purged", report["consistency"] == "content_purged",
              str(report["consistency"]))
        db.rollback()


def run_all():
    H.boot()
    H.seed()
    case_offboarding_closes_contracts()


def test_contract_offboarding():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("OFFBOARDING CLOSES THE CONTRACTS, REMOVES THE FACTORY'S DATA AND KEEPS WHAT WAS AGREED")
