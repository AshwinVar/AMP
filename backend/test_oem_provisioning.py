"""Onboarding a manufacturer, and letting it sign in (ADR-0017).

Until these routes existed an OEM could not authenticate AT ALL: `OemUser` rows
were only ever read, by oem_auth.resolve, and every test minted its own JWT by
hand. The security spine was complete and the front door was missing.

Adding a front door adds three failure modes the read-only layer could not have:

    a login route that hands out the WRONG KIND of session
    a user-creation route through which one manufacturer reaches another
    a role change that locks an organisation out of its own account

so those are what this file is mostly about. The full journey is exercised
end-to-end over real HTTP — founder creates the org, provisions the admin, the
admin signs in with the one-time password, rotates it, adds an engineer, and the
engineer signs in — because each step's output is the next step's input and a
mocked seam would hide exactly the mismatch that matters.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_provisioning.py
"""
import ast
import asyncio
import io
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
import oem_auth
import tenancy
from database import Base
from security import hash_password

HERE = os.path.dirname(os.path.abspath(__file__))
_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
ALGO = getattr(auth, "ALGORITHM", "HS256")

failures = []
STATE = {}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def factory_token(sub, tenant, role="Admin"):
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

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await main.app(scope, receive, send)
    data = b"".join(chunks)
    try:
        return status.get("code"), json.loads(data or b"{}")
    except Exception:
        return status.get("code"), {"_raw": data[:200].decode("utf-8", "replace")}


def GET(path, tok=None):
    return asyncio.run(_call(path, tok))


def POST(path, tok=None, body=None):
    return asyncio.run(_call(path, tok, "POST", body or {}))


def PATCH(path, tok=None, body=None):
    return asyncio.run(_call(path, tok, "PATCH", body or {}))


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    for mod in ("oem_routes", "oem_admin_routes", "saas_routes"):
        __import__(mod)
        sys.modules[mod].SessionLocal = SessionLocal

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    # The founder, and an ordinary customer Admin who must never reach any of it.
    db.add(models.User(username="founder", password=hash_password("x"),
                       role="Admin", tenant_code="DEFAULT", is_active=True))
    db.add(models.User(username="factory_a-admin", password=hash_password("x"),
                       role="Admin", tenant_code="FACTORY_A", is_active=True))
    # A pre-existing manufacturer, so the cross-OEM attacks have a real target.
    db.add(models.OemOrganization(oem_code="OEM_RIVAL", name="Rival Systems"))
    db.add(models.OemUser(oem_code="OEM_RIVAL", username="rival_admin",
                          password=hash_password("rival-pass-123"),
                          role="OEM_ADMIN", is_active=True))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()


# =====================================================================
def case_only_the_founder_can_onboard_a_manufacturer():
    print("=" * 74)
    print("1. ONBOARDING IS THE PLATFORM OPERATOR'S ACT, AND NOBODY ELSE'S")
    print("=" * 74)
    founder = factory_token("founder", "DEFAULT")
    customer = factory_token("factory_a-admin", "FACTORY_A")

    code, body = POST("/saas/oems", customer,
                      {"oem_code": "SNEAKY", "name": "Sneaky"})
    check("a CUSTOMER admin cannot create a manufacturer", code == 403,
          f"{code} {body}")
    code, _ = GET("/saas/oems", customer)
    check("...nor list the manufacturers on the platform", code == 403, str(code))

    code, _ = POST("/saas/oems", None, {"oem_code": "ANON", "name": "Anon"})
    check("an unauthenticated caller cannot create one", code in (401, 403), str(code))

    code, body = POST("/saas/oems", founder,
                      {"oem_code": "oem_alpha", "name": "Alpha Compressors",
                       "brand_name": "Alpha Connect", "brand_color": "#0f766e"})
    check("the founder can", code == 200, f"{code} {body}")
    check("...and the code is normalised to upper case",
          body.get("oem_code") == "OEM_ALPHA", str(body.get("oem_code")))
    STATE["alpha_id"] = body.get("id")

    code, body = POST("/saas/oems", founder,
                      {"oem_code": "OEM_ALPHA", "name": "Duplicate"})
    check("a duplicate code is refused", code == 400, f"{code} {body}")


def case_the_manufacturer_code_shape_is_enforced():
    print()
    print("=" * 74)
    print("2. THE CODE BECOMES PART OF THE SENTINEL, SO ITS SHAPE IS CHECKED")
    print("=" * 74)
    founder = factory_token("founder", "DEFAULT")
    # A colon would produce "OEM:A:B" — a sentinel nobody reasoned about, and the
    # whole isolation rests on that string being unmatchable by a factory.
    for bad, why in (("OEM:X", "a colon"), ("has space", "whitespace"),
                     ("A", "one character"), ("*", "a wildcard"),
                     ("X" * 40, "an over-long code")):
        code, body = POST("/saas/oems", founder, {"oem_code": bad, "name": "X"})
        check(f"{why} is refused ({bad!r})", code == 400, f"{code} {body}")

    # CONTROL: the validator is not so strict it refuses ordinary codes.
    code, body = POST("/saas/oems", founder, {"oem_code": "ACME-2", "name": "Acme"})
    check("CONTROL: an ordinary code with a dash is accepted", code == 200,
          f"{code} {body}")
    # Lower case is NORMALISED, not refused — the route upper-cases before
    # validating. An earlier version of this test asserted a refusal and failed;
    # the test was wrong, not the code, and case-folding a code somebody typed
    # is the friendly behaviour.
    code, body = POST("/saas/oems", founder, {"oem_code": "lower", "name": "Lower"})
    check("lower case is normalised rather than refused",
          code == 200 and body.get("oem_code") == "LOWER", f"{code} {body}")


def case_the_first_admin_is_a_bootstrap_not_a_user_factory():
    print()
    print("=" * 74)
    print("3. PROVISIONING THE FIRST ADMINISTRATOR")
    print("=" * 74)
    founder = factory_token("founder", "DEFAULT")
    customer = factory_token("factory_a-admin", "FACTORY_A")
    oem_id = STATE["alpha_id"]

    code, _ = POST(f"/saas/oems/{oem_id}/admin", customer)
    check("a customer admin cannot provision a manufacturer's login",
          code == 403, str(code))

    code, body = POST(f"/saas/oems/{oem_id}/admin", founder)
    check("the founder can", code == 200, f"{code} {body}")
    check("...and the password is returned exactly once, in the response",
          bool(body.get("temporary_password")), str(body))
    check("...with the OEM_ADMIN role", body.get("role") == "OEM_ADMIN", str(body))
    STATE["admin_user"] = body.get("username")
    STATE["admin_pass"] = body.get("temporary_password")

    # The password must be STORED as a hash, never in the clear.
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    row = (db.query(models.OemUser)
             .filter(models.OemUser.username == STATE["admin_user"]).first())
    stored = row.password
    tenancy.reset_current_tenant(tok)
    db.close()
    check("the password is stored hashed, not in the clear",
          stored != STATE["admin_pass"] and stored.startswith("$2"),
          stored[:12])

    # A BOOTSTRAP, not a user factory. Once an admin exists the platform
    # operator stops minting logins into somebody's supplier.
    code, body = POST(f"/saas/oems/{oem_id}/admin", founder)
    check("a SECOND founder-provisioned admin is refused", code == 400,
          f"{code} {body}")
    check("...and the refusal says where further accounts come from",
          "portal" in str(body.get("detail", "")).lower(), str(body))

    code, _ = POST("/saas/oems/99999/admin", founder)
    check("provisioning for a nonexistent manufacturer is a 404", code == 404,
          str(code))


def case_signing_in():
    print()
    print("=" * 74)
    print("4. THE FRONT DOOR")
    print("=" * 74)
    code, body = POST("/oem/login", None, {"username": STATE["admin_user"],
                                           "password": STATE["admin_pass"]})
    check("the provisioned admin can sign in", code == 200, f"{code} {body}")
    STATE["alpha_token"] = body.get("access_token")
    check("...and gets an OEM session", body.get("oem") == "OEM_ALPHA", str(body))
    check("...carrying the branding, so the portal can render itself",
          body.get("branding", {}).get("name") == "Alpha Connect", str(body))

    claims = pyjwt.decode(STATE["alpha_token"], auth.SECRET_KEY, algorithms=[ALGO])
    check("the token declares principal=oem", claims.get("principal") == "oem",
          str(claims))
    check("...names the organisation", claims.get("oem") == "OEM_ALPHA", str(claims))
    # An inert claim that LOOKS authoritative is how somebody later "fixes" the
    # tenancy branch to honour it.
    check("...and carries NO tenant claim at all", "tenant" not in claims, str(claims))

    # Bad credentials must be indistinguishable: a different message for a
    # wrong username hands out a user-enumeration oracle.
    _, wrong_pw = POST("/oem/login", None, {"username": STATE["admin_user"],
                                            "password": "not-the-password"})
    code_nouser, no_user = POST("/oem/login", None,
                                {"username": "nobody_at_all", "password": "x"})
    check("a wrong password and an unknown user give the SAME answer",
          wrong_pw.get("detail") == no_user.get("detail"),
          f"{wrong_pw.get('detail')!r} vs {no_user.get('detail')!r}")
    check("...and that answer is a 401", code_nouser == 401, str(code_nouser))

    # The session actually works.
    code, body = GET("/oem/me", STATE["alpha_token"])
    check("the issued token opens the portal", code == 200, f"{code} {body}")
    check("...as the right manufacturer", body.get("oem_code") == "OEM_ALPHA",
          str(body))

    # And it is NOT a factory session.
    code, _ = GET("/machines", STATE["alpha_token"])
    check("an OEM session is still refused on factory routes", code == 403, str(code))


def case_a_suspended_organisation_cannot_sign_in():
    print()
    print("=" * 74)
    print("5. SUSPENSION TAKES EFFECT AT THE DOOR AND ON THE NEXT REQUEST")
    print("=" * 74)
    founder = factory_token("founder", "DEFAULT")
    oem_id = STATE["alpha_id"]

    code, _ = PATCH(f"/saas/oems/{oem_id}", founder, {"is_active": False})
    check("the founder can suspend a manufacturer", code == 200, str(code))

    code, body = POST("/oem/login", None, {"username": STATE["admin_user"],
                                           "password": STATE["admin_pass"]})
    check("a suspended organisation cannot sign in", code == 403, f"{code} {body}")
    check("...and is told why, not given 'invalid credentials'",
          "suspend" in str(body.get("detail", "")).lower(), str(body))

    # The EXISTING token dies too, because oem_auth re-reads the organisation on
    # every request rather than trusting the claim.
    code, _ = GET("/oem/me", STATE["alpha_token"])
    check("an already-issued token stops working immediately", code == 403, str(code))

    code, _ = PATCH(f"/saas/oems/{oem_id}", founder, {"is_active": True})
    check("reactivation restores it", code == 200, str(code))
    code, _ = GET("/oem/me", STATE["alpha_token"])
    check("...and the same token works again", code == 200, str(code))


def case_a_manufacturer_manages_only_its_own_people():
    print()
    print("=" * 74)
    print("6. USER MANAGEMENT STOPS AT THE ORGANISATION BOUNDARY")
    print("=" * 74)
    alpha = STATE["alpha_token"]

    code, body = POST("/oem/users", alpha,
                      {"username": "alpha_eng", "role": "OEM_SERVICE_ENGINEER"})
    check("an OEM admin can add somebody to its own organisation", code == 200,
          f"{code} {body}")
    STATE["eng_user"] = body.get("username")
    STATE["eng_pass"] = body.get("temporary_password")
    check("...with a one-time password", bool(STATE["eng_pass"]), str(body))

    # THE ATTACK. oem_code appears nowhere in the request model, so there is no
    # input through which one manufacturer creates an account inside another.
    code, body = POST("/oem/users", alpha,
                      {"username": "planted", "role": "OEM_ADMIN",
                       "oem_code": "OEM_RIVAL", "oem": "OEM_RIVAL"})
    check("an oem_code in the body cannot place a user in another organisation",
          code == 200, f"{code} {body}")
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    planted = db.query(models.OemUser).filter(
        models.OemUser.username == "planted").first()
    rival_users = db.query(models.OemUser).filter(
        models.OemUser.oem_code == "OEM_RIVAL").count()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("...the account landed in the CALLER's organisation",
          planted is not None and planted.oem_code == "OEM_ALPHA",
          str(planted and planted.oem_code))
    check("...and the rival's roster is unchanged", rival_users == 1,
          str(rival_users))

    # The roster read is filtered too.
    code, body = GET("/oem/users", alpha)
    names = sorted(u["username"] for u in body.get("users", []))
    check("the roster lists only this manufacturer's people",
          "rival_admin" not in names, str(names))

    # A rival's user id is a 404, never a 403 — a 403 would confirm the row and
    # turn id probing into a roster of a competitor's staff.
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    rival_id = db.query(models.OemUser).filter(
        models.OemUser.username == "rival_admin").first().id
    tenancy.reset_current_tenant(tok)
    db.close()
    code, _ = PATCH(f"/oem/users/{rival_id}", alpha, {"is_active": False})
    code_ghost, _ = PATCH("/oem/users/999999", alpha, {"is_active": False})
    check("a competitor's user id is refused", code == 404, str(code))
    check("...indistinguishably from a nonexistent one", code == code_ghost,
          f"{code} vs {code_ghost}")

    # An unknown role would authenticate and be able to do nothing.
    code, body = POST("/oem/users", alpha,
                      {"username": "weird", "role": "Admin"})
    check("a FACTORY role string is refused, not stored", code == 400,
          f"{code} {body}")
    code, body = POST("/oem/users", alpha,
                      {"username": "weird2", "role": "SUPERUSER"})
    check("an invented role is refused", code == 400, f"{code} {body}")

    # Username collision across the two principal tables.
    code, body = POST("/oem/users", alpha,
                      {"username": "factory_a-admin", "role": "OEM_VIEWER"})
    check("a username already used by a FACTORY login is refused", code == 400,
          f"{code} {body}")
    code, body = POST("/oem/users", alpha,
                      {"username": "rival_admin", "role": "OEM_VIEWER"})
    check("...and one already used by another manufacturer", code == 400,
          f"{code} {body}")


def case_capabilities_gate_user_management():
    print()
    print("=" * 74)
    print("7. AN ENGINEER IS NOT AN ADMINISTRATOR")
    print("=" * 74)
    code, body = POST("/oem/login", None, {"username": STATE["eng_user"],
                                           "password": STATE["eng_pass"]})
    check("the provisioned engineer can sign in", code == 200, f"{code} {body}")
    eng = body.get("access_token")
    STATE["eng_token"] = eng

    code, _ = GET("/oem/fleet", eng)
    check("an engineer can read the fleet", code == 200, str(code))
    code, _ = GET("/oem/users", eng)
    check("an engineer cannot read the roster", code == 403, str(code))
    code, _ = POST("/oem/users", eng, {"username": "x2", "role": "OEM_ADMIN"})
    check("...nor create a user", code == 403, str(code))
    code, _ = POST("/saas/oems", eng, {"oem_code": "SELFMADE", "name": "Self"})
    check("...nor create an organisation on the platform", code in (401, 403),
          str(code))


def case_nobody_can_lock_the_organisation_out():
    print()
    print("=" * 74)
    print("8. AN ORGANISATION CANNOT BE LEFT WITHOUT AN ADMINISTRATOR")
    print("=" * 74)
    alpha = STATE["alpha_token"]
    code, body = GET("/oem/users", alpha)
    users = {u["username"]: u for u in body.get("users", [])}

    # ESTABLISH THE PREMISE RATHER THAN ASSUMING IT. The body-injection attack in
    # section 6 planted an OEM_ADMIN — in the CALLER's own organisation, which is
    # the point of that test — so this organisation has two administrators, not
    # one. The first version of this section assumed "sole administrator", the
    # guard correctly did not fire, and the failure was the fixture's.
    #
    # Disabling it is also the CONTROL the next assertions need: with two
    # administrators, one of them may be switched off.
    admins = [u for u in users.values()
              if u["role"] == "OEM_ADMIN" and u["is_active"]]
    check("the organisation currently has TWO administrators", len(admins) == 2,
          str([u["username"] for u in admins]))
    code, body = PATCH(f"/oem/users/{users['planted']['id']}", alpha,
                       {"is_active": False})
    check("CONTROL: with two administrators, one may be switched off",
          code == 200, f"{code} {body}")

    code, body = GET("/oem/users", alpha)
    users = {u["username"]: u for u in body.get("users", [])}
    me = users[STATE["admin_user"]]

    # NOW there is exactly one, and they are acting on themselves. This is the
    # ONLY way the guard can be reached — writing this test is what showed that
    # an earlier "you cannot demote yourself" rule made the organisation-level
    # rule dead code, because any OTHER caller with manage_users is themselves a
    # remaining admin.
    code, body = PATCH(f"/oem/users/{me['id']}", alpha, {"role": "OEM_VIEWER"})
    check("the sole administrator cannot demote themselves", code == 400,
          f"{code} {body}")
    check("...and the refusal explains the consequence",
          "lock" in str(body.get("detail", "")).lower(), str(body))
    code, body = PATCH(f"/oem/users/{me['id']}", alpha, {"is_active": False})
    check("...nor switch their own account off", code == 400, f"{code} {body}")

    # CONTROL: the guard is about the ORGANISATION's state, not a blanket ban on
    # touching an admin. With a second administrator in place, a handover is a
    # normal thing to do and is allowed.
    code, body = POST("/oem/users", alpha,
                      {"username": "alpha_admin2", "role": "OEM_ADMIN"})
    check("a second administrator can be added", code == 200, f"{code} {body}")
    second_pass = body.get("temporary_password")
    code, body = POST("/oem/login", None, {"username": "alpha_admin2",
                                           "password": second_pass})
    second = body.get("access_token")
    check("...and can sign in", code == 200, f"{code} {body}")

    code, body = PATCH(f"/oem/users/{me['id']}", second, {"role": "OEM_VIEWER"})
    check("CONTROL: with two admins, one may demote the other", code == 200,
          f"{code} {body}")

    # ...and now the remaining one is sole again, so the guard returns.
    code, body = GET("/oem/users", second)
    users = {u["username"]: u for u in body.get("users", [])}
    last = users["alpha_admin2"]
    code, body = PATCH(f"/oem/users/{last['id']}", second, {"is_active": False})
    check("the last remaining administrator is protected again", code == 400,
          f"{code} {body}")

    # And a handover: promote somebody, THEN step down.
    eng = users[STATE["eng_user"]]
    code, _ = PATCH(f"/oem/users/{eng['id']}", second, {"role": "OEM_ADMIN"})
    check("CONTROL: promoting a successor is allowed", code == 200, str(code))
    code, body = PATCH(f"/oem/users/{last['id']}", second, {"is_active": False})
    check("CONTROL: ...and then the outgoing admin CAN step down", code == 200,
          f"{code} {body}")

    # Leave a working administrator behind for the suites that follow.
    STATE["handover_admin"] = STATE["eng_user"]


def case_rotating_the_temporary_password():
    print()
    print("=" * 74)
    print("9. THE ONE-TIME PASSWORD IS MEANT TO BE REPLACED")
    print("=" * 74)
    eng = STATE["eng_token"]

    code, body = POST("/oem/change-password", eng,
                      {"current_password": "wrong", "new_password": "a-long-enough-1"})
    check("the current password must be correct", code == 401, f"{code} {body}")

    code, body = POST("/oem/change-password", eng,
                      {"current_password": STATE["eng_pass"], "new_password": "short"})
    check("a too-short new password is refused", code == 400, f"{code} {body}")

    code, body = POST("/oem/change-password", eng,
                      {"current_password": STATE["eng_pass"],
                       "new_password": "a-proper-new-password"})
    check("a valid rotation succeeds", code == 200, f"{code} {body}")

    code, _ = POST("/oem/login", None, {"username": STATE["eng_user"],
                                        "password": STATE["eng_pass"]})
    check("the OLD password stops working", code == 401, str(code))
    code, body = POST("/oem/login", None, {"username": STATE["eng_user"],
                                           "password": "a-proper-new-password"})
    check("...and the new one works", code == 200, str(code))
    # This account was PROMOTED to administrator during the handover in section
    # 8, so it — not the original admin token, which section 8 demoted — is the
    # organisation's current administrator. Using the stale token here is how
    # this block first failed, with a 403 that said nothing about the guard under
    # test.
    admin = body.get("access_token")

    # A DISABLED ACCOUNT MUST BE REFUSED AT THE DOOR. oem_auth.resolve refuses it
    # on every request too, so this login check is defence in depth — but only
    # this one gives the person a usable answer instead of a token that fails on
    # its first use. A mutation removing it survived until this existed.
    code, body = POST("/oem/users", admin,
                      {"username": "alpha_temp", "role": "OEM_VIEWER"})
    check("a temporary account can be created", code == 200, f"{code} {body}")
    temp_pass, temp_id = body.get("temporary_password"), body.get("id")
    code, _ = POST("/oem/login", None, {"username": "alpha_temp",
                                        "password": temp_pass})
    check("CONTROL: it can sign in while active", code == 200, str(code))

    code, _ = PATCH(f"/oem/users/{temp_id}", admin, {"is_active": False})
    check("...and can be switched off", code == 200, str(code))
    code, body = POST("/oem/login", None, {"username": "alpha_temp",
                                           "password": temp_pass})
    check("a DISABLED account cannot sign in, even with the right password",
          code == 403, f"{code} {body}")
    check("...and is told the account is disabled, not 'invalid credentials'",
          "disabled" in str(body.get("detail", "")).lower(), str(body))

    # Nobody can rotate anybody else's: the account comes from the principal and
    # there is no parameter for another user.
    src = io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read()
    body_src = src.split("def oem_change_password", 1)[1].split("\n@router", 1)[0]
    check("the change-password handler takes its account from the principal",
          'principal["username"]' in body_src, "reads a username from elsewhere")
    # Parsed with AST rather than by splitting on "class" — OemPasswordChange is
    # the LAST class in the file, so the naive split ran to end-of-file and
    # matched "username" in unrelated handlers. The test failed and the code was
    # fine, which is its own kind of wrong.
    fields = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "OemPasswordChange":
            fields = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
    check("the change-password request model exists and was parsed", fields,
          "OemPasswordChange not found")
    check("...and names no user: only the two passwords",
          fields == ["current_password", "new_password"], str(fields))


def case_login_is_the_only_unauthenticated_oem_route():
    print()
    print("=" * 74)
    print("10. THE EXCEPTION THAT MUST NOT BE COPIED BY ACCIDENT")
    print("=" * 74)
    src = io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    unguarded = []
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routed = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                     and d.func.attr in ("get", "post", "put", "patch", "delete")
                     for d in node.decorator_list)
        if not routed:
            continue
        total += 1
        seg = ast.get_source_segment(src, node) or ""
        if "require_oem" not in seg:
            unguarded.append(node.name)

    check("the scan found the OEM handlers", total >= 15, str(total))
    check("EXACTLY ONE /oem route is unauthenticated, and it is the login",
          unguarded == ["oem_login"], str(unguarded))

    # The founder-side module must be the mirror image: no OEM principal may
    # reach any of it.
    admin_src = io.open(os.path.join(HERE, "oem_admin_routes.py"),
                        encoding="utf-8").read()
    check("no founder-side route uses the OEM authenticator",
          "require_oem" not in admin_src, "an OEM principal can reach onboarding")
    admin_tree = ast.parse(admin_src)
    for node in ast.walk(admin_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr in ("get", "post", "patch") for d in node.decorator_list):
            seg = ast.get_source_segment(admin_src, node) or ""
            check(f"{node.name}() demands the founder workspace",
                  "_require_founder" in seg, "no founder gate")

    # And the front door is throttled like the factory's.
    import http_security
    check("/oem/login is rate limited", http_security._limit_for("/oem/login"),
          "no throttle on the manufacturer's front door")


def run_all():
    seed()
    case_only_the_founder_can_onboard_a_manufacturer()
    case_the_manufacturer_code_shape_is_enforced()
    case_the_first_admin_is_a_bootstrap_not_a_user_factory()
    case_signing_in()
    case_a_suspended_organisation_cannot_sign_in()
    case_a_manufacturer_manages_only_its_own_people()
    case_capabilities_gate_user_management()
    case_nobody_can_lock_the_organisation_out()
    case_rotating_the_temporary_password()
    case_login_is_the_only_unauthenticated_oem_route()


def test_oem_provisioning():
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
    print("A MANUFACTURER CAN BE ONBOARDED, SIGN IN, AND MANAGE ONLY ITS OWN PEOPLE")
