"""A founder's company preview reads a service contract but cannot sign it (ADR-0021).

The factory side derived its party from tenancy.request_tenant, which honours
the founder's company-switcher preview (an X-Tenant header, for a DEFAULT-claim
Admin). So the platform operator, previewing a customer, could ACCEPT a service
contract for that customer, and with it give the manufacturer the customer's
consent to its downtime evidence (grant_downtime_sharing). The product's whole
promise is a statement both parties accepted; the platform operator accepting
for one of them breaks it.

The rule is tenancy.is_preview, shared with AMP-native AI's consent (ADR-0020):
a preview may read, it may not speak for the company. Every factory-side POST
(accept, reject, amend, compute, dispute, resolve, withdraw, terminate) takes its
party from service_contracts.for_factory_signer, which refuses a preview with a
403 that says why. Reads keep using for_factory and stay open to a preview.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_preview_cannot_sign.py
"""
import ast
import os

import contract_route_harness as H
import models
import tenancy
from contract_route_harness import GET, POST, check, section
from security import hash_password

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW_A = {"X-Tenant": "FACTORY_A"}


def _founder_token():
    with H.unscoped() as db:
        if not db.query(models.User).filter(models.User.username == "founder").first():
            db.add(models.User(username="founder", password=hash_password("x"), role="Admin",
                               tenant_code="DEFAULT", is_active=True))
    return H.fac_token("founder", "DEFAULT", "Admin")


def case_a_preview_reads_but_cannot_accept():
    section("1. A PREVIEW READS THE CONTRACT, BUT CANNOT ACCEPT IT FOR THE COMPANY")
    tok = _founder_token()
    r = H.draft()
    assert r.status == 200, r
    cid = r.body["id"]
    assert H.propose(cid).status == 200
    seen = GET(f"/service-contracts/{cid}", tok, headers=PREVIEW_A)
    check("the founder previewing FACTORY_A can read the proposed contract", seen.status == 200, seen)
    body = {"terms_hash": H.factory_hash(cid), "grant_downtime_sharing": True}
    refused = POST(f"/service-contracts/{cid}/accept", tok, body, headers=PREVIEW_A)
    check("...but accepting it from the preview is refused (403)", refused.status == 403, refused)
    check("...and the refusal says why", "preview" in str(refused.body).lower(), refused.body)
    check("the contract is still only proposed", H.contract_detail(cid).body.get("status") == "proposed",
          H.contract_detail(cid).body.get("status"))
    check("...and the manufacturer was given no downtime-sharing grant",
          "SHARE_DOWNTIME" not in (H.grants() or set()), H.grants())
    own = H.factory_accept(cid)
    check("FACTORY_A's own Admin can still accept it", own.status == 200, own)


def case_a_preview_cannot_dispute_or_terminate():
    section("2. NOR RAISE, RESOLVE OR END ANYTHING FOR IT")
    tok = _founder_token()
    cid = H.active_contract(serials=("SN-A2",))
    r = POST(f"/service-contracts/{cid}/terminate", tok, {"reason": "previewing"}, headers=PREVIEW_A)
    check("terminating from the preview is refused (403), not rejected for its body",
          r.status == 403 and "preview" in str(r.body).lower(), r)
    own = POST(f"/service-contracts/{cid}/terminate", H.TOKENS["fa"], {"reason": "CONTROL: a valid body"})
    check("CONTROL: the same body from FACTORY_A's own Admin is accepted", own.status == 200, own)


def case_the_rule_itself():
    section("3. tenancy.is_preview")
    founder = {"sub": "founder", "role": "Admin", "tenant": "DEFAULT"}
    own = {"sub": "factory_a_admin", "role": "Admin", "tenant": "FACTORY_A"}
    token = tenancy.set_current_tenant("FACTORY_A")
    try:
        check("the founder bound to FACTORY_A is previewing", tenancy.is_preview(founder) is True)
        check("FACTORY_A's own Admin is not", tenancy.is_preview(own) is False)
    finally:
        tenancy.reset_current_tenant(token)
    check("with nothing bound, nobody is previewing", tenancy.is_preview(founder) is False
          and tenancy.is_preview(own) is False)


def _handlers(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for dec in fn.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
                called = {n.func.attr for n in ast.walk(fn)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                out.append((dec.func.attr, fn.name, called))
    return out


def case_every_factory_post_takes_a_signer():
    section("4. EVERY FACTORY-SIDE POST TAKES ITS PARTY FROM for_factory_signer")
    handlers = _handlers(os.path.join(HERE, "service_contract_routes.py"))
    posts = [(n, c) for verb, n, c in handlers if verb == "post"]
    gets = [(n, c) for verb, n, c in handlers if verb == "get"]
    check(f"the scan found the POST handlers ({len(posts)} >= 13)", len(posts) >= 13, [n for n, _ in posts])
    bad = [n for n, c in posts if "for_factory_signer" not in c or "for_factory" in c]
    check("every POST calls for_factory_signer and never plain for_factory", not bad, bad)
    check("no GET needs a signer (a preview may read)",
          not [n for n, c in gets if "for_factory_signer" in c], [n for n, c in gets])


def run_all():
    H.boot()
    H.seed()
    case_a_preview_reads_but_cannot_accept()
    case_a_preview_cannot_dispute_or_terminate()
    case_the_rule_itself()
    case_every_factory_post_takes_a_signer()


def test_contract_preview_cannot_sign():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("A PREVIEW READS A SERVICE CONTRACT; ONLY THE COMPANY'S OWN ADMIN SIGNS IT")
