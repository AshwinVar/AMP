"""Consent: what a manufacturer sees of a contract, and when (ADR-0020, ADR-0017).

THE PROPERTIES UNDER TEST
-------------------------
  * Statement content is the factory's data. An OEM reads it, verifies it,
    downloads it, computes it, accepts it or disputes it ONLY while the factory
    grants SHARE_DOWNTIME, read on every request: a withdrawal under Connected
    Equipment takes effect on the very next request, and restoring the grant
    restores access. Withheld means withheld: a 403 that says why and carries no
    number, hash or interval.
  * The CONTRACT is not the factory's data: terms, versions, periods, the
    dispute list and history stay visible to the OEM without the grant, and an
    OEM may still withdraw its own dispute. The statement rows of that history
    are listed with their details (content hashes) withheld until the grant is
    restored.
  * The factory's own access never depends on the grant.
  * Proposing a contract grants nothing; accepting it grants SHARE_DOWNTIME to
    THAT manufacturer only, creating a policy where none existed.
  * The reason vocabulary is the factory's alone: an OEM token cannot reach it,
    and it lists only this factory's reasons, for the covered machines, from the
    last 90 days, bounded at both ends.
  * Nothing an OEM can read about a contract carries the factory's operational
    secrets (a needle scan over every OEM read).
  * History is the caller's own audit trail: its tenant's rows, about this
    contract, since the contract was created; no totals in any audit detail.
    Past its cap it keeps the NEWEST rows, oldest-first, and says it truncated.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_consent.py
"""
from datetime import datetime, timedelta

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, PUT, S, TOKENS, check, section

SENTINEL_A = "OEM:OEM_ALPHA"
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _set_grants(grants, tok=None, oem="OEM_ALPHA"):
    r = PUT("/connected-equipment/sharing", tok or TOKENS["fa"],
            {"oem_code": oem, "grants": list(grants)})
    assert r.status == 200, r
    return r


def _withheld(resp):
    detail = resp.body.get("detail") if isinstance(resp.body, dict) else None
    return (resp.status == 403 and isinstance(detail, dict)
            and detail.get("withheld") is True
            and detail.get("reason") == "sharing withdrawn by factory")


def _no_statement_data(resp, st):
    text = resp.raw.decode("utf-8", "replace")
    return all(n not in text for n in (st["content_hash"], "intervals", "totals",
                                        "AVAILABLE", "UNMEASURED", "evidence"))


def _acceptances(sid):
    with H.unscoped() as db:
        return (db.query(models.ContractStatementAcceptance)
                  .filter_by(statement_id=sid).count())


def case_setup():
    section("0. A CONTRACT, A STATEMENT AND A DISPUTE, WITH DOWNTIME SHARED")
    before = H.grants()
    r = H.draft(serials=("SN-A1",), start_offset=-4)
    cid = r.body["id"]
    H.propose(cid)
    check("proposing a contract grants nothing",
          H.grants() == before == {"SHARE_ALARMS"}, H.grants())
    check("...and an OEM cannot compute on a contract the factory has not accepted",
          H.compute(cid, H.periods(cid)[0]["start"]).status in (403, 409))
    a = H.factory_accept(cid)
    check("the factory accepts, granting SHARE_DOWNTIME", a.status == 200
          and H.grants() == {"SHARE_ALARMS", "SHARE_DOWNTIME"}, (a, H.grants()))
    S["cid"] = cid
    ps = H.periods(cid)
    S["ps"] = ps
    H.measured_periods(cid)
    c = H.compute(cid, ps[1]["start"], oem=False)
    S["st"] = c.body["statement"]
    ws = H.parse_ts(ps[1]["start"]) + timedelta(days=3)
    d = POST(f"/oem/contracts/{cid}/statements/{S['st']['id']}/disputes", TOKENS["alpha"],
             {"installation_id": S["inst"]["SN-A1"], "window_start": ws.strftime(FMT),
              "window_end": (ws + timedelta(hours=1)).strftime(FMT),
              "reason": "fault", "proposed_bucket": "OEM"})
    check("CONTROL: with the grant the OEM raises a dispute", d.status == 200, d)
    S["did"] = d.body["dispute"]["id"]
    S["st"] = d.body["statement"]
    g = GET(f"/oem/contracts/{cid}/statements/{S['st']['id']}", TOKENS["alpha"])
    check("CONTROL: with the grant the OEM reads the statement content",
          g.status == 200 and g.body.get("content") is not None, g)


def case_withdrawal_withholds_on_the_next_request():
    section("1. WITHDRAWING SHARE_DOWNTIME WITHHOLDS STATEMENTS ON THE NEXT REQUEST")
    cid, st, ps, did = S["cid"], S["st"], S["ps"], S["did"]
    sid = st["id"]
    _set_grants(["SHARE_ALARMS"])
    base = f"/oem/contracts/{cid}"
    ws = H.parse_ts(ps[1]["start"]) + timedelta(days=9)
    attempts = [
        ("read the statement", GET(f"{base}/statements/{sid}", TOKENS["alpha"])),
        ("verify it", GET(f"{base}/statements/{sid}/verify", TOKENS["alpha"])),
        ("download it", GET(f"{base}/statements/{sid}/download", TOKENS["alpha"])),
        ("preview the running period", GET(f"{base}/preview", TOKENS["alpha"])),
        ("compute a statement", H.compute(cid, ps[0]["start"])),
        ("accept it", H.accept_statement(cid, sid, st["content_hash"], st["revision"])),
        ("raise a dispute", POST(f"{base}/statements/{sid}/disputes", TOKENS["alpha"],
                                 {"installation_id": S["inst"]["SN-A1"],
                                  "window_start": ws.strftime(FMT),
                                  "window_end": (ws + timedelta(hours=1)).strftime(FMT),
                                  "reason": "x", "proposed_bucket": "OEM"})),
        ("propose a resolution", POST(f"{base}/disputes/{did}/propose-resolution",
                                      TOKENS["alpha"], {"resolution_bucket": "OEM"})),
        ("accept a resolution", POST(f"{base}/disputes/{did}/accept-resolution",
                                     TOKENS["alpha"], {"resolution_bucket": "OEM"})),
    ]
    for label, resp in attempts:
        check(f"without the grant the OEM cannot {label}: withheld, and says why",
              _withheld(resp), resp)
        check("...with no statement data in the refusal", _no_statement_data(resp, st),
              resp.raw[:200])
    check("the refused acceptance inserted nothing", _acceptances(sid) == 0)
    with H.unscoped() as db:
        n = db.query(models.ContractStatement).filter_by(contract_id=cid).count()
    check("the refused compute created no statement", n == 1, n)

    for label, path in (("the contract and its terms", base),
                        ("the periods", f"{base}/periods"),
                        ("the dispute list", f"{base}/disputes"),
                        ("its history", f"{base}/history")):
        r = GET(path, TOKENS["alpha"])
        check(f"the OEM still sees {label}", r.status == 200, r)
    detail = GET(base, TOKENS["alpha"]).body
    check("...terms included, and the contract says downtime is not shared",
          detail["versions"][0]["terms"]["sla_target_pct"] == "97.00"
          and detail.get("downtime_shared") is False, detail.get("downtime_shared"))

    f = GET(f"/service-contracts/{cid}/statements/{sid}", TOKENS["fa"])
    check("the factory's own access does not depend on the grant",
          f.status == 200 and f.body.get("content") is not None, f)

    w = POST(f"{base}/disputes/{did}/withdraw", TOKENS["alpha"], {})
    check("the OEM may still withdraw its own dispute", w.status == 200
          and w.body["dispute"]["status"] == "withdrawn", w)
    check("...and that reply carries no statement content, not even the hash",
          _no_statement_data(w, S["st"])
          and w.body.get("statement", {}).get("content_hash") is None, w.raw[:300])
    with H.unscoped() as db:
        revised_hash = db.query(models.ContractStatement).filter_by(id=sid).one().content_hash
    check("CONTROL: the withdrawal really revised the statement to a new hash",
          revised_hash != st["content_hash"], revised_hash)
    oh = GET(f"{base}/history", TOKENS["alpha"])
    rows = [x for x in oh.body.get("history", []) if x["entity_type"] == "contract_statement"]
    check("...nor does the OEM's history carry it: the statement rows are listed, their "
          "details withheld",
          oh.status == 200 and rows and revised_hash not in oh.raw.decode("utf-8", "replace")
          and all(x["details"] is None and x.get("details_withheld") is True for x in rows),
          (oh.status, rows[:2]))
    other = [x for x in oh.body.get("history", []) if x["entity_type"] != "contract_statement"]
    check("...while the contract's own rows keep their details",
          other and all(x["details"] and x.get("details_withheld") is False for x in other),
          other[:2])
    fh = GET(f"/service-contracts/{cid}/history", TOKENS["fa"])
    check("CONTROL: the factory's own history carries the revised hash",
          revised_hash in fh.raw.decode("utf-8", "replace"))

    _set_grants(["SHARE_ALARMS", "SHARE_DOWNTIME"])
    g = GET(f"{base}/statements/{sid}", TOKENS["alpha"])
    check("restoring the grant restores access on the next request",
          g.status == 200 and g.body.get("content") is not None, g)
    oh = GET(f"{base}/history", TOKENS["alpha"])
    check("...and the statement details in the OEM's history",
          revised_hash in oh.raw.decode("utf-8", "replace")
          and not any(x.get("details_withheld") for x in oh.body.get("history", [])),
          oh.raw[:300])


def case_acceptance_grants_to_that_manufacturer_only():
    section("2. ACCEPTING GRANTS SHARE_DOWNTIME TO THAT MANUFACTURER, AND ONLY IT")
    check("FACTORY_B starts with an empty policy for ALPHA",
          H.grants("OEM_ALPHA", "FACTORY_B") == set())
    check("...and none at all for BETA", H.grants("OEM_BETA", "FACTORY_B") == set())
    cid = H.active_contract(serials=("SN-AB",), tenant="FACTORY_B",
                            fac_tok=TOKENS["fb"])
    check("FACTORY_B's acceptance grants exactly SHARE_DOWNTIME to ALPHA",
          H.grants("OEM_ALPHA", "FACTORY_B") == {"SHARE_DOWNTIME"},
          H.grants("OEM_ALPHA", "FACTORY_B"))
    check("...and nothing to BETA", H.grants("OEM_BETA", "FACTORY_B") == set())
    check("...and nothing changed between FACTORY_A and ALPHA",
          H.grants() == {"SHARE_ALARMS", "SHARE_DOWNTIME"}, H.grants())
    S["cid_b"] = cid


def case_the_reason_vocabulary_is_the_factorys_alone():
    section("3. THE REASON VOCABULARY IS THE FACTORY'S ALONE, AND BOUNDED")
    cid = S["cid"]
    now = datetime.utcnow()
    with H.unscoped() as db:
        for tenant, machine, reason, at in (
                ("FACTORY_A", S["machines"]["A1"], "Compressor tripped", now - timedelta(days=2)),
                ("FACTORY_A", S["machines"]["A1"], "Breakdown", now - timedelta(days=1)),
                ("FACTORY_A", S["machines"]["A1"], "  NO MATERIAL ", now - timedelta(days=3)),
                ("FACTORY_A", S["machines"]["A1"], "Ancient reason", now - timedelta(days=91)),
                ("FACTORY_A", S["machines"]["A1"], "Future reason", now + timedelta(days=2)),
                ("FACTORY_A", S["machines"]["A2"], "Uncovered machine reason",
                 now - timedelta(days=1)),
                ("FACTORY_B", S["machines"]["AB"], "Other factory reason",
                 now - timedelta(days=1))):
            db.add(models.DowntimeLog(tenant_code=tenant, machine_id=machine, reason=reason,
                                      duration="5 min", notes="SECRET-NOTE",
                                      created_at=at))
        db.commit()
    r = GET(f"/service-contracts/{cid}/reason-vocabulary", TOKENS["fa_super"])
    check("a factory Supervisor reads the reason vocabulary", r.status == 200, r)
    got = {x["reason_key"]: (x["status"], x["bucket"], x["count"])
           for x in r.body.get("reasons", [])}
    check("'No material' (logged twice, differently cased) is mapped to FACTORY",
          got.get("no material") == ("mapped", "FACTORY", 2), got)
    check("an unmapped reason is reported as unmapped, i.e. DISPUTED",
          got.get("compressor tripped") == ("unmapped", "DISPUTED", 1), got)
    check("MQTT's automatic 'Breakdown' is generic, explaining nothing",
          got.get("breakdown") == ("generic", None, 1), got)
    check("a reason older than 90 days is outside the window",
          "ancient reason" not in got, got)
    check("a reason dated in the future is outside the window",
          "future reason" not in got, got)
    check("a reason on a machine the terms do not cover is not listed",
          "uncovered machine reason" not in got, got)
    check("another factory's reason on its own machine is not listed",
          "other factory reason" not in got, got)
    check("an Operator cannot read it",
          GET(f"/service-contracts/{cid}/reason-vocabulary", TOKENS["fa_op"]).status == 403)
    check("an OEM token cannot read it through the factory route (403)",
          GET(f"/service-contracts/{cid}/reason-vocabulary", TOKENS["alpha"]).status == 403)
    o = GET(f"/oem/contracts/{cid}/reason-vocabulary", TOKENS["alpha"])
    check("...and there is no OEM route for it", o.status in (404, 405), o)
    check("another factory gets 404",
          GET(f"/service-contracts/{cid}/reason-vocabulary", TOKENS["fb"]).status == 404)


def case_nothing_an_oem_reads_carries_factory_secrets():
    section("4. NOTHING AN OEM CAN READ ABOUT A CONTRACT CARRIES FACTORY SECRETS")
    cid, sid = S["cid"], S["st"]["id"]
    base = f"/oem/contracts/{cid}"
    paths = ["/oem/contracts", base, f"{base}/periods", f"{base}/disputes",
             f"{base}/history", f"{base}/statements/{sid}",
             f"{base}/statements/{sid}/verify", f"{base}/statements/{sid}/download",
             f"{base}/preview"]
    for tok_name in ("alpha", "alpha_view"):
        for path in paths:
            r = GET(path, TOKENS[tok_name])
            text = r.raw.decode("utf-8", "replace")
            leaked = [n for n in H.NEEDLES if n in text]
            check(f"{tok_name} {path.replace(base, '<contract>')}: {r.status}, no needles",
                  r.status in (200, 404) and not leaked, (r.status, leaked))
    notes = []
    with H.unscoped() as db:
        notes = [n.message + n.title for n in db.query(models.Notification)
                 .filter(models.Notification.tenant_code == SENTINEL_A).all()]
    check("the OEM's notifications carry no needles and no figures",
          notes and not any(n in m for m in notes for n in H.NEEDLES + ("AVAILABLE",)),
          notes[:3])


def case_history_is_the_callers_own_trail():
    section("5. HISTORY IS THE CALLER'S OWN AUDIT TRAIL, BOUNDED")
    cid = S["cid"]
    with H.unscoped() as db:
        contract = db.query(models.ServiceContract).filter_by(id=cid).one()
        created = contract.created_at
        # Planted: the same entity id, but before the contract existed; another
        # tenant's row; and an unrelated entity type with the same id.
        db.add(models.AuditLog(tenant_code="FACTORY_A", actor="x", action="planted_old",
                               entity_type="service_contract", entity_id=cid,
                               details="old", created_at=created - timedelta(days=1)))
        db.add(models.AuditLog(tenant_code="FACTORY_B", actor="x", action="planted_other",
                               entity_type="service_contract", entity_id=cid,
                               details="other tenant", created_at=datetime.utcnow()))
        db.add(models.AuditLog(tenant_code="FACTORY_A", actor="x", action="planted_type",
                               entity_type="work_order", entity_id=cid,
                               details="other entity", created_at=datetime.utcnow()))
        db.commit()
    fh = GET(f"/service-contracts/{cid}/history", TOKENS["fa_super"]).body.get("history", [])
    oh = GET(f"/oem/contracts/{cid}/history", TOKENS["alpha_view"]).body.get("history", [])
    factory_actions = {x["action"] for x in fh}
    oem_actions = {x["action"] for x in oh}
    check("the factory's history has the proposal, its acceptance and the statements",
          {"contract_proposed", "contract_accepted", "contract_dispute_raised"}
          <= factory_actions, sorted(factory_actions))
    check("...but not the OEM-only drafting row", "contract_drafted" not in factory_actions,
          sorted(factory_actions))
    check("the OEM's history includes its drafting row",
          "contract_drafted" in oem_actions, sorted(oem_actions))
    check("neither includes a row from before the contract existed",
          "planted_old" not in factory_actions | oem_actions)
    check("...another tenant's row about the same id",
          "planted_other" not in factory_actions | oem_actions)
    check("...or another entity type with the same id",
          "planted_type" not in factory_actions | oem_actions)
    details = " ".join((x["details"] or "") for x in fh + oh)
    check("no audit detail carries statement figures",
          not any(w in details for w in ("AVAILABLE", "UNMEASURED", "seconds", "availability",
                                         "credit", "totals")), details[:300])
    other = GET(f"/service-contracts/{S['cid_b']}/history", TOKENS["fa"])
    check("FACTORY_A cannot read FACTORY_B's contract history", other.status == 404, other)
    check("CONTROL: a history under the cap says it is not truncated",
          GET(f"/service-contracts/{cid}/history", TOKENS["fa"]).body.get("truncated") is False)

    # A long contract outgrows the cap. What falls off must be the OLDEST rows:
    # a trail that silently drops the latest action is a trail that lies.
    import service_contracts
    with H.unscoped() as db:
        for i in range(service_contracts.MAX_HISTORY):
            db.add(models.AuditLog(tenant_code="FACTORY_A", actor="x", action="planted_bulk",
                                   entity_type="service_contract", entity_id=cid,
                                   details=f"bulk {i}", created_at=created))
        db.commit()
    t = POST(f"/service-contracts/{cid}/terminate", TOKENS["fa"], {"reason": "closing the site"})
    check("CONTROL: the factory terminates after the bulk rows (a new audited action)",
          t.status == 200, t)
    big = GET(f"/service-contracts/{cid}/history", TOKENS["fa"]).body
    rows = big.get("history", [])
    check(f"past {service_contracts.MAX_HISTORY} rows the history is capped and says so",
          len(rows) == service_contracts.MAX_HISTORY and big.get("truncated") is True,
          (len(rows), big.get("truncated")))
    # The bulk rows are stamped at the contract's creation instant, older than
    # every real row, so they are what the cap must drop.
    actions = [x["action"] for x in rows]
    check("...keeping the newest action (the termination) and every real row, dropping "
          "only the oldest",
          rows and rows[-1]["action"] == "contract_terminated"
          and "contract_proposed" in actions
          and actions.count("planted_bulk") < service_contracts.MAX_HISTORY,
          (rows[-1:], actions.count("planted_bulk")))
    check("...still oldest-first within the page",
          [x["at"] for x in rows] == sorted(x["at"] for x in rows))


def run_all():
    H.boot()
    H.seed()
    case_setup()
    case_withdrawal_withholds_on_the_next_request()
    case_acceptance_grants_to_that_manufacturer_only()
    case_the_reason_vocabulary_is_the_factorys_alone()
    case_nothing_an_oem_reads_carries_factory_secrets()
    case_history_is_the_callers_own_trail()


def test_contract_consent():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A MANUFACTURER SEES STATEMENTS ONLY WHILE THE FACTORY SHARES DOWNTIME")
