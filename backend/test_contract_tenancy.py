"""Tenant and manufacturer isolation of service contracts (ADR-0020, ADR-0017, ADR-0002).

THE PROPERTIES UNDER TEST
-------------------------
  * A manufacturer reaches ONLY its own contracts. Every contract route, driven
    with a valid body, answers another manufacturer 404 — the same refusal, word
    for word, as a contract that does not exist — and changes nothing.
  * A factory reaches ONLY contracts addressed to it. Every factory route answers
    another factory 404 and changes nothing.
  * The two authentication worlds do not cross: a factory token on an OEM route
    is not an OEM session (401), an OEM token on a factory route is refused
    (403), and an Operator reaches no contract route at all.
  * A statement or dispute id belongs to its contract: pairing it with another
    contract of the same manufacturer is a 404.
  * The founder's company switcher previews a factory's contracts only for an
    Admin, and a switcher aimed at an OEM sentinel is refused outright.
  * Service contracts are core: the plan gate never blocks them, while it still
    blocks a gated pack for the same tenant.
  * A party's contract list, past its cap, keeps the NEWEST contracts and says
    it truncated.

The route lists are read from the routers themselves, and the suite asserts it
found every route, so a route added later cannot silently escape these checks.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_tenancy.py
"""
import inspect
from datetime import datetime, timedelta

from pydantic import BaseModel

import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS, check, section

FMT = "%Y-%m-%dT%H:%M:%SZ"
OEM_ROUTE_COUNT = 24
FACTORY_ROUTE_COUNT = 23

CONTRACT_TABLES = (models.ServiceContract, models.ServiceContractTermVersion,
                   models.ServiceContractMachine, models.ContractStatement,
                   models.ContractAttributionRecord, models.ContractStatementAcceptance,
                   models.ContractDispute, models.OemDataSharingPolicy, models.AuditLog,
                   models.Notification, models.EventLog)


def _snapshot():
    with H.unscoped() as db:
        counts = {m.__tablename__: db.query(m).count() for m in CONTRACT_TABLES}
        counts["statuses"] = sorted((c.id, c.status, c.termination_effective_at is None)
                                    for c in db.query(models.ServiceContract).all())
        counts["disputes"] = sorted((d.id, d.status)
                                    for d in db.query(models.ContractDispute).all())
        counts["grants"] = sorted((p.oem_code, p.tenant_code, p.grants)
                                  for p in db.query(models.OemDataSharingPolicy).all())
        return counts


def _samples():
    """A VALID body for every request model, so a refusal is the route's
    decision about WHO is asking, never a validation error."""
    st = S["st"]
    ws = H.parse_ts(S["ps"][1]["start"]) + timedelta(days=12)
    return {
        "ContractDraft": {"contract_ref": "AMC-PROBE", "title": "probe",
                          "contract_type": "AMC", "factory_tenant_code": "FACTORY_A",
                          "start_month": H.month_label(30),
                          "terms": H.terms(("SN-A1",))},
        "TermsHash": {"terms_hash": S["hash"]},
        "FactoryAcceptance": {"terms_hash": S["hash"], "grant_downtime_sharing": True},
        "DecisionNote": {"note": "probe"},
        "Termination": {"reason": "probe"},
        "AmendmentDraft": {"terms": H.terms(("SN-A1",)),
                           "effective_from": S["ps"][6]["start"]},
        "ComputeRequest": {"period_start": S["ps"][0]["start"]},
        "StatementAcceptance": {"content_hash": st["content_hash"],
                                "revision": st["revision"]},
        "DisputeRaise": {"installation_id": S["inst"]["SN-A1"],
                         "window_start": ws.strftime(FMT),
                         "window_end": (ws + timedelta(hours=1)).strftime(FMT),
                         "reason": "probe", "proposed_bucket": "OEM"},
        "ResolutionProposal": {"resolution_bucket": "OEM"},
        "ResolutionAcceptance": {"resolution_bucket": "OEM"},
    }


def _requests(router, prefix, ids):
    """(method, concrete path, body, label) for every route of a router."""
    samples = _samples()
    out = []
    for route in router.routes:
        body = None
        for p in inspect.signature(route.endpoint).parameters.values():
            ann = p.annotation
            if inspect.isclass(ann) and issubclass(ann, BaseModel):
                body = samples[ann.__name__]
        path = route.path
        for key, value in ids.items():
            path = path.replace("{" + key + "}", str(value))
        for method in sorted(route.methods):
            out.append((method, path, body, f"{method} {route.path[len(prefix):] or '/'}"))
    return out


def _send(method, path, tok, body):
    if method == "GET":
        return GET(path, tok)
    if method == "PUT":
        return H.PUT(path, tok, body)
    return POST(path, tok, body)


def case_setup():
    section("0. TWO MANUFACTURERS, TWO FACTORIES, A LIVE CONTRACT WITH A DISPUTE")
    cid = H.active_contract(serials=("SN-A1", "SN-A2"), start_offset=-4)
    S["cid"] = cid
    S["ps"] = H.periods(cid)
    S["hash"] = H.version_hash(cid)
    H.measured_periods(cid)
    c = H.compute(cid, S["ps"][1]["start"], oem=False)
    ws = H.parse_ts(S["ps"][1]["start"]) + timedelta(days=2)
    d = POST(f"/service-contracts/{cid}/statements/{c.body['statement']['id']}/disputes",
             TOKENS["fa"], {"installation_id": S["inst"]["SN-A1"],
                            "window_start": ws.strftime(FMT),
                            "window_end": (ws + timedelta(hours=1)).strftime(FMT),
                            "reason": "stop", "proposed_bucket": "FACTORY"})
    check("setup: FACTORY_A's contract has a statement and an open dispute",
          c.status == 200 and d.status == 200, (c, d))
    S["st"] = d.body["statement"]
    S["did"] = d.body["dispute"]["id"]
    S["ids"] = {"contract_id": cid, "version": 1, "statement_id": S["st"]["id"],
                "dispute_id": S["did"]}
    # A second ALPHA contract, at FACTORY_B, with its own statement.
    other = H.active_contract(serials=("SN-AB",), tenant="FACTORY_B", fac_tok=TOKENS["fb"],
                              start_offset=-4)
    H.add_span("FACTORY_B", S["machines"]["AB"], H.parse_ts(S["ps"][0]["start"]),
               H.parse_ts(S["ps"][1]["end"]))
    c2 = H.compute(other, H.periods(other)[0]["start"], oem=False, tok=TOKENS["fb"])
    check("setup: ALPHA also has a contract, with a statement, at FACTORY_B",
          c2.status == 200, c2)
    S["other"] = other
    S["other_sid"] = c2.body["statement"]["id"]


def case_another_manufacturer_gets_404_everywhere():
    section("1. ANOTHER MANUFACTURER GETS THE SAME 404 ON EVERY CONTRACT ROUTE")
    import oem_contract_routes
    reqs = _requests(oem_contract_routes.router, "/oem/contracts", S["ids"])
    check(f"found all {OEM_ROUTE_COUNT} OEM contract routes on the router",
          len(reqs) == OEM_ROUTE_COUNT, [r[3] for r in reqs])
    missing = H.GET("/oem/contracts/999999", TOKENS["alpha"])
    check("CONTROL: a contract that does not exist is 404", missing.status == 404, missing)
    before = _snapshot()
    scoped = 0
    for method, path, body, label in reqs:
        if "{" in path or path == "/oem/contracts":
            continue          # the list and create are not about one contract
        scoped += 1
        r = _send(method, path, TOKENS["beta"], body)
        check(f"OEM_BETA {label}: 404, worded like a missing contract",
              r.status == 404 and r.body.get("detail") == missing.body.get("detail"), r)
    check("...(22 contract-scoped routes probed)", scoped == OEM_ROUTE_COUNT - 2, scoped)
    check("...and none of those requests changed anything", _snapshot() == before)
    lst = GET("/oem/contracts", TOKENS["beta"])
    check("OEM_BETA's list holds none of ALPHA's contracts",
          lst.status == 200 and not [c for c in lst.body["contracts"]
                                     if c["oem_code"] != "OEM_BETA"], lst)
    body = dict(_samples()["ContractDraft"], oem_code="OEM_ALPHA")
    r = POST("/oem/contracts", TOKENS["beta"], body)
    check("OEM_BETA cannot draft as ALPHA by naming an oem_code", r.status == 422, r)
    r = POST("/oem/contracts", TOKENS["beta"], _samples()["ContractDraft"])
    check("...nor draft over ALPHA's machine at FACTORY_A", r.status == 422, r)


def case_another_factory_gets_404_everywhere():
    section("2. ANOTHER FACTORY GETS 404 ON EVERY FACTORY CONTRACT ROUTE")
    import service_contract_routes
    reqs = _requests(service_contract_routes.router, "/service-contracts", S["ids"])
    check(f"found all {FACTORY_ROUTE_COUNT} factory contract routes on the router",
          len(reqs) == FACTORY_ROUTE_COUNT, [r[3] for r in reqs])
    before = _snapshot()
    for method, path, body, label in reqs:
        if path == "/service-contracts":
            continue
        r = _send(method, path, TOKENS["fb"], body)
        check(f"FACTORY_B Admin {label}: 404", r.status == 404, r)
        r = _send(method, path, TOKENS["fc"], body)
        check(f"FACTORY_C Admin {label}: 404", r.status == 404, r)
    check("...and none of those requests changed anything", _snapshot() == before)
    for tok in ("fb", "fc"):
        lst = GET("/service-contracts", TOKENS[tok])
        check(f"{tok}'s list holds no contract addressed to another factory",
              lst.status == 200 and all(c["factory_tenant_code"] != "FACTORY_A"
                                        for c in lst.body["contracts"]), lst)


def case_authentication_worlds_do_not_cross():
    section("3. FACTORY AND OEM SESSIONS DO NOT CROSS; OPERATORS REACH NOTHING")
    import oem_contract_routes
    import service_contract_routes
    before = _snapshot()
    for method, path, body, label in _requests(oem_contract_routes.router, "/oem/contracts",
                                               S["ids"]):
        r = _send(method, path, TOKENS["fa"], body)
        check(f"a factory Admin token on OEM {label}: 401 not an OEM session",
              r.status == 401 and r.body.get("detail") == "Not an OEM session", r)
    for method, path, body, label in _requests(service_contract_routes.router,
                                               "/service-contracts", S["ids"]):
        r = _send(method, path, TOKENS["alpha"], body)
        check(f"an OEM admin token on factory {label}: 403 OEM session",
              r.status == 403 and "OEM session" in str(r.body.get("detail")), r)
        r = _send(method, path, TOKENS["fa_op"], body)
        check(f"a factory Operator on {label}: 403", r.status == 403, r)
    check("...and none of those requests changed anything", _snapshot() == before)


def case_ids_belong_to_their_contract():
    section("4. A STATEMENT OR DISPUTE ID BELONGS TO ITS OWN CONTRACT")
    cid, other, osid = S["cid"], S["other"], S["other_sid"]
    for path in (f"/oem/contracts/{cid}/statements/{osid}",
                 f"/oem/contracts/{cid}/statements/{osid}/verify",
                 f"/oem/contracts/{cid}/statements/{osid}/download"):
        r = GET(path, TOKENS["alpha"])
        check(f"ALPHA pairing contract {cid} with contract {other}'s statement: 404",
              r.status == 404, r)
    r = GET(f"/oem/contracts/{other}/statements/{osid}", TOKENS["alpha"])
    check("CONTROL: the statement under its own contract is readable", r.status == 200, r)
    r = POST(f"/oem/contracts/{other}/disputes/{S['did']}/propose-resolution",
             TOKENS["alpha"], {"resolution_bucket": "OEM"})
    check("a dispute id under the wrong contract: 404", r.status == 404, r)
    r = POST(f"/oem/contracts/{other}/statements/{S['st']['id']}/accept", TOKENS["alpha"],
             {"content_hash": S["st"]["content_hash"], "revision": S["st"]["revision"]})
    check("accepting another contract's statement through this one: 404", r.status == 404, r)

    # Ids no INTEGER column can hold get the missing-row answer, never a 500.
    huge = 10 ** 30
    missing = GET("/oem/contracts/999999", TOKENS["alpha"])
    for path in (f"/oem/contracts/{huge}", f"/oem/contracts/{cid}/statements/{huge}",
                 f"/service-contracts/{huge}/history"):
        tok = TOKENS["fa"] if path.startswith("/service") else TOKENS["alpha"]
        r = GET(path, tok)
        check(f"{path.replace(str(huge), 'HUGE')}: 404, no server error", r.status == 404, r)
    r = GET(f"/oem/contracts/{huge}", TOKENS["alpha"])
    check("...worded exactly like a missing contract", r.body == missing.body, r)
    ws = H.parse_ts(S["ps"][1]["start"]) + timedelta(days=20)
    r = POST(f"/service-contracts/{cid}/statements/{S['st']['id']}/disputes", TOKENS["fa"],
             {"installation_id": huge, "window_start": ws.strftime(FMT),
              "window_end": (ws + timedelta(hours=1)).strftime(FMT), "reason": "x",
              "proposed_bucket": "OEM"})
    check("a dispute naming a HUGE installation id: 422, no server error", r.status == 422, r)
    r = POST(f"/oem/contracts/{cid}/amendments/{huge}/accept", TOKENS["alpha"],
             {"terms_hash": S["hash"]})
    check("an amendment version no column can hold: 404", r.status == 404, r)


def case_founder_preview():
    section("5. THE FOUNDER'S SWITCHER: ADMIN ONLY, NEVER AT AN OEM SENTINEL")
    cid = S["cid"]
    founder = H.fac_token("founder_admin", "DEFAULT", "Admin")
    staff = H.fac_token("founder_staff", "DEFAULT", "Supervisor")
    r = GET(f"/service-contracts/{cid}", founder, headers={"X-Tenant": "FACTORY_A"})
    check("a founder-workspace Admin previewing FACTORY_A sees its contract",
          r.status == 200, r)
    r = GET(f"/service-contracts/{cid}", staff, headers={"X-Tenant": "FACTORY_A"})
    check("a founder-workspace Supervisor's X-Tenant is ignored: 404", r.status == 404, r)
    with H.unscoped() as db:
        now = H.now_utc()
        planted = models.ServiceContract(
            oem_code="OEM_ALPHA", factory_tenant_code="OEM:OEM_ALPHA", contract_ref="PLANTED",
            title="bad data", contract_type="AMC", status="proposed", starts_at=now,
            ends_at=now + timedelta(days=365), created_by="x", created_at=now,
            proposed_at=now)
        db.add(planted)
        db.commit()
        pid = planted.id
    r = GET("/service-contracts", founder, headers={"X-Tenant": "OEM:OEM_ALPHA"})
    check("a switcher aimed at an OEM sentinel is refused outright (403)",
          r.status == 403, r)
    r = GET(f"/service-contracts/{pid}", founder, headers={"X-Tenant": "OEM:OEM_ALPHA"})
    check("...even for a (planted) row addressed to that sentinel", r.status == 403, r)


def case_the_plan_gate_never_blocks_contracts():
    section("6. SERVICE CONTRACTS ARE CORE: THE PLAN GATE NEVER BLOCKS THEM")
    import plan_gate
    for path in ("/service-contracts", "/service-contracts/1/accept", "/oem/contracts",
                 "/oem/contracts/1/statements/2"):
        pack = plan_gate.pack_for_path(path)
        check(f"{path} maps to no gated pack", pack is None
              or pack in plan_gate._ALWAYS_OPEN_PACKS, pack)
    original = plan_gate.SessionLocal
    plan_gate.SessionLocal = H.SessionLocal
    try:
        with H.unscoped() as db:
            db.add(models.TenantConfig(tenant_code="FACTORY_A", plan="starter",
                                       enabled_modules="core",
                                       trial_ends_at=datetime.utcnow() + timedelta(days=30)))
            db.commit()
        plan_gate.invalidate("FACTORY_A")
        gated = GET("/work-orders", TOKENS["fa"])
        check("CONTROL: the gate is live for a starter FACTORY_A (work orders: 403)",
              gated.status == 403, gated)
        r = GET("/service-contracts", TOKENS["fa"])
        check("...and /service-contracts still answers it (200)", r.status == 200, r)
    finally:
        plan_gate.SessionLocal = original
        plan_gate.invalidate("FACTORY_A")


def case_a_long_list_keeps_the_newest_contracts():
    section("7. PAST ITS CAP A CONTRACT LIST KEEPS THE NEWEST, AND SAYS SO")
    import service_contracts
    cap = service_contracts.MAX_LIST
    for path, tok in (("/oem/contracts", TOKENS["beta"]), ("/service-contracts", TOKENS["fc"])):
        r = GET(path, tok)
        check(f"CONTROL: {path} under the cap says it is not truncated",
              r.status == 200 and r.body.get("truncated") is False, r)
    # Planted, because no route makes 500 contracts quickly: OEM_BETA contracts
    # proposed to FACTORY_C (then withdrawn), so both parties' lists outgrow the
    # cap. The last one planted is the newest.
    now = H.now_utc()
    with H.unscoped() as db:
        for i in range(cap + 1):
            db.add(models.ServiceContract(
                oem_code="OEM_BETA", factory_tenant_code="FACTORY_C",
                contract_ref="BULK-NEWEST" if i == cap else f"BULK-{i:04d}", title="bulk",
                contract_type="AMC", status="withdrawn", starts_at=now,
                ends_at=now + timedelta(days=365), created_by="x", created_at=now,
                proposed_at=now, updated_at=now))
        db.commit()
    for path, tok, who in (("/oem/contracts", TOKENS["beta"], "the manufacturer"),
                           ("/service-contracts", TOKENS["fc"], "the factory")):
        r = GET(path, tok)
        refs = [c["contract_ref"] for c in r.body.get("contracts", [])]
        check(f"{who}'s list is capped at {cap} and says it truncated",
              r.status == 200 and len(refs) == cap and r.body.get("truncated") is True,
              (r.status, len(refs), r.body.get("truncated")))
        check("...keeping the newest contract and dropping the oldest",
              "BULK-NEWEST" in refs and "BULK-0000" not in refs, refs[-2:])
        ids = [c["id"] for c in r.body.get("contracts", [])]
        check("...in ascending id order", ids == sorted(ids))


def run_all():
    H.boot()
    H.seed()
    case_setup()
    case_another_manufacturer_gets_404_everywhere()
    case_another_factory_gets_404_everywhere()
    case_authentication_worlds_do_not_cross()
    case_ids_belong_to_their_contract()
    case_founder_preview()
    case_the_plan_gate_never_blocks_contracts()
    case_a_long_list_keeps_the_newest_contracts()


def test_contract_tenancy():
    H.failures.clear()
    try:
        run_all()
    finally:
        H.uninstall()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A CONTRACT IS VISIBLE TO ITS TWO PARTIES AND TO NOBODY ELSE")
