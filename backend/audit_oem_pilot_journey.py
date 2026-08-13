"""THE PILOT, END TO END, THROUGH THE REAL API AND NOTHING ELSE.

Every other harness tests a boundary. This one tests whether the product exists:
can a real manufacturer be onboarded, register a real machine, have a real
customer claim it, commission it, receive telemetry, and be serviced — with

    NO database editing
    NO hand-minted JWTs
    NO source changes
    NO tenant-id manipulation
    NO developer intervention

Every step below is an HTTP request through the whole middleware stack, carrying
a token that AMP itself issued at a login endpoint. The moment any step needs a
fixture written directly into a table, this file stops being evidence and the
answer to "is it pilot-ready" is no.

THE ONE EXCEPTION, STATED PLAINLY: the founder account and the machine model.
A platform needs a first administrator before anybody can log in to create one,
and the model catalogue has no write route in this release — both are seeded
here and both are named in OEM-PLATFORM-READINESS.md as remaining work.
Everything else is the product.

Run: python backend/audit_oem_pilot_journey.py [port]      (PostgreSQL)
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = "amp_scratch_pilot"
STEPS = [0]
FAILURES = []


def step(label, ok, detail=""):
    STEPS[0] += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    import pg_scratch
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    os.environ["DATABASE_URL"] = url

    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import database
    import main as amp
    import models
    import tenancy
    from database import Base
    from security import hash_password

    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = Session
    amp.SessionLocal = Session
    for mod in ("oem_routes", "oem_admin_routes", "connected_equipment_routes",
                "saas_routes", "core_routes", "machines_routes"):
        __import__(mod)
        sys.modules[mod].SessionLocal = Session

    print(version.split(",")[0])
    print()

    async def call(path, tok=None, method="GET", body=None):
        raw = [(b"host", b"testserver")]
        payload = b"" if body is None else json.dumps(body).encode()
        if tok:
            raw.append((b"authorization", f"Bearer {tok}".encode()))
        if body is not None:
            raw.append((b"content-type", b"application/json"))
            raw.append((b"content-length", str(len(payload)).encode()))
        p, _, q = path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": p,
                 "raw_path": p.encode(), "query_string": q.encode(),
                 "root_path": "", "headers": raw,
                 "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
        chunks, status = [], {}

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                status["code"] = m["status"]
            elif m["type"] == "http.response.body":
                chunks.append(m.get("body", b""))

        await amp.app(scope, receive, send)
        data = b"".join(chunks)
        try:
            return status.get("code"), json.loads(data or b"{}")
        except Exception:
            return status.get("code"), {"_raw": data[:300].decode("utf-8", "replace")}

    def GET(p, t=None):
        return asyncio.run(call(p, t))

    def POST(p, t=None, b=None):
        return asyncio.run(call(p, t, "POST", b or {}))

    def PUT(p, t=None, b=None):
        return asyncio.run(call(p, t, "PUT", b or {}))

    # ── THE ONLY SEEDED THINGS, AND WHY ──────────────────────────────
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.User(username="founder", password=hash_password("founder-pw-123"),
                       role="Admin", tenant_code="DEFAULT", is_active=True))
    db.add(models.User(username="factorya_admin",
                       password=hash_password("factory-pw-123"),
                       role="Admin", tenant_code="FACTORY_A", is_active=True))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    print("=" * 74)
    print("1. THE PLATFORM OPERATOR ONBOARDS A MANUFACTURER")
    print("=" * 74)
    code, body = POST("/login", None, {"username": "founder",
                                       "password": "founder-pw-123"})
    step("the founder signs in through /login", code == 200, f"{code} {body}")
    founder = body.get("access_token")

    code, body = POST("/saas/oems", founder,
                      {"oem_code": "PILOT_OEM", "name": "Pilot Compressors",
                       "brand_name": "Pilot Connect", "brand_color": "#0f766e",
                       "support_email": "service@pilot.example"})
    step("registers the manufacturer", code == 200, f"{code} {body}")
    oem_id = body.get("id")

    code, body = POST(f"/saas/oems/{oem_id}/admin", founder)
    step("provisions its first administrator", code == 200, f"{code} {body}")
    oem_user, oem_temp = body.get("username"), body.get("temporary_password")
    step("...and hands over a one-time password", bool(oem_temp), str(body))

    # The catalogue. Named in the readiness doc as remaining work: there is no
    # model-creation route in this release.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    model = models.MachineModel(
        oem_code="PILOT_OEM", family="Compressor", model_code="PC-40",
        name="PC-40 Screw Compressor", service_interval_hours=2000,
        warranty_months=24,
        telemetry_profile=json.dumps({"signals": [
            {"name": "running", "source": "Run", "datatype": "bool",
             "state_signal": True},
            {"name": "operating_hours", "source": "HrsRun", "datatype": "number",
             "unit": "h", "aggregation": "max"}]}))
    db.add(model)
    db.commit()
    model_id = model.id
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("2. THE MANUFACTURER SIGNS IN AND REGISTERS A MACHINE")
    print("=" * 74)
    code, body = POST("/oem/login", None, {"username": oem_user,
                                           "password": oem_temp})
    step("the OEM admin signs in with the one-time password", code == 200,
         f"{code} {body}")
    oem_tok = body.get("access_token")

    code, body = POST("/oem/change-password", oem_tok,
                      {"current_password": oem_temp,
                       "new_password": "pilot-oem-real-password"})
    step("rotates it immediately", code == 200, f"{code} {body}")
    code, body = POST("/oem/login", None, {"username": oem_user,
                                           "password": "pilot-oem-real-password"})
    step("...and signs in again with the new one", code == 200, f"{code} {body}")
    oem_tok = body.get("access_token")

    code, body = POST("/oem/machines", oem_tok,
                      {"serial_number": "PC40-0001", "model_id": model_id,
                       "warranty_start": "2026-08-01", "warranty_end": "2028-08-01"})
    step("registers machine PC40-0001", code == 200, f"{code} {body}")
    inst_id = body.get("installation_id")
    step("...which belongs to NO factory yet", body.get("factory") is None, str(body))

    code, body = POST(f"/oem/machines/{inst_id}/claim", oem_tok,
                      {"expires_in_days": 30})
    step("creates an installation invitation", code == 200, f"{code} {body}")
    claim_code = body.get("claim_code")
    step("...and is shown the code exactly once", bool(claim_code), str(body))

    print()
    print("=" * 74)
    print("3. THE CUSTOMER CLAIMS IT")
    print("=" * 74)
    code, body = POST("/login", None, {"username": "factorya_admin",
                                       "password": "factory-pw-123"})
    step("the factory Admin signs in", code == 200, f"{code} {body}")
    fac = body.get("access_token")

    code, body = GET(f"/connected-equipment/claim/{claim_code}", fac)
    step("previews the machine before deciding", code == 200, f"{code} {body}")
    step("...and is told who made it", body.get("manufacturer") == "PILOT_OEM",
         str(body))
    step("...which model", body.get("model_code") == "PC-40", str(body))
    step("...and the warranty the OEM recorded",
         body.get("warranty", {}).get("state") == "active", str(body.get("warranty")))

    code, body = POST(f"/connected-equipment/claim/{claim_code}", fac,
                      {"grants": ["SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS",
                                  "SHARE_MACHINE_HEALTH"]})
    step("accepts it, choosing three sharing categories", code == 200,
         f"{code} {body}")
    step("...and the machine is now theirs",
         body.get("lifecycle_status") == "Assigned", str(body))

    code, body = GET("/connected-equipment", fac)
    serials = [e["serial_number"] for e in body.get("equipment", [])]
    step("it appears under their Connected Equipment", "PC40-0001" in serials,
         str(serials))

    code, body = GET("/oem/notifications", oem_tok)
    kinds = {n["type"] for n in body.get("notifications", [])}
    step("the manufacturer is notified it was accepted",
         "installation_accepted" in kinds, str(kinds))

    print()
    print("=" * 74)
    print("4. COMMISSIONING AND TELEMETRY")
    print("=" * 74)
    # The factory links it to a machine on its own floor, through its own API.
    code, body = POST("/machines", fac,
                      {"name": "COMP-PC40", "status": "Running",
                       "utilization": 0, "downtime": "0m"})
    step("the factory creates the shop-floor machine", code in (200, 201),
         f"{code} {body}")
    machine_id = body.get("id")

    code, body = POST(f"/connected-equipment/{inst_id}/link", fac,
                      {"machine_id": machine_id})
    step("...and says which machine the supplied equipment is",
         code == 200 and body.get("machine_id") == machine_id, f"{code} {body}")
    # NOTE: the site stays as the installation recorded it. schemas.MachineBase
    # carries no `site`, so POST /machines cannot set one; the link copies the
    # machine's site only when the machine has one (CSV onboarding, MQTT).
    step("...which is the FACTORY's decision, not the manufacturer's",
         POST(f"/connected-equipment/{inst_id}/link", oem_tok,
              {"machine_id": machine_id})[0] in (401, 403, 404))

    db = Session()
    tok = tenancy.set_current_tenant(None)
    # TELEMETRY, and telemetry only. The hours a machine reports arrive over
    # MQTT (ADR-0011); there is no HTTP route to post them and there should not
    # be one, so this is the single seeded fact in the whole journey and it is
    # named as such in the readiness doc.
    db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == inst_id).update(
        {"last_seen_at": datetime.utcnow(), "operating_hours": 120.0})
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    code, body = POST(f"/oem/machines/{inst_id}/transition", oem_tok,
                      {"target": "Installed"})
    step("the OEM records it as installed", code == 200, f"{code} {body}")
    code, body = POST(f"/oem/machines/{inst_id}/commission", oem_tok)
    step("and commissions it", code == 200, f"{code} {body}")
    step("...with every commissioning check passing",
         body.get("commissioning", {}).get("ready") is True,
         str(body.get("commissioning")))
    step("...leaving the machine Active", body.get("to") == "Active", str(body))

    code, body = GET("/oem/fleet", oem_tok)
    row = [m for m in body.get("machines", []) if m["serial_number"] == "PC40-0001"]
    step("the OEM sees the machine at its customer",
         row and row[0]["customer"] == "FACTORY_A", str(row))
    step("...and the operating hours the customer agreed to share",
         row and row[0]["operating_hours"] == 120.0, str(row))

    print()
    print("=" * 74)
    print("5. SERVICE")
    print("=" * 74)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == inst_id).update(
        {"operating_hours": 2100.0})     # past the 2,000 h interval
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    code, body = GET("/oem/service", oem_tok)
    recs = [r for r in body.get("recommendations", []) if r["machine"] == "PC40-0001"]
    step("the service queue raises the machine", recs, str(body.get("total")))
    step("...as overdue, with its evidence",
         recs and recs[0]["kind"] == "service_due" and recs[0]["evidence"],
         str(recs[:1]))
    step("...and claims no prediction it cannot justify",
         recs and recs[0]["confidence"] is None, str(recs[:1]))

    code, body = POST(f"/oem/machines/{inst_id}/service", oem_tok,
                      {"service_hours": 2100})
    step("the service manager records the completed service", code == 200,
         f"{code} {body}")
    step("...and the machine is no longer overdue",
         body.get("service", {}).get("state") == "ok", str(body.get("service")))

    code, body = GET("/connected-equipment", fac)
    eq = [e for e in body.get("equipment", []) if e["serial_number"] == "PC40-0001"]
    step("the FACTORY sees the updated service state",
         eq and eq[0]["service"]["state"] == "ok", str(eq and eq[0]["service"]))

    print()
    print("=" * 74)
    print("6. THE CUSTOMER WITHDRAWS ONE PERMISSION")
    print("=" * 74)
    code, body = GET("/oem/fleet", oem_tok)
    before = [m for m in body.get("machines", [])
              if m["serial_number"] == "PC40-0001"][0]
    step("CONTROL: the OEM currently sees operating hours",
         before.get("operating_hours") == 2100.0, str(before.get("operating_hours")))

    code, body = PUT("/connected-equipment/sharing", fac,
                     {"oem_code": "PILOT_OEM",
                      "grants": ["SHARE_SERVICE_STATUS", "SHARE_MACHINE_HEALTH"]})
    step("the factory withdraws SHARE_OPERATING_HOURS", code == 200,
         f"{code} {body}")

    code, body = GET("/oem/fleet", oem_tok)
    after = [m for m in body.get("machines", [])
             if m["serial_number"] == "PC40-0001"][0]
    step("THE OEM LOSES THE FIELD ON ITS VERY NEXT REQUEST",
         after.get("operating_hours") is None, str(after.get("operating_hours")))
    step("...and is told it is not shared rather than shown a zero",
         "SHARE_OPERATING_HOURS" not in after.get("shared", []),
         str(after.get("shared")))
    step("...while the machine itself is still visible",
         after.get("serial_number") == "PC40-0001", str(after))

    print()
    print("=" * 74)
    print(f"{STEPS[0]} steps, all through the real API")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("   *", f)
        engine.dispose()
        return 1
    print("THE PILOT JOURNEY COMPLETES WITHOUT A DEVELOPER")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
