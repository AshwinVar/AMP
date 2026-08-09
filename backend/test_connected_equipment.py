"""The factory sees, and controls, what its manufacturers can see (ADR-0017).

A consent control nobody can read is not consent, and a consent control the
grantee can edit is not consent either. This suite pins both halves:

  * a factory administrator can see which machines came from an OEM, who made
    them, and exactly which fields that OEM currently receives;
  * only the FACTORY can change it — an OEM principal cannot reach this surface
    at all, let alone widen its own permissions.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_connected_equipment.py
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


def token(sub, tenant=None, oem=None, role="Admin", principal=None):
    c = {"sub": sub, "role": role, "exp": datetime.utcnow() + timedelta(hours=1)}
    if tenant:
        c["tenant"] = tenant
    if oem:
        c["oem"] = oem
    if principal:
        c["principal"] = principal
    return pyjwt.encode(c, auth.SECRET_KEY, algorithm=ALGO)


async def call(path, tk=None, method="GET", body=None):
    raw = [(b"host", b"testserver")]
    if tk:
        raw.append((b"authorization", f"Bearer {tk}".encode()))
    payload = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    p, _, q = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": p, "raw_path": p.encode(),
             "query_string": q.encode(), "root_path": "", "headers": raw,
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    out, status, sent = [], {}, {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out.append(msg.get("body", b""))

    await main.app(scope, receive, send)
    blob = b"".join(out)
    try:
        return status.get("code"), json.loads(blob or b"{}")
    except Exception:
        return status.get("code"), {"_raw": blob[:160].decode("utf-8", "replace")}


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    for mod in ("oem_routes", "connected_equipment_routes"):
        __import__(mod)
        sys.modules[mod].SessionLocal = SessionLocal

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha Compressors",
                                  support_email="service@alpha.example"))
    db.add(models.OemOrganization(oem_code="OEM_BETA", name="Beta Systems"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN"))
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.User(username=f"{t.lower()}-admin", password="x",
                           role="Admin", tenant_code=t, is_active=True))
        db.add(models.User(username=f"{t.lower()}-op", password="x",
                           role="Operator", tenant_code=t, is_active=True))
    db.flush()
    mods = {}
    for code in ("OEM_ALPHA", "OEM_BETA"):
        m = models.MachineModel(oem_code=code, family="Compressor",
                                model_code="X200", name=f"{code} X200",
                                service_interval_hours=2000)
        db.add(m)
        db.flush()
        mods[code] = m
    machines = {}
    for t in ("FACTORY_A", "FACTORY_B"):
        mc = models.Machine(tenant_code=t, site="Plant 1", name="COMP-001",
                            status="Running", utilization=70)
        db.add(mc)
        db.flush()
        machines[t] = mc
    for oem, t, serial in (("OEM_ALPHA", "FACTORY_A", "SN-A1"),
                           ("OEM_BETA", "FACTORY_A", "SN-B1"),
                           ("OEM_ALPHA", "FACTORY_B", "SN-A2")):
        db.add(models.MachineInstallation(
            oem_code=oem, serial_number=serial, model_id=mods[oem].id,
            factory_tenant_code=t, site="Plant 1", machine_id=machines[t].id,
            status="Active", operating_hours=1950.0,
            warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()


async def run_all():
    seed()
    fac_a = token("factory_a-admin", tenant="FACTORY_A")
    fac_a_op = token("factory_a-op", tenant="FACTORY_A", role="Operator")
    fac_b = token("factory_b-admin", tenant="FACTORY_B")
    oem = token("alpha-admin", oem="OEM_ALPHA", role="OEM_ADMIN", principal="oem")

    print("=" * 74)
    print("1. THE FACTORY SEES ITS CONNECTED EQUIPMENT")
    print("=" * 74)
    code, body = await call("/connected-equipment", fac_a)
    check("a factory admin can read it", code == 200, str(code))
    serials = sorted(e["serial_number"] for e in body["equipment"])
    check("FACTORY_A sees BOTH manufacturers' machines on its floor",
          serials == ["SN-A1", "SN-B1"], str(serials))
    check("...and not FACTORY_B's machine", "SN-A2" not in serials, str(serials))
    makers = sorted(m["oem_code"] for m in body["manufacturers"])
    check("both manufacturers are named", makers == ["OEM_ALPHA", "OEM_BETA"],
          str(makers))
    check("...with a human name and support contact",
          any(m["name"] == "Alpha Compressors" for m in body["manufacturers"]),
          str(body["manufacturers"]))
    check("the FULL grant vocabulary is offered, with labels",
          len(body["available_grants"]) == 7
          and all(g["label"] for g in body["available_grants"]),
          str(body["available_grants"])[:120])
    check("nothing is shared yet, and it says so",
          all(e["shared_with_manufacturer"] == [] for e in body["equipment"]),
          str([e["shared_with_manufacturer"] for e in body["equipment"]]))
    check("service and warranty position are shown to the OWNER",
          all(e["service"]["state"] and e["warranty"]["state"]
              for e in body["equipment"]))

    code, other = await call("/connected-equipment", fac_b)
    bser = sorted(e["serial_number"] for e in other["equipment"])
    check("FACTORY_B sees only its own equipment", bser == ["SN-A2"], str(bser))

    print()
    print("=" * 74)
    print("2. ONLY THE FACTORY MAY CHANGE WHAT IS SHARED")
    print("=" * 74)
    code, _ = await call("/connected-equipment", oem)
    check("an OEM principal cannot even READ this surface", code in (401, 403),
          str(code))
    code, _ = await call("/connected-equipment/sharing", oem, "PUT",
                         {"oem_code": "OEM_ALPHA",
                          "grants": ["SHARE_OPERATING_HOURS"]})
    check("an OEM principal cannot widen its OWN permissions",
          code in (401, 403), str(code))

    code, _ = await call("/connected-equipment/sharing", fac_a_op, "PUT",
                         {"oem_code": "OEM_ALPHA", "grants": ["SHARE_ALARMS"]})
    check("a factory OPERATOR cannot change sharing", code == 403, str(code))

    code, body = await call("/connected-equipment/sharing", fac_a, "PUT",
                            {"oem_code": "OEM_ALPHA",
                             "grants": ["SHARE_OPERATING_HOURS"]})
    check("a factory ADMIN can grant", code == 200 and
          body.get("granted") == ["SHARE_OPERATING_HOURS"], f"{code} {body}")

    print()
    print("=" * 74)
    print("3. A GRANT IS NARROW, AND VISIBLE AFTERWARDS")
    print("=" * 74)
    code, body = await call("/connected-equipment", fac_a)
    alpha_row = [e for e in body["equipment"] if e["serial_number"] == "SN-A1"][0]
    beta_row = [e for e in body["equipment"] if e["serial_number"] == "SN-B1"][0]
    check("the granted manufacturer's row shows the grant",
          alpha_row["shared_with_manufacturer"] == ["SHARE_OPERATING_HOURS"],
          str(alpha_row["shared_with_manufacturer"]))
    check("the OTHER manufacturer on the same floor still gets nothing",
          beta_row["shared_with_manufacturer"] == [],
          str(beta_row["shared_with_manufacturer"]))

    # The OEM now sees exactly that one field, and no more.
    code, fleet = await call("/oem/fleet", oem)
    a1 = [m for m in fleet["machines"] if m["serial_number"] == "SN-A1"][0]
    a2 = [m for m in fleet["machines"] if m["serial_number"] == "SN-A2"][0]
    check("the OEM now receives operating hours at FACTORY_A",
          a1["operating_hours"] == 1950.0, str(a1["operating_hours"]))
    check("...but still not machine status", a1["machine_status"] is None,
          str(a1["machine_status"]))
    check("...and FACTORY_B, which granted nothing, still shares nothing",
          a2["operating_hours"] is None, str(a2["operating_hours"]))

    print()
    print("=" * 74)
    print("4. BAD GRANTS ARE REFUSED, NOT SILENTLY DROPPED")
    print("=" * 74)
    code, body = await call("/connected-equipment/sharing", fac_a, "PUT",
                            {"oem_code": "OEM_ALPHA",
                             "grants": ["SHARE_OPERATING_HOURS", "SHARE_EVERYTHING"]})
    check("an unknown grant key is a 400, not a quiet drop", code == 400, str(code))
    check("...and the message names the offending key",
          "SHARE_EVERYTHING" in str(body), str(body))
    code, after = await call("/connected-equipment", fac_a)
    still = [e for e in after["equipment"]
             if e["serial_number"] == "SN-A1"][0]["shared_with_manufacturer"]
    check("...and the previous grant was left untouched",
          still == ["SHARE_OPERATING_HOURS"], str(still))

    code, _ = await call("/connected-equipment/sharing", fac_a, "PUT",
                         {"oem_code": "OEM_NOWHERE", "grants": []})
    check("granting to a manufacturer with no equipment here is a 404",
          code == 404, str(code))

    print()
    print("=" * 74)
    print("5. WITHDRAWING CONSENT TAKES EFFECT IMMEDIATELY, AND IS AUDITED")
    print("=" * 74)
    code, body = await call("/connected-equipment/sharing", fac_a, "PUT",
                            {"oem_code": "OEM_ALPHA", "grants": []})
    check("the factory can withdraw everything", code == 200 and
          body.get("granted") == [], f"{code} {body}")
    code, fleet = await call("/oem/fleet", oem)
    a1 = [m for m in fleet["machines"] if m["serial_number"] == "SN-A1"][0]
    check("the OEM's very next request shows no hours",
          a1["operating_hours"] is None, str(a1["operating_hours"]))
    check("...but it still sees the machine it sold",
          a1["serial_number"] == "SN-A1")

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    entries = (db.query(models.AuditLog)
                 .filter(models.AuditLog.action == "oem_sharing_changed").all())
    tenancy.reset_current_tenant(tok)
    check("every sharing change was written to the audit log",
          len(entries) >= 2, str(len(entries)))
    check("...naming the actor who made it",
          all(e.actor == "factory_a-admin" for e in entries),
          str([e.actor for e in entries]))
    check("...and recording what changed, before and after",
          any("before=" in (e.details or "") and "after=" in (e.details or "")
              for e in entries), str([e.details for e in entries])[:160])
    db.close()


def test_connected_equipment():
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
    print("THE FACTORY OWNS ITS CONSENT, AND CAN SEE WHAT IT GRANTED")
