"""The factory-controlled machine claim (ADR-0019).

#517 refused to build an OEM-side assignment and wrote a test that posts
`factory_tenant_code` in the body to prove it is ignored. This is the workflow
that closes the gap that refusal left, and the property under test is the one it
was protecting:

    AN OEM CANNOT ATTACH A MACHINE TO A FACTORY. IT CAN ONLY OFFER.

The damage a direct assignment would do is not that the OEM reads the factory's
data — the sharing policy still stands, and a fresh installation shares nothing.
It is that a row appears on that factory's Connected Equipment screen, presented
by AMP as their equipment, from a supplier they have never dealt with, beside
controls inviting them to grant it access.

So the tests below spend most of their effort on the ways a code could be
guessed, replayed, raced, reused, or accepted by the wrong party — and every one
of those must produce the SAME refusal, because a refusal that explains itself is
an oracle.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_claim.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import main
import models
import oem_claims
import tenancy
from database import Base
from security import hash_password

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
ALGO = getattr(auth, "ALGORITHM", "HS256")

failures = []
S = {}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def oem_token(sub, oem, role="OEM_ADMIN"):
    return pyjwt.encode({"sub": sub, "role": role, "principal": "oem", "oem": oem,
                         "exp": datetime.utcnow() + timedelta(hours=1)},
                        auth.SECRET_KEY, algorithm=ALGO)


def fac_token(sub, tenant, role="Admin"):
    return pyjwt.encode({"sub": sub, "role": role, "tenant": tenant,
                         "exp": datetime.utcnow() + timedelta(hours=1)},
                        auth.SECRET_KEY, algorithm=ALGO)


async def _call(path, tok=None, method="GET", body=None):
    raw = [(b"host", b"testserver")]
    payload = b"" if body is None else json.dumps(body).encode()
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    if body is not None:
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    p, _, q = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": p, "raw_path": p.encode(),
             "query_string": q.encode(), "root_path": "", "headers": raw,
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    chunks, status = [], {}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            status["code"] = m["status"]
        elif m["type"] == "http.response.body":
            chunks.append(m.get("body", b""))

    await main.app(scope, receive, send)
    data = b"".join(chunks)
    try:
        return status.get("code"), json.loads(data or b"{}")
    except Exception:
        return status.get("code"), {"_raw": data[:200].decode("utf-8", "replace")}


def GET(p, t=None):
    return asyncio.run(_call(p, t))


def POST(p, t=None, b=None):
    return asyncio.run(_call(p, t, "POST", b or {}))


def unscoped():
    """A session with NO tenant bound, so a test can see every tenant's rows.
    The only way to prove a row landed in the RIGHT place."""
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    return db, tok


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    for mod in ("oem_routes", "connected_equipment_routes", "oem_admin_routes"):
        __import__(mod)
        sys.modules[mod].SessionLocal = SessionLocal

    db, tok = unscoped()
    for code, name in (("OEM_ALPHA", "Alpha"), ("OEM_BETA", "Beta")):
        db.add(models.OemOrganization(oem_code=code, name=name))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha_admin",
                          password=hash_password("x"), role="OEM_ADMIN"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha_view",
                          password=hash_password("x"), role="OEM_VIEWER"))
    db.add(models.OemUser(oem_code="OEM_BETA", username="beta_admin",
                          password=hash_password("x"), role="OEM_ADMIN"))
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        db.add(models.User(username=f"{t.lower()}_admin", password=hash_password("x"),
                           role="Admin", tenant_code=t, is_active=True))
        db.add(models.User(username=f"{t.lower()}_op", password=hash_password("x"),
                           role="Operator", tenant_code=t, is_active=True))
        # Something worth stealing, so a leak would be visible.
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-1",
                                part_number=f"{t}-SECRET-PART", batch_number="B",
                                target_quantity=5, status="Planned"))
    db.flush()
    for oem in ("OEM_ALPHA", "OEM_BETA"):
        m = models.MachineModel(oem_code=oem, family="Compressor",
                                model_code="X200", name=f"{oem} X200",
                                service_interval_hours=2000)
        db.add(m)
        db.flush()
        S[f"model_{oem}"] = m.id
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()


def register(oem_tok, oem, serial):
    code, body = POST("/oem/machines", oem_tok,
                      {"serial_number": serial, "model_id": S[f"model_{oem}"]})
    return code, body


def issue(oem_tok, installation_id, **kw):
    code, body = POST(f"/oem/machines/{installation_id}/claim", oem_tok, kw)
    return code, body


# =====================================================================
def case_registration_is_per_manufacturer():
    print("=" * 74)
    print("1. AN OEM REGISTERS WHAT IT BUILT — INTO NOBODY'S FACTORY")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    beta = oem_token("beta_admin", "OEM_BETA")

    code, body = register(alpha, "OEM_ALPHA", "SN-001")
    check("an OEM can register a machine", code == 200, f"{code} {body}")
    check("...and it belongs to NO factory yet", body.get("factory") is None,
          str(body))
    check("...starting at Manufactured", body.get("status") == "Manufactured",
          str(body))
    S["a1"] = body.get("installation_id")

    code, body = register(alpha, "OEM_ALPHA", "SN-001")
    check("the same serial cannot be registered twice by one OEM", code == 400,
          f"{code} {body}")

    # Serials are unique PER OEM. A global namespace would let one manufacturer
    # discover another's serials by probing for collisions.
    code, body = register(beta, "OEM_BETA", "SN-001")
    check("a DIFFERENT manufacturer may use the same serial", code == 200,
          f"{code} {body}")
    S["b1"] = body.get("installation_id")

    code, body = POST("/oem/machines", alpha,
                      {"serial_number": "SN-X", "model_id": S["model_OEM_BETA"]})
    check("a competitor's model id is a 404", code == 404, f"{code} {body}")

    viewer = oem_token("alpha_view", "OEM_ALPHA", role="OEM_VIEWER")
    code, _ = POST("/oem/machines", viewer,
                   {"serial_number": "SN-V", "model_id": S["model_OEM_ALPHA"]})
    check("a read-only OEM user cannot register a machine", code == 403, str(code))

    for t in ("FACTORY_A", "FACTORY_B"):
        code, _ = register(alpha, "OEM_ALPHA", f"SN-{t}")
        S[f"a_{t}"] = _.get("installation_id")
    code, _ = register(beta, "OEM_BETA", "SN-BETA-B")
    S["b_B"] = _.get("installation_id")
    code, _ = register(beta, "OEM_BETA", "SN-BETA-C")
    S["b_C"] = _.get("installation_id")


def case_an_invitation_attaches_nothing():
    print()
    print("=" * 74)
    print("2. AN INVITATION IS AN OFFER, NOT AN ASSIGNMENT")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")

    code, body = issue(alpha, S["a1"])
    check("an OEM can issue a claim code", code == 200, f"{code} {body}")
    S["code_a1"] = body.get("claim_code")
    S["claim_a1"] = body.get("id")
    check("...and gets the raw code exactly once", bool(S["code_a1"]), str(body))
    check("...with a claim URL for the QR", bool(body.get("claim_url")), str(body))
    check("...and an expiry", bool(body.get("expires_at")), str(body))

    # THE CENTRAL ASSERTION. Issuing attached nothing to anybody.
    db, tok = unscoped()
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == S["a1"]).first()
    claim = db.query(models.MachineClaim).filter(
        models.MachineClaim.id == S["claim_a1"]).first()
    stored = claim.token_hash if claim else ""
    hint = claim.code_hint if claim else ""
    tenancy.reset_current_tenant(tok)
    db.close()
    check("ISSUING A CODE ATTACHED THE MACHINE TO NO FACTORY",
          inst.factory_tenant_code is None, str(inst.factory_tenant_code))

    # The raw code is not recoverable from the database.
    check("the raw code is NOT stored", S["code_a1"] not in stored, "found in the row")
    check("...only its hash", stored == oem_claims.hash_code(S["code_a1"]),
          stored[:16])
    check("...and a 4-character hint, not the code",
          hint == oem_claims.normalise(S["code_a1"])[-4:] and len(hint) == 4, hint)

    code, body = issue(alpha, S["a1"])
    check("a machine cannot have two live invitations at once", code == 400,
          f"{code} {body}")

    code, body = issue(alpha, S["b1"])
    check("an OEM cannot issue a claim for a COMPETITOR's machine", code == 404,
          f"{code} {body}")

    code, body = issue(alpha, S["a1"], expires_in_days=99999)
    check("an absurd expiry is refused", code == 400, f"{code} {body}")


def case_the_factory_is_the_one_who_decides():
    print()
    print("=" * 74)
    print("3. ONLY A FACTORY ADMIN CAN TURN AN OFFER INTO AN INSTALLATION")
    print("=" * 74)
    c = S["code_a1"]

    code, body = GET(f"/connected-equipment/claim/{c}")
    check("an anonymous caller cannot even preview a claim", code in (401, 403),
          f"{code} {body}")
    code, _ = POST(f"/connected-equipment/claim/{c}", None, {"grants": []})
    check("...nor accept one", code in (401, 403), str(code))

    op = fac_token("factory_a_op", "FACTORY_A", role="Operator")
    code, _ = GET(f"/connected-equipment/claim/{c}", op)
    check("an Operator cannot preview a claim", code == 403, str(code))
    code, _ = POST(f"/connected-equipment/claim/{c}", op, {"grants": []})
    check("...nor accept one", code == 403, str(code))

    oem = oem_token("alpha_admin", "OEM_ALPHA")
    code, _ = POST(f"/connected-equipment/claim/{c}", oem, {"grants": []})
    check("the ISSUING OEM cannot accept its own invitation", code in (401, 403),
          str(code))

    admin = fac_token("factory_a_admin", "FACTORY_A")
    code, body = GET(f"/connected-equipment/claim/{c}", admin)
    check("a factory Admin can preview", code == 200, f"{code} {body}")
    check("...and is shown who made it", body.get("manufacturer") == "OEM_ALPHA",
          str(body))
    check("...the serial", body.get("serial_number") == "SN-001", str(body))
    check("...the model", body.get("model_code") == "X200", str(body))
    check("...and the sharing vocabulary to choose from",
          len(body.get("available_grants", [])) == 7, str(body.get("available_grants")))

    # PREVIEWING IS NOT CLAIMING. A URL is not consent.
    db, tok = unscoped()
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == S["a1"]).first()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("PREVIEWING ATTACHED NOTHING", inst.factory_tenant_code is None,
          str(inst.factory_tenant_code))


def case_accepting_creates_the_relationship_and_the_consent():
    print()
    print("=" * 74)
    print("4. ACCEPTANCE: THE RELATIONSHIP AND THE CONSENT, TOGETHER")
    print("=" * 74)
    admin = fac_token("factory_a_admin", "FACTORY_A")
    code, body = POST(f"/connected-equipment/claim/{S['code_a1']}", admin,
                      {"grants": ["SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"]})
    check("a factory Admin can accept", code == 200, f"{code} {body}")
    check("...and the machine is now theirs",
          body.get("lifecycle_status") == "Assigned", str(body))
    check("...sharing exactly what they chose",
          body.get("granted") == ["SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"],
          str(body.get("granted")))

    db, tok = unscoped()
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == S["a1"]).first()
    claim = db.query(models.MachineClaim).filter(
        models.MachineClaim.id == S["claim_a1"]).first()
    policy = db.query(models.OemDataSharingPolicy).filter(
        models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
        models.OemDataSharingPolicy.tenant_code == "FACTORY_A").first()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("the installation names the ACCEPTING factory",
          inst.factory_tenant_code == "FACTORY_A", str(inst.factory_tenant_code))
    check("the claim is spent", claim.status == "Claimed", claim.status)
    check("...and records who took it",
          claim.claimed_tenant_code == "FACTORY_A"
          and claim.claimed_by == "factory_a_admin", str(claim.claimed_by))
    check("the consent used the EXISTING sharing policy", policy is not None
          and set(policy.grants.split(",")) ==
          {"SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"},
          str(policy and policy.grants))

    # It appears where the factory expects it.
    code, body = GET("/connected-equipment", admin)
    serials = [e["serial_number"] for e in body.get("equipment", [])]
    check("it shows up under Connected Equipment", "SN-001" in serials, str(serials))

    # And the OEM sees it as assigned.
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    code, body = GET("/oem/fleet", alpha)
    row = [m for m in body.get("machines", []) if m["serial_number"] == "SN-001"]
    check("the OEM sees it assigned to its customer",
          row and row[0]["customer"] == "FACTORY_A", str(row))


def case_a_used_code_is_dead():
    print()
    print("=" * 74)
    print("5. REPLAY, REUSE, AND EVERY OTHER SECOND ATTEMPT")
    print("=" * 74)
    used = S["code_a1"]
    a = fac_token("factory_a_admin", "FACTORY_A")
    b = fac_token("factory_b_admin", "FACTORY_B")

    code, body = POST(f"/connected-equipment/claim/{used}", a, {"grants": []})
    check("the SAME factory cannot replay a spent code", code == 404,
          f"{code} {body}")
    code, body2 = POST(f"/connected-equipment/claim/{used}", b, {"grants": []})
    check("a DIFFERENT factory cannot use a spent code", code == 404, str(code))
    check("...and both get the identical refusal",
          body.get("detail") == body2.get("detail"), str(body))

    code, _ = GET(f"/connected-equipment/claim/{used}", b)
    check("a spent code cannot even be previewed", code == 404, str(code))

    # The sharing policy did not change because somebody tried.
    db, tok = unscoped()
    n = db.query(models.OemDataSharingPolicy).filter(
        models.OemDataSharingPolicy.tenant_code == "FACTORY_B").count()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("a failed attempt created no policy at the attacker's factory", n == 0,
          str(n))


def case_every_refusal_is_the_same_refusal():
    print()
    print("=" * 74)
    print("6. NO ORACLE: EVERY FAILURE LOOKS IDENTICAL")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    a = fac_token("factory_a_admin", "FACTORY_A")

    # An expired claim.
    code, body = register(alpha, "OEM_ALPHA", "SN-EXPIRED")
    exp_inst = body["installation_id"]
    _, body = issue(alpha, exp_inst)
    expired_code = body["claim_code"]
    db, tok = unscoped()
    db.query(models.MachineClaim).filter(
        models.MachineClaim.id == body["id"]).update(
        {"expires_at": datetime.utcnow() - timedelta(seconds=1)})
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    # A revoked claim.
    code, body = register(alpha, "OEM_ALPHA", "SN-REVOKED")
    rev_inst = body["installation_id"]
    _, body = issue(alpha, rev_inst)
    revoked_code = body["claim_code"]
    code, _ = POST(f"/oem/claims/{body['id']}/revoke", alpha)
    check("an OEM can revoke an unused invitation", code == 200, str(code))

    # A claim meant for somebody else.
    code, body = register(alpha, "OEM_ALPHA", "SN-INTENDED")
    _, body = issue(alpha, body["installation_id"], intended_customer="FACTORY_C")
    intended_code = body["claim_code"]

    answers = {}
    for label, c in (("garbage", "AMP-XXXXX-XXXXX-XXXXX"),
                     ("empty", "-"),
                     ("expired", expired_code),
                     ("revoked", revoked_code),
                     ("spent", S["code_a1"]),
                     ("meant for another factory", intended_code)):
        st, bd = POST(f"/connected-equipment/claim/{c}", a, {"grants": []})
        answers[label] = (st, bd.get("detail"))
        check(f"a {label} code is refused", st == 404, f"{st} {bd}")

    distinct = {v for v in answers.values()}
    check("EVERY refusal is byte-identical — status and message",
          len(distinct) == 1, str(answers))

    # THE PREVIEW PATH MUST BE JUST AS UNIFORM. A mutation that made only the
    # preview explain itself survived until this existed: every refusal above was
    # asserted on POST, and GET is the endpoint somebody probing codes would
    # actually use, because it is the one with no side effects.
    previews = {}
    for label, cde in (("garbage", "AMP-XXXXX-XXXXX-XXXXX"),
                       ("expired", expired_code),
                       ("revoked", revoked_code),
                       ("spent", S["code_a1"]),
                       ("meant for another factory", intended_code)):
        st, bd = GET(f"/connected-equipment/claim/{cde}", a)
        previews[label] = (st, bd.get("detail"))
        check(f"PREVIEW of a {label} code is refused", st == 404, f"{st} {bd}")
    check("EVERY preview refusal is byte-identical too",
          len({v for v in previews.values()}) == 1, str(previews))
    check("...and identical to the accept refusal",
          set(previews.values()) == set(answers.values()),
          f"{set(previews.values())} vs {set(answers.values())}")

    # A COMPETITOR'S CLAIM IS NOT AN OEM'S TO WITHDRAW.
    beta = oem_token("beta_admin", "OEM_BETA")
    st, bd = register(alpha, "OEM_ALPHA", "SN-MINE")
    _, bd = issue(alpha, bd["installation_id"])
    alpha_claim_id = bd["id"]
    st, bd = POST(f"/oem/claims/{alpha_claim_id}/revoke", beta)
    check("one manufacturer cannot revoke another's invitation", st == 404,
          f"{st} {bd}")
    st_ghost, _ = POST("/oem/claims/999999/revoke", beta)
    check("...indistinguishably from a claim that does not exist",
          st == st_ghost, f"{st} vs {st_ghost}")
    # CONTROL: the issuer still can, so the refusal is about ownership.
    st, _ = POST(f"/oem/claims/{alpha_claim_id}/revoke", alpha)
    check("CONTROL: the ISSUING manufacturer can revoke it", st == 200, str(st))

    # A CLAIM FOR A MACHINE SOMEBODY ELSE ALREADY TOOK IS DEAD, even though the
    # claim row itself is still Pending. This is the state a race leaves behind,
    # reproduced deterministically by assigning the machine through another path.
    st, bd = register(alpha, "OEM_ALPHA", "SN-TAKEN")
    taken_inst = bd["installation_id"]
    _, bd = issue(alpha, taken_inst)
    stale_code = bd["claim_code"]
    db, tok = unscoped()
    db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == taken_inst).update(
        {"factory_tenant_code": "FACTORY_C", "status": "Assigned"})
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    st, bd = POST(f"/connected-equipment/claim/{stale_code}", a, {"grants": []})
    check("a still-Pending claim for an ALREADY-ASSIGNED machine is refused",
          st == 404, f"{st} {bd}")
    db, tok = unscoped()
    owner = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == taken_inst).first().factory_tenant_code
    tenancy.reset_current_tenant(tok)
    db.close()
    check("...and the machine still belongs to whoever had it",
          owner == "FACTORY_C", str(owner))

    # THE TENANT COMES FROM THE TOKEN, NEVER FROM THE REQUEST. A body field
    # naming somebody else must not redirect the machine — this is the same
    # shape as the OEM-side assignment the whole ADR refuses.
    st, bd = register(alpha, "OEM_ALPHA", "SN-BODYTENANT")
    body_inst = bd["installation_id"]
    _, bd = issue(alpha, body_inst)
    b = fac_token("factory_b_admin", "FACTORY_B")
    st, bd = POST(f"/connected-equipment/claim/{bd['claim_code']}", b,
                  {"grants": [], "tenant": "FACTORY_A",
                   "tenant_code": "FACTORY_A", "factory_tenant_code": "FACTORY_A"})
    check("CONTROL: the poisoned accept was ACCEPTED, so the body was read",
          st == 200, f"{st} {bd}")
    db, tok = unscoped()
    landed = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == body_inst).first().factory_tenant_code
    tenancy.reset_current_tenant(tok)
    db.close()
    check("a tenant in the request body cannot redirect the machine",
          landed == "FACTORY_B", f"landed at {landed}")

    # CONTROL: the intended factory CAN use the one meant for it, so the refusal
    # above is about who is asking and not about the code being broken.
    c = fac_token("factory_c_admin", "FACTORY_C")
    st, bd = POST(f"/connected-equipment/claim/{intended_code}", c, {"grants": []})
    check("CONTROL: the INTENDED factory can accept it", st == 200, f"{st} {bd}")

    # A revoked claim cannot be revived, and revoking twice is refused.
    st, bd = POST(f"/oem/claims/{S['claim_a1']}/revoke", alpha)
    check("a CLAIMED invitation cannot be revoked afterwards", st == 400,
          f"{st} {bd}")


def case_two_manufacturers_three_factories():
    print()
    print("=" * 74)
    print("7. TWO OEMs, THREE FACTORIES, ONE SHARED CUSTOMER")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    beta = oem_token("beta_admin", "OEM_BETA")
    a = fac_token("factory_a_admin", "FACTORY_A")
    b = fac_token("factory_b_admin", "FACTORY_B")
    c = fac_token("factory_c_admin", "FACTORY_C")

    # The legitimate claims: ALPHA -> B, BETA -> B (the shared customer), BETA -> C.
    _, body = issue(alpha, S["a_FACTORY_B"])
    ab_code = body["claim_code"]
    _, body = issue(beta, S["b_B"])
    bb_code = body["claim_code"]
    _, body = issue(beta, S["b_C"])
    bc_code = body["claim_code"]

    # ATTACK FIRST: the wrong factory tries each one.
    for label, code_, wrong in (("ALPHA's B-machine", ab_code, c),
                                ("BETA's B-machine", bb_code, a),
                                ("BETA's C-machine", bc_code, a)):
        st, _ = POST(f"/connected-equipment/claim/{code_}", wrong, {"grants": []})
        # No `intended_customer` was set on these, so a wrong factory CAN take
        # them — which is the honest behaviour of a bearer code and exactly why
        # the code travels physically with the machine. What must NOT happen is
        # a factory taking a machine it was never given the code for.
        check(f"[{label}] a bearer code works for whoever holds it (documented)",
              st in (200, 404), str(st))

    # KEYED BY INSTALLATION ID, NOT BY SERIAL. A serial is unique PER OEM and
    # deliberately not globally — case 1 registers SN-001 for both manufacturers
    # on purpose. The first version of this section built a dict keyed by serial,
    # so one machine silently overwrote the other and the isolation assertions
    # compared the wrong rows. The test made exactly the mistake the design
    # forbids.
    db, tok = unscoped()
    rows = {i.id: i for i in db.query(models.MachineInstallation).all()}
    tenancy.reset_current_tenant(tok)
    db.close()

    assigned = {i: r.factory_tenant_code for i, r in rows.items()
                if r.factory_tenant_code}
    check("every assigned machine has exactly ONE factory",
          all(isinstance(v, str) and v for v in assigned.values()), str(assigned))

    # THE ISOLATION QUESTION. FACTORY_B holds equipment from BOTH manufacturers;
    # neither may see the other's.
    st, body = GET("/oem/fleet", alpha)
    alpha_ids = {m["installation_id"] for m in body.get("machines", [])}
    st, body = GET("/oem/fleet", beta)
    beta_ids = {m["installation_id"] for m in body.get("machines", [])}
    check("ALPHA's fleet and BETA's fleet share no machine",
          not (alpha_ids & beta_ids), str(alpha_ids & beta_ids))
    check("...and ALPHA sees only machines IT registered",
          all(rows[i].oem_code == "OEM_ALPHA" for i in alpha_ids if i in rows),
          str([(i, rows[i].oem_code) for i in alpha_ids if i in rows]))
    check("...and BETA likewise",
          all(rows[i].oem_code == "OEM_BETA" for i in beta_ids if i in rows),
          str([(i, rows[i].oem_code) for i in beta_ids if i in rows]))
    # The identical serial is the point: two DIFFERENT machines, one name.
    dupes = [r for r in rows.values() if r.serial_number == "SN-001"]
    check("CONTROL: SN-001 exists for BOTH manufacturers and they are distinct rows",
          len(dupes) == 2 and {r.oem_code for r in dupes} == {"OEM_ALPHA", "OEM_BETA"},
          str([(r.id, r.oem_code) for r in dupes]))

    # A shared customer does not collapse the boundary.
    st, body = GET("/connected-equipment", b)
    makers = {e["manufacturer"] for e in body.get("equipment", [])}
    check("FACTORY_B may see equipment from both of ITS suppliers",
          makers <= {"OEM_ALPHA", "OEM_BETA"}, str(makers))
    blob = json.dumps(body)
    for secret in ("FACTORY_A-SECRET-PART", "FACTORY_C-SECRET-PART"):
        check(f"...and nothing of another factory's ({secret})",
              secret not in blob)


def case_consent_is_separate_and_minimal():
    print()
    print("=" * 74)
    print("8. CLAIMING IS NOT CONSENTING")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    a = fac_token("factory_a_admin", "FACTORY_A")

    st, body = register(alpha, "OEM_ALPHA", "SN-NOSHARE")
    inst_id = body["installation_id"]
    _, body = issue(alpha, inst_id)
    nos_code = body["claim_code"]

    # Accepting with NO grants is a complete, valid answer: it adds a machine and
    # changes nothing about what is shared.
    def standing():
        db, tok = unscoped()
        row = db.query(models.OemDataSharingPolicy).filter(
            models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
            models.OemDataSharingPolicy.tenant_code == "FACTORY_A").first()
        g = set((row.grants or "").split(",")) - {""} if row else set()
        tenancy.reset_current_tenant(tok)
        db.close()
        return g

    before = standing()
    st, body = POST(f"/connected-equipment/claim/{nos_code}", a, {"grants": []})
    check("a machine can be accepted while granting NOTHING new", st == 200,
          f"{st} {body}")
    # The response reports the STANDING agreement, not just this request's list —
    # that is what the person needs to see. What it must not do is change it.
    check("...and the agreement is exactly what it was", standing() == before,
          f"{before} -> {standing()}")
    check("...with the response reporting that standing agreement",
          set(body.get("granted", [])) == standing(), str(body.get("granted")))

    st, body = GET("/oem/fleet", alpha)
    row = [m for m in body.get("machines", []) if m["serial_number"] == "SN-NOSHARE"]
    check("the OEM sees the machine exists (its own sales record)", row, str(body))
    check("...but is told nothing operational about it",
          row and row[0].get("operating_hours") is None
          and row[0].get("machine_status") is None, str(row))

    # An unknown grant is refused rather than dropped.
    st, body = register(alpha, "OEM_ALPHA", "SN-BADGRANT")
    _, body = issue(alpha, body["installation_id"])
    st, body2 = POST(f"/connected-equipment/claim/{body['claim_code']}", a,
                     {"grants": ["SHARE_EVERYTHING"]})
    check("an unknown grant is REFUSED, not silently dropped", st == 400,
          f"{st} {body2}")

    # ACCEPTING A SECOND MACHINE MUST NOT SILENTLY REVOKE EXISTING CONSENT.
    #
    # THE DEFECT THIS CAUGHT. The policy is keyed (oem, tenant) — it is a
    # RELATIONSHIP-level agreement, not per-machine. The first version of the
    # accept handler OVERWROTE it, so a factory that already shared operating
    # hours with a supplier, then added a second machine from them and left the
    # boxes unticked, silently turned sharing off for the first machine. Nobody
    # asked for that, and nobody would notice until an engineer was dispatched on
    # stale data.
    def policy_for(oem, tenant):
        db, tok = unscoped()
        row = db.query(models.OemDataSharingPolicy).filter(
            models.OemDataSharingPolicy.oem_code == oem,
            models.OemDataSharingPolicy.tenant_code == tenant).first()
        grants = set((row.grants or "").split(",")) - {""} if row else set()
        tenancy.reset_current_tenant(tok)
        db.close()
        return grants

    # FACTORY_A already agreed to two categories with OEM_ALPHA (section 4), and
    # SN-NOSHARE was just accepted above with NO grants.
    after_empty = policy_for("OEM_ALPHA", "FACTORY_A")
    check("a later claim with NO grants does not revoke what was already agreed",
          after_empty >= {"SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"},
          str(after_empty))

    # And ticking a box at claim time ADDS to the agreement.
    st, body = register(alpha, "OEM_ALPHA", "SN-WIDEN")
    _, body = issue(alpha, body["installation_id"])
    st, body = POST(f"/connected-equipment/claim/{body['claim_code']}", a,
                    {"grants": ["SHARE_ALARMS"]})
    check("accepting with a new grant WIDENS the agreement", st == 200,
          f"{st} {body}")
    widened = policy_for("OEM_ALPHA", "FACTORY_A")
    check("...adding the new category", "SHARE_ALARMS" in widened, str(widened))
    check("...and keeping the old ones",
          widened >= {"SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"}, str(widened))

    # ONE FACTORY'S DECISION NEVER MOVES ANOTHER'S. Give FACTORY_C a distinctive
    # agreement, then have FACTORY_A withdraw everything, and check C is untouched
    # — asserted on the actual values, not on an inequality that any two
    # different sets would satisfy.
    st, body = register(alpha, "OEM_ALPHA", "SN-FORC")
    _, body = issue(alpha, body["installation_id"])
    c = fac_token("factory_c_admin", "FACTORY_C")
    st, _ = POST(f"/connected-equipment/claim/{body['claim_code']}", c,
                 {"grants": ["SHARE_TELEMETRY"]})
    check("FACTORY_C accepts a machine and grants telemetry", st == 200, str(st))
    check("...so C's agreement is exactly that",
          policy_for("OEM_ALPHA", "FACTORY_C") == {"SHARE_TELEMETRY"},
          str(policy_for("OEM_ALPHA", "FACTORY_C")))

    # CONTROL: withdrawal still works — it just lives on the deliberate control,
    # not as a side effect of adding equipment.
    st, body = asyncio.run(_call("/connected-equipment/sharing", a, "PUT",
                                 {"oem_code": "OEM_ALPHA", "grants": []}))
    check("CONTROL: the explicit sharing control can still withdraw everything",
          st == 200 and body.get("granted") == [], f"{st} {body}")
    check("...and FACTORY_A's agreement really is gone",
          policy_for("OEM_ALPHA", "FACTORY_A") == set(),
          str(policy_for("OEM_ALPHA", "FACTORY_A")))
    check("...while FACTORY_C's is EXACTLY as it was",
          policy_for("OEM_ALPHA", "FACTORY_C") == {"SHARE_TELEMETRY"},
          str(policy_for("OEM_ALPHA", "FACTORY_C")))


def case_a_machine_moves_only_by_release():
    print()
    print("=" * 74)
    print("9. TRANSFER: THE ONLY PATH FROM ONE FACTORY TO ANOTHER")
    print("=" * 74)
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    a = fac_token("factory_a_admin", "FACTORY_A")
    b = fac_token("factory_b_admin", "FACTORY_B")

    # SN-001 is at FACTORY_A. FACTORY_B must not be able to take it.
    st, body = issue(alpha, S["a1"])
    check("an OEM cannot re-offer a machine that is still installed", st == 400,
          f"{st} {body}")

    st, _ = POST(f"/connected-equipment/{S['a1']}/release", b)
    check("another factory cannot release somebody else's installation",
          st == 404, str(st))
    op = fac_token("factory_a_op", "FACTORY_A", role="Operator")
    st, _ = POST(f"/connected-equipment/{S['a1']}/release", op)
    check("an Operator cannot release equipment", st == 403, str(st))

    st, body = POST(f"/connected-equipment/{S['a1']}/release", a)
    check("the OWNING factory can release it", st == 200, f"{st} {body}")

    db, tok = unscoped()
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == S["a1"]).first()
    audits = [r for r in db.query(models.AuditLog).all()
              if r.action == "installation_released"]
    tenancy.reset_current_tenant(tok)
    db.close()
    check("the machine returns to unassigned stock",
          inst.factory_tenant_code is None and inst.status == "Sold",
          f"{inst.factory_tenant_code} {inst.status}")
    check("...its factory machine link is cleared", inst.machine_id is None,
          str(inst.machine_id))
    check("...and the release is audited with the previous tenant",
          audits and "FACTORY_A" in (audits[-1].details or ""),
          str(audits and audits[-1].details))

    # Now, and only now, it can go to FACTORY_B — by a NEW explicit acceptance.
    st, body = issue(alpha, S["a1"])
    check("the OEM can now issue a fresh invitation", st == 200, f"{st} {body}")
    st, body = POST(f"/connected-equipment/claim/{body['claim_code']}", b,
                    {"grants": ["SHARE_ALARMS"]})
    check("FACTORY_B accepts it EXPLICITLY", st == 200, f"{st} {body}")

    db, tok = unscoped()
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == S["a1"]).first()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("the machine is now FACTORY_B's", inst.factory_tenant_code == "FACTORY_B",
          str(inst.factory_tenant_code))

    # HISTORY SURVIVED THE MOVE. FACTORY_A's record of what happened is intact.
    db, tok = unscoped()
    a_events = [r for r in db.query(models.EventLog).all()
                if r.tenant_code == "FACTORY_A" and "SN-001" in (r.payload or "")]
    tenancy.reset_current_tenant(tok)
    db.close()
    check("FACTORY_A keeps its record of the machine having been there",
          a_events, "the history vanished with the move")


def case_the_history_lands_in_the_right_tenant():
    print()
    print("=" * 74)
    print("10. WHOSE LOG, WHOSE NOTIFICATION")
    print("=" * 74)
    db, tok = unscoped()
    claims = [r for r in db.query(models.EventLog).all()
              if r.event_type == "MachineClaimed"]
    notes = db.query(models.Notification).all()
    tenancy.reset_current_tenant(tok)
    db.close()

    check("acceptance published MachineClaimed", claims, "no event")
    check("no claim event landed in DEFAULT (the founder's workspace)",
          not [r for r in claims if r.tenant_code == "DEFAULT"],
          str([r.tenant_code for r in claims]))
    for r in claims:
        payload = json.loads(r.payload)
        check(f"[{payload['serial_number']}] filed under the ACCEPTING factory",
              r.tenant_code == payload["tenant_code"]
              and not r.tenant_code.startswith("OEM:"), r.tenant_code)

    fac_notes = [n for n in notes if n.notification_type == "equipment_claimed"]
    oem_notes = [n for n in notes if n.notification_type == "installation_accepted"]
    check("the factory was notified", fac_notes, "none")
    check("...in a FACTORY tenant",
          all(not n.tenant_code.startswith("OEM:") for n in fac_notes),
          str([n.tenant_code for n in fac_notes]))
    check("the manufacturer was notified too", oem_notes, "none")
    check("...in ITS OWN sentinel tenant, invisible to every factory",
          all(n.tenant_code.startswith("OEM:") for n in oem_notes),
          str([n.tenant_code for n in oem_notes]))

    blob = " ".join((n.message or "") + (n.title or "") for n in notes)
    for secret in ("SECRET-PART", "WO-1"):
        check(f"no notification leaks {secret!r}", secret not in blob)

    # The OEM can actually read its own.
    alpha = oem_token("alpha_admin", "OEM_ALPHA")
    st, body = GET("/oem/notifications", alpha)
    kinds = {n["type"] for n in body.get("notifications", [])}
    check("an OEM can read its notifications", st == 200, f"{st} {body}")
    check("...and sees its acceptance", "installation_accepted" in kinds, str(kinds))
    check("...and NOT the factory's copy", "equipment_claimed" not in kinds,
          str(kinds))


def case_no_raw_code_is_ever_logged():
    print()
    print("=" * 74)
    print("11. THE CODE APPEARS IN NO LOG, EVER")
    print("=" * 74)
    db, tok = unscoped()
    audits = db.query(models.AuditLog).all()
    events = db.query(models.EventLog).all()
    notes = db.query(models.Notification).all()
    tenancy.reset_current_tenant(tok)
    db.close()

    blob = " ".join((r.details or "") + (r.action or "") for r in audits)
    blob += " ".join((r.payload or "") for r in events)
    blob += " ".join((n.message or "") + (n.title or "") for n in notes)

    leaked = [c for c in (S.get("code_a1"),) if c and c in blob]
    check("no raw claim code appears in any audit, event or notification",
          not leaked, str(leaked))
    # CONTROL: the hint IS recorded, so the check above is not passing simply
    # because nothing was written.
    check("CONTROL: the 4-character hint IS recorded, for support",
          "hint=" in blob, "no hint written — the audit may be empty")
    check("...and claim creation was audited",
          "claim_created" in blob and "claim_accepted" in blob, "missing audit")
    check("...as was a failed attempt", "claim_rejected" in blob, "not audited")


def run_all():
    seed()
    case_registration_is_per_manufacturer()
    case_an_invitation_attaches_nothing()
    case_the_factory_is_the_one_who_decides()
    case_accepting_creates_the_relationship_and_the_consent()
    case_a_used_code_is_dead()
    case_every_refusal_is_the_same_refusal()
    case_two_manufacturers_three_factories()
    case_consent_is_separate_and_minimal()
    case_a_machine_moves_only_by_release()
    case_the_history_lands_in_the_right_tenant()
    case_no_raw_code_is_ever_logged()


def test_machine_claim():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("AN OEM MAY OFFER A MACHINE; ONLY A FACTORY MAY ACCEPT IT")
