"""The /oem API, attacked over HTTP (ADR-0017).

The unit suites prove the sharing rules. This drives the ROUTES — through the
real dependencies, with real tokens — because a rule that holds in a function and
is bypassed by a query parameter is not a rule.

Driven at the ASGI layer rather than through Starlette's TestClient: TestClient
needs httpx, which CI does not install and which would have to become a RUNTIME
dependency to serve a test. The repo already made the opposite call for
pytest-cov (see .coveragerc).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_routes.py
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import main
import models
import oem_sharing
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
ALGO = getattr(auth, "ALGORITHM", "HS256")

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def token(sub, oem=None, tenant=None, role="OEM_ADMIN", principal="oem"):
    c = {"sub": sub, "role": role, "exp": datetime.utcnow() + timedelta(hours=1)}
    if principal:
        c["principal"] = principal
    if oem:
        c["oem"] = oem
    if tenant:
        c["tenant"] = tenant
    return pyjwt.encode(c, auth.SECRET_KEY, algorithm=ALGO)


async def call(path, tok=None, headers=None):
    """One real HTTP request through the whole middleware stack."""
    raw = [(b"host", b"testserver")]
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    for k, v in (headers or {}).items():
        raw.append((k.encode(), v.encode()))
    path_only, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path_only,
             "raw_path": path_only.encode(), "query_string": query.encode(),
             "root_path": "", "headers": raw,
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    chunks, status = [], {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await main.app(scope, receive, send)
    body = b"".join(chunks)
    try:
        return status.get("code"), json.loads(body or b"{}")
    except Exception:
        return status.get("code"), {"_raw": body[:200].decode("utf-8", "replace")}


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    import oem_routes
    oem_routes.SessionLocal = SessionLocal
    import oem_auth as _oa
    _oa_db = SessionLocal

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    for code, name in (("OEM_ALPHA", "Alpha"), ("OEM_BETA", "Beta")):
        db.add(models.OemOrganization(oem_code=code, name=name,
                                      brand_name=f"{name} Connect"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-viewer",
                          password="x", role="OEM_VIEWER"))
    db.add(models.OemUser(oem_code="OEM_BETA", username="beta-admin",
                          password="x", role="OEM_ADMIN"))
    db.add(models.User(username="factory-admin", password="x", role="Admin",
                       tenant_code="FACTORY_A", is_active=True))
    db.flush()
    mods = {}
    for code in ("OEM_ALPHA", "OEM_BETA"):
        m = models.MachineModel(oem_code=code, family="Compressor",
                                model_code="X200", name=f"{code} X200")
        db.add(m)
        db.flush()
        mods[code] = m
    machines = {}
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        mc = models.Machine(tenant_code=t, site="Plant 1", name="COMP-001",
                            status="Running", utilization=71)
        db.add(mc)
        db.flush()
        machines[t] = mc
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001",
                                part_number="SECRET-PART", batch_number="B1",
                                target_quantity=10, status="Planned"))
    ids = {}
    for oem, tenant, serial in (("OEM_ALPHA", "FACTORY_A", "SN-A1"),
                                ("OEM_ALPHA", "FACTORY_B", "SN-A2"),
                                ("OEM_BETA", "FACTORY_B", "SN-B1"),
                                ("OEM_BETA", "FACTORY_C", "SN-B2")):
        row = models.MachineInstallation(
            oem_code=oem, serial_number=serial, model_id=mods[oem].id,
            factory_tenant_code=tenant, site="Plant 1",
            machine_id=machines[tenant].id, status="Active",
            operating_hours=1850.0, last_seen_at=datetime.utcnow(),
            warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1))
        db.add(row)
        db.flush()
        ids[serial] = row.id
    db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA",
                                       tenant_code="FACTORY_A",
                                       grants="SHARE_OPERATING_HOURS"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return ids


async def run_all():
    ids = seed()
    print("=" * 74)
    print("1. WHO MAY CALL /oem AT ALL")
    print("=" * 74)
    for label, tok, expect in [
        ("no token", None, 401),
        ("a FACTORY admin's token", token("factory-admin", tenant="FACTORY_A",
                                          role="Admin", principal=None), 401),
        ("a token claiming an OEM it does not belong to",
         token("beta-admin", oem="OEM_ALPHA"), 403),
        ("a garbage token", "not-a-jwt", 401),
    ]:
        code, _ = await call("/oem/fleet", tok)
        check(f"{label} -> {expect}", code == expect, str(code))
    code, body = await call("/oem/fleet", token("alpha-admin", oem="OEM_ALPHA"))
    check("CONTROL: a real OEM admin gets its fleet", code == 200, str(code))

    print()
    print("=" * 74)
    print("2. THE FLEET IS THIS MANUFACTURER'S, AND ONLY ITS OWN")
    print("=" * 74)
    serials = sorted(m["serial_number"] for m in body.get("machines", []))
    check("OEM_ALPHA sees exactly its own two machines",
          serials == ["SN-A1", "SN-A2"], str(serials))
    code, beta = await call("/oem/fleet", token("beta-admin", oem="OEM_BETA"))
    bser = sorted(m["serial_number"] for m in beta.get("machines", []))
    check("OEM_BETA sees exactly its own two machines",
          bser == ["SN-B1", "SN-B2"], str(bser))

    # `customer` NARROWS; it can never widen.
    code, only_c = await call("/oem/fleet?customer=FACTORY_C",
                              token("alpha-admin", oem="OEM_ALPHA"))
    check("asking for a customer it does not serve returns an EMPTY fleet",
          only_c.get("total") == 0, str(only_c)[:140])

    # The attack that matters: pass a COMPETITOR'S OEM CODE as the customer. If
    # the handler ever took its scope from the parameter instead of the
    # principal, this returns OEM_BETA's entire fleet. Passing a tenant code
    # (above) does NOT catch that — it filters to nothing by coincidence.
    code, hijack = await call("/oem/fleet?customer=OEM_BETA",
                              token("alpha-admin", oem="OEM_ALPHA"))
    serials_seen = {m["serial_number"] for m in hijack.get("machines", [])}
    check("passing a COMPETITOR'S OEM CODE as `customer` returns nothing",
          hijack.get("total") == 0 and not (serials_seen & {"SN-B1", "SN-B2"}),
          str(hijack)[:160])

    # The catalogue is the same boundary: models belong to a manufacturer.
    code, cat = await call("/oem/models", token("alpha-admin", oem="OEM_ALPHA"))
    names = [m["name"] for m in cat]
    check("the catalogue holds only this manufacturer's models",
          all("OEM_ALPHA" in n for n in names) and names, str(names))
    code, bcat = await call("/oem/models", token("beta-admin", oem="OEM_BETA"))
    bnames = [m["name"] for m in bcat]
    check("...and the other manufacturer sees only its own",
          all("OEM_BETA" in n for n in bnames) and bnames, str(bnames))

    print()
    print("=" * 74)
    print("3. A COMPETITOR'S MACHINE IS A 404, NOT A 403")
    print("=" * 74)
    code, _ = await call(f"/oem/machines/{ids['SN-B1']}",
                         token("alpha-admin", oem="OEM_ALPHA"))
    check("another manufacturer's installation id is 404", code == 404, str(code))
    code, _ = await call("/oem/machines/999999",
                         token("alpha-admin", oem="OEM_ALPHA"))
    check("an id that does not exist is the SAME 404 (no oracle)", code == 404,
          str(code))
    code, own = await call(f"/oem/machines/{ids['SN-A1']}",
                           token("alpha-admin", oem="OEM_ALPHA"))
    check("CONTROL: its own installation resolves", code == 200, str(code))

    print()
    print("=" * 74)
    print("4. THE SHARING POLICY IS APPLIED OVER HTTP")
    print("=" * 74)
    check("FACTORY_A granted hours, so hours are present",
          own.get("operating_hours") == 1850.0, str(own.get("operating_hours")))
    check("...and machine status was NOT granted, so it is absent",
          own.get("machine_status") is None, str(own.get("machine_status")))
    check("...and what is not shared is stated, not silently omitted",
          "SHARE_MACHINE_HEALTH" in (own.get("not_shared") or []),
          str(own.get("not_shared")))
    a2 = [m for m in body.get("machines", []) if m["serial_number"] == "SN-A2"]
    check("FACTORY_B granted nothing, so its machine shows no hours",
          a2 and a2[0]["operating_hours"] is None, str(a2)[:140])

    print()
    print("=" * 74)
    print("5. NO /oem RESPONSE CARRIES FACTORY OPERATIONAL DATA")
    print("=" * 74)
    leaked = []
    for path in ("/oem/me", "/oem/models", "/oem/fleet", "/oem/customers",
                 "/oem/sharing", f"/oem/machines/{ids['SN-A1']}"):
        code, payload = await call(path, token("alpha-admin", oem="OEM_ALPHA"))
        blob = json.dumps(payload)
        for secret in ("SECRET-PART", "WO-001"):
            if secret in blob:
                leaked.append(f"{path} -> {secret}")
    check("no OEM endpoint returns a work order or part number", not leaked,
          str(leaked))

    print()
    print("=" * 74)
    print("6. AN OEM TOKEN CANNOT REACH A FACTORY ROUTE")
    print("=" * 74)
    for path in ("/machines", "/work-orders", "/inventory/items"):
        code, payload = await call(path, token("alpha-admin", oem="OEM_ALPHA"))
        empty = payload in ([], {}) or (isinstance(payload, list) and not payload)
        check(f"{path} returns nothing for an OEM token",
              code in (401, 403) or empty, f"{code} {str(payload)[:90]}")

    # And the X-Tenant attack, over real HTTP this time.
    code, payload = await call("/machines", token("alpha-admin", oem="OEM_ALPHA"),
                               headers={"x-tenant": "FACTORY_A"})
    empty = isinstance(payload, list) and not payload
    check("an OEM token + X-Tenant: FACTORY_A still reads no machines",
          code in (401, 403) or empty, f"{code} {str(payload)[:90]}")


def test_oem_routes():
    asyncio.new_event_loop().run_until_complete(run_all())
    assert not failures, failures


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(run_all())
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE OEM API SERVES ONLY ITS OWN FLEET, ONLY WHAT WAS SHARED")
