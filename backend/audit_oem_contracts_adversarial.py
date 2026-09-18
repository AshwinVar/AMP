"""Service contracts under attack: try to break every boundary they add (ADR-0021).

The route suites prove the rules. This audit plays the adversary against a
populated two-OEM, three-factory world through the HTTP surface only, and
asks one question per line: did the attempt get through?

    A  identity      forged, mismatched, demoted, disabled, expired principals
    B  injection     bodies that name what the principal should decide
    C  replay        a valid acceptance or hash moved to where it does not belong
    D  enumeration   ids that probe for existence, and ids that probe for a 500
    E  consent       a grant withdrawn between reading and acting
    F  races         the same binding action twice at once (threads on PostgreSQL)
    G  transport     a contract reference that tries to become a header

Run: DATABASE_URL="sqlite:///./ci.db" python backend/audit_oem_contracts_adversarial.py
     DATABASE_URL=postgresql://... python backend/audit_oem_contracts_adversarial.py
     (PostgreSQL: point it at a DISPOSABLE database; the seed drops every table.)
"""
import sys
import threading
from datetime import datetime, timedelta

from jose import jwt as pyjwt

import auth
import contract_route_harness as H
import models
from contract_route_harness import GET, POST, S, TOKENS

FMT = "%Y-%m-%dT%H:%M:%SZ"
BREACHES = []
COUNT = [0]


def refused(label, held, detail=""):
    """`held` is True when the boundary held. Evidence prints only on a breach."""
    COUNT[0] += 1
    print(f"  {'REFUSED ' if held else 'BREACHED'}  {label}"
          + (f"   [{detail}]" if detail and not held else ""))
    if not held:
        BREACHES.append(f"{label}: {detail}")


def control(label, condition, detail=""):
    COUNT[0] += 1
    print(f"  {'PASS    ' if condition else 'FAIL    '}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        BREACHES.append(f"CONTROL {label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def _rows(model, **filters):
    with H.unscoped() as db:
        return db.query(model).filter_by(**filters).count()


def _set_active(model, active, **key):
    """Flip one row's is_active through the ORM (one row, loaded by its key)."""
    with H.unscoped() as db:
        row = db.query(model).filter_by(**key).one()
        row.is_active = active
        db.commit()


def setup():
    section("SETUP: ALPHA HAS A LIVE CONTRACT AT FACTORY_A WITH A STATEMENT")
    cid = H.active_contract(serials=("SN-A1",), start_offset=-4)
    ps = H.periods(cid)
    H.measured_periods(cid)
    c0 = H.compute(cid, ps[0]["start"], oem=False)
    c1 = H.compute(cid, ps[1]["start"], oem=False)
    control("two statements computed", c0.status == 200 and c1.status == 200, (c0, c1))
    S.update(cid=cid, ps=ps, s0=c0.body["statement"], s1=c1.body["statement"])
    beta = H.draft(tok=TOKENS["beta"], serials=("SN-B1",), start_offset=40)
    S["beta_cid"] = beta.body["id"]


def attack_identity():
    section("A. IDENTITY: FORGED, MISMATCHED, DEMOTED, DISABLED, EXPIRED")
    cid, s0 = S["cid"], S["s0"]
    path = f"/oem/contracts/{cid}/statements/{s0['id']}/accept"
    body = {"content_hash": s0["content_hash"], "revision": s0["revision"]}
    exp = datetime.utcnow() + timedelta(hours=1)

    forged = pyjwt.encode({"sub": "alpha_admin", "role": "OEM_ADMIN", "principal": "oem",
                           "oem": "OEM_ALPHA", "exp": exp}, "not-the-key", algorithm="HS256")
    r = POST(path, forged, body)
    refused("a token signed with another key", r.status == 401, r)

    cross = H.oem_token("beta_admin", "OEM_ALPHA", "OEM_ADMIN")
    r = GET(f"/oem/contracts/{cid}", cross)
    refused("BETA's admin claiming to be ALPHA", r.status == 403, r)

    promoted = H.oem_token("alpha_view", "OEM_ALPHA", "OEM_ADMIN")
    r = POST(path, promoted, body)
    refused("a viewer whose token claims OEM_ADMIN accepts a statement", r.status == 403, r)

    fake_oem = pyjwt.encode({"sub": "factory_a_admin", "role": "Admin", "tenant": "FACTORY_A",
                             "principal": "oem", "oem": "OEM_ALPHA", "exp": exp},
                            auth.SECRET_KEY, algorithm=H.ALGO)
    r = POST(path, fake_oem, body)
    refused("a factory admin whose token claims to be an OEM, on the OEM route",
            r.status in (401, 403), r)
    r = POST(f"/service-contracts/{cid}/statements/{s0['id']}/accept", fake_oem, body)
    refused("...and the same token on the factory route", r.status == 403, r)

    expired = pyjwt.encode({"sub": "alpha_admin", "role": "OEM_ADMIN", "principal": "oem",
                            "oem": "OEM_ALPHA", "exp": datetime.utcnow() - timedelta(minutes=1)},
                           auth.SECRET_KEY, algorithm=H.ALGO)
    refused("an expired OEM token", POST(path, expired, body).status == 401)

    _set_active(models.OemUser, False, username="alpha_admin")
    try:
        r = GET(f"/oem/contracts/{cid}", TOKENS["alpha"])
        refused("a disabled OEM admin, on the very next request", r.status == 403, r)
    finally:
        _set_active(models.OemUser, True, username="alpha_admin")
    _set_active(models.OemOrganization, False, oem_code="OEM_ALPHA")
    try:
        r = GET(f"/oem/contracts/{cid}", TOKENS["alpha"])
        refused("a suspended manufacturer, on the very next request", r.status == 403, r)
    finally:
        _set_active(models.OemOrganization, True, oem_code="OEM_ALPHA")
    r = GET(f"/service-contracts/{cid}", TOKENS["fb"], headers={"X-Tenant": "FACTORY_A"})
    refused("a customer Admin's X-Tenant header aimed at another factory", r.status == 404, r)
    control("CONTROL: the rightful OEM admin still reads it",
            GET(f"/oem/contracts/{cid}", TOKENS["alpha"]).status == 200)


def attack_injection():
    section("B. INJECTION: BODIES THAT NAME WHAT THE PRINCIPAL DECIDES")
    cid = S["cid"]
    base = {"contract_ref": "INJ-1", "title": "t", "contract_type": "AMC",
            "factory_tenant_code": "FACTORY_A", "start_month": H.month_label(60),
            "terms": H.terms(("SN-A2",))}
    before = _rows(models.ServiceContract)
    for extra, label in (({"oem_code": "OEM_BETA"}, "an oem_code"),
                         ({"status": "accepted"}, "a status"),
                         ({"factory_accepted_by": "factory_a_admin"}, "an acceptance")):
        r = POST("/oem/contracts", TOKENS["alpha"], dict(base, **extra))
        refused(f"a draft body carrying {label}", r.status == 422, r)
    r = POST("/oem/contracts", TOKENS["alpha"], dict(base, factory_tenant_code="DEFAULT"))
    refused("a contract addressed to the founder workspace", r.status == 422, r)
    many = H.terms(("SN-A2",))
    many["covered_installations"] = [{"installation_id": 10 ** 6 + i, "serial_number": f"X{i}"}
                                     for i in range(201)]
    r = POST("/oem/contracts", TOKENS["alpha"], dict(base, terms=many))
    refused("a contract covering 201 installations (row-lock amplification)",
            r.status == 422, r)
    nan = dict(H.terms(("SN-A2",)), reason_lead_seconds=float("nan"))
    r = POST("/oem/contracts", TOKENS["alpha"], dict(base, terms=nan))
    refused("terms carrying a non-integer lead time", r.status == 422, r)
    refused("...none of these created a contract", _rows(models.ServiceContract) == before)

    r = POST(f"/service-contracts/{cid}/accept", TOKENS["fa"],
             {"terms_hash": H.factory_hash(cid), "grant_downtime_sharing": True,
              "factory_tenant_code": "FACTORY_B"})
    refused("an acceptance body naming a factory", r.status == 422, r)
    s0 = S["s0"]
    r = POST(f"/oem/contracts/{cid}/statements/{s0['id']}/accept", TOKENS["alpha"],
             {"content_hash": s0["content_hash"], "revision": True})
    refused("revision sent as the boolean true", r.status == 422, r)
    r = POST(f"/oem/contracts/{cid}/statements/{s0['id']}/accept", TOKENS["alpha"],
             {"content_hash": "a" * 200000, "revision": 1})
    refused("a 200 kB content hash (no crash, no acceptance)", r.status in (409, 422)
            and _rows(models.ContractStatementAcceptance) == 0, r.status)
    r = H.compute(cid, "9999-12-31T23:59:59Z")
    refused("compute for the year 9999", r.status == 422, r)
    r = POST(f"/service-contracts/{cid}/statements/{s0['id']}/disputes", TOKENS["fa"],
             {"installation_id": S["inst"]["SN-A1"], "window_start": "0001-01-01T00:00:00Z",
              "window_end": "9999-01-01T00:00:00Z", "reason": "everything",
              "proposed_bucket": "FACTORY"})
    refused("a dispute over all of time", r.status == 422, r)


def attack_replay():
    section("C. REPLAY: A VALID HASH OR ACCEPTANCE MOVED WHERE IT DOES NOT BELONG")
    cid, s0, s1 = S["cid"], S["s0"], S["s1"]
    r = H.accept_statement(cid, s1["id"], s0["content_hash"], s0["revision"])
    refused("statement 1 accepted with statement 0's hash", r.status == 409, r)
    r = POST(f"/oem/contracts/{S['beta_cid']}/propose", TOKENS["alpha"],
             {"terms_hash": H.version_hash(S["beta_cid"], tok=TOKENS["beta"])})
    refused("ALPHA proposing BETA's draft with BETA's own hash", r.status == 404, r)
    r = POST(f"/service-contracts/{cid}/accept", TOKENS["fb"],
             {"terms_hash": H.factory_hash(cid), "grant_downtime_sharing": True})
    refused("FACTORY_B replaying FACTORY_A's acceptance", r.status == 404, r)
    r = POST(f"/service-contracts/{cid}/accept", TOKENS["fa"],
             {"terms_hash": H.factory_hash(cid), "grant_downtime_sharing": True})
    refused("FACTORY_A accepting the same contract a second time", r.status == 409, r)
    a = H.accept_statement(cid, s0["id"], s0["content_hash"], s0["revision"], oem=False)
    control("CONTROL: the factory accepts statement 0", a.status == 200, a)
    r = H.accept_statement(cid, s0["id"], s0["content_hash"], s0["revision"], oem=False)
    refused("the factory accepting the same revision twice", r.status == 409, r)
    control("...leaving exactly one acceptance row",
            _rows(models.ContractStatementAcceptance, statement_id=s0["id"]) == 1)


def attack_enumeration():
    section("D. ENUMERATION: EXISTENCE ORACLES AND 500s")
    cid, s0 = S["cid"], S["s0"]
    real = GET(f"/oem/contracts/{cid}", TOKENS["beta"])
    for probe in (0, -1, 2 ** 31, 2 ** 63 - 1, 10 ** 30):
        r = GET(f"/oem/contracts/{probe}", TOKENS["beta"])
        refused(f"GET /oem/contracts/{probe}: no 500, and no different answer from a real id",
                r.status in (404, 422) and (r.status == 422 or r.body == real.body), r)
    a = GET(f"/oem/contracts/{cid}/statements/{s0['id']}", TOKENS["beta"])
    b = GET(f"/oem/contracts/{cid}/statements/999999", TOKENS["beta"])
    refused("BETA cannot tell a real statement id from a missing one",
            a.status == b.status == 404 and a.body == b.body, (a, b))
    a = GET(f"/service-contracts/{cid}/disputes", TOKENS["fc"])
    b = GET("/service-contracts/999999/disputes", TOKENS["fc"])
    refused("FACTORY_C cannot tell a real contract from a missing one",
            a.status == b.status == 404 and a.body == b.body, (a, b))


def attack_consent():
    section("E. CONSENT WITHDRAWN BETWEEN READING AND ACTING")
    cid, s1 = S["cid"], S["s1"]
    seen = GET(f"/oem/contracts/{cid}/statements/{s1['id']}", TOKENS["alpha"])
    control("the OEM reads the statement while downtime is shared", seen.status == 200, seen)
    r = H.PUT("/connected-equipment/sharing", TOKENS["fa"],
              {"oem_code": "OEM_ALPHA", "grants": ["SHARE_OPERATING_HOURS"]})
    control("the factory withdraws SHARE_DOWNTIME", r.status == 200, r)
    n = _rows(models.ContractStatementAcceptance, statement_id=s1["id"])
    r = H.accept_statement(cid, s1["id"], seen.body["content_hash"], seen.body["revision"])
    refused("the OEM accepting what it read a moment ago", r.status == 403
            and _rows(models.ContractStatementAcceptance, statement_id=s1["id"]) == n, r)
    r = GET(f"/oem/contracts/{cid}/statements/{s1['id']}/download", TOKENS["alpha"])
    refused("the OEM downloading it", r.status == 403 and b"intervals" not in r.raw, r.status)
    H.PUT("/connected-equipment/sharing", TOKENS["fa"],
          {"oem_code": "OEM_ALPHA", "grants": ["SHARE_OPERATING_HOURS", "SHARE_DOWNTIME"]})


def attack_races():
    section("F. THE SAME BINDING ACTION TWICE AT ONCE")
    cid, s1 = S["cid"], S["s1"]
    rounds = 5 if H.ON_POSTGRES else 1
    for n in range(rounds):
        cur = GET(f"/service-contracts/{cid}/statements/{s1['id']}", TOKENS["fa"]).body
        if cur.get("agreed"):
            break
        with H.unscoped() as db:
            for row in (db.query(models.ContractStatementAcceptance)
                          .filter_by(statement_id=s1["id"], party="OEM").all()):
                db.delete(row)
            db.commit()
        out = []
        if H.ON_POSTGRES:
            barrier = threading.Barrier(2)

            def go():
                barrier.wait()
                out.append(H.accept_statement(cid, s1["id"], cur["content_hash"],
                                              cur["revision"]).status)
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for _ in range(2):
                out.append(H.accept_statement(cid, s1["id"], cur["content_hash"],
                                              cur["revision"]).status)
        refused(f"round {n + 1}: two simultaneous OEM acceptances of one revision "
                "record exactly one", sorted(out) == [200, 409]
                and _rows(models.ContractStatementAcceptance, statement_id=s1["id"],
                          party="OEM") == 1, out)

    ws = H.parse_ts(S["ps"][1]["start"]) + timedelta(days=6)
    body = {"installation_id": S["inst"]["SN-A1"], "window_start": ws.strftime(FMT),
            "window_end": (ws + timedelta(hours=2)).strftime(FMT), "reason": "race",
            "proposed_bucket": "FACTORY"}
    out = []
    if H.ON_POSTGRES:
        barrier = threading.Barrier(2)

        def raise_one():
            barrier.wait()
            out.append(POST(f"/service-contracts/{cid}/statements/{s1['id']}/disputes",
                            TOKENS["fa"], body).status)
        threads = [threading.Thread(target=raise_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for _ in range(2):
            out.append(POST(f"/service-contracts/{cid}/statements/{s1['id']}/disputes",
                            TOKENS["fa"], body).status)
    refused("two identical overlapping disputes at once: exactly one is recorded",
            sorted(out) == [200, 409] and _rows(models.ContractDispute,
                                                statement_id=s1["id"]) == 1, out)


def attack_transport():
    section("G. A CONTRACT REFERENCE THAT TRIES TO BECOME A HEADER")
    r = POST("/oem/contracts", TOKENS["alpha"],
             {"contract_ref": "AMC\r\nX-Evil: 1", "title": "t\r\nSet-Cookie: a=b",
              "contract_type": "AMC", "factory_tenant_code": "FACTORY_A",
              "start_month": H.month_label(70), "terms": H.terms(("SN-A2",))})
    control("the reference is stored as text (it is only ever JSON)", r.status in (200, 422), r)
    cid, s0 = S["cid"], S["s0"]
    d = GET(f"/oem/contracts/{cid}/statements/{s0['id']}/download", TOKENS["alpha"])
    refused("the download's headers carry no request-controlled text",
            d.status == 200 and "x-evil" not in d.headers and "set-cookie" not in d.headers
            and d.headers.get("content-disposition", "").startswith(
                f'attachment; filename="statement-{cid}-{s0["id"]}-r'), d.headers)


def main():
    H.boot()
    H.seed()
    setup()
    attack_identity()
    attack_injection()
    attack_replay()
    attack_enumeration()
    attack_consent()
    attack_races()
    attack_transport()
    print()
    H.stand_in_banner()
    print("=" * 74)
    if BREACHES:
        print(f"{len(BREACHES)} OF {COUNT[0]} CHECKS FAILED:")
        for b in BREACHES:
            print("   *", b)
        return 1
    print(f"ALL {COUNT[0]} ATTACKS REFUSED ({'PostgreSQL' if H.ON_POSTGRES else 'SQLite'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
