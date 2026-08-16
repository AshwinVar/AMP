"""The AERON demo, walked end to end by a machine, so it cannot rot.

A sales demo that breaks in front of a prospect is worse than no demo. This
harness performs the exact sequence in docs/sales/OEM-DEMO-RUNBOOK.md — the same
endpoints the browser calls, in the same order — and fails loudly the moment a
step stops working.

WHAT MAKES IT EVIDENCE. Every business step below is an HTTP request through the
whole middleware stack, carrying a token AMP itself issued at a login endpoint.
The telemetry is not an HTTP shortcut either: it goes through
`mqtt_service.on_message`, the same function the broker calls, with the same
topic and payload a gateway would send.

THE ONE THING THAT IS SEEDED is the demo's starting state — the manufacturer,
its catalogue, the customer and their logins — because that is onboarding, not
part of the ten minutes. `SN-ACX-0001` is NOT seeded: registering it is minute 2
and the harness does it through the API like everything else.

    python audit_oem_demo_journey.py          # SQLite
    python audit_oem_demo_journey.py 5432     # scratch PostgreSQL
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STEPS = [0]
FAILURES = []


def step(label, ok, detail=""):
    STEPS[0] += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        import pg_scratch
        print(pg_scratch.ensure(port, "amp_demo_journey").split(",")[0])
        os.environ["DATABASE_URL"] = pg_scratch.scratch_url(port, "amp_demo_journey")
    else:
        os.environ["DATABASE_URL"] = "sqlite:///./demo_journey.db"
        if os.path.exists("demo_journey.db"):
            os.remove("demo_journey.db")
    os.environ.setdefault("DEMO_PASSWORD", "demo-journey-pass")

    import demo_aeron
    import main as amp
    import models
    import mqtt_service
    import tenancy
    from database import Base, engine, SessionLocal

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()

    PW = os.environ["DEMO_PASSWORD"]

    # ------------------------------------------------------------ HTTP ----
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
                 "method": method, "scheme": "http", "path": p,
                 "raw_path": p.encode(), "query_string": q.encode(),
                 "root_path": "", "headers": raw, "client": ("127.0.0.1", 5000),
                 "server": ("testserver", 80)}
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
            return status.get("code"), {"_raw": data[:200].decode("utf-8", "replace")}

    def GET(p, t=None):
        return asyncio.run(_call(p, t))

    def POST(p, t=None, b=None):
        return asyncio.run(_call(p, t, "POST", b or {}))

    def PUT(p, t=None, b=None):
        return asyncio.run(_call(p, t, "PUT", b or {}))

    # ---------------------------------------------------- starting state ----
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        demo_aeron.seed(db)
    except demo_aeron.DemoRefused as e:
        # A refusal is a configuration answer, not a crash. Reported as one so
        # somebody running this with a short DEMO_PASSWORD reads a sentence
        # rather than a traceback.
        print(f"CANNOT START: {e}")
        return 2
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()

    print()
    print("=" * 74)
    print("BEFORE THE MEETING — the demo is at its starting state")
    print("=" * 74)
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    step("the demo factory exists and is labelled a demo",
         db.query(models.CompanyTenant).filter(
             models.CompanyTenant.company_code == demo_aeron.DEMO_TENANT
         ).first() is not None, "no demo tenant")
    step("AERON exists with the ACX-75 in its catalogue",
         db.query(models.MachineModel).filter(
             models.MachineModel.oem_code == "AERON",
             models.MachineModel.model_code == "ACX-75").first() is not None,
         "no model")
    step("NOTHING is registered yet — the demo starts empty",
         db.query(models.MachineInstallation).filter(
             models.MachineInstallation.oem_code == "AERON").count() == 0,
         "a machine is already registered")
    tenancy.reset_current_tenant(tok)
    db.close()

    # ------------------------------------------------------ minutes 1-4 ----
    # A NEIGHBOUR. The demo runs on the same deployment as real customers, so
    # "the demo cannot show anybody else's data" has to be an assertion rather
    # than an assurance. This stands in for a real tenant.
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code="REAL_CUSTOMER"))
    db.add(models.Machine(tenant_code="REAL_CUSTOMER", site="", name="THEIR-SECRET-LINE",
                          status="Running", utilization=90, downtime="0 min"))
    db.add(models.OemOrganization(oem_code="RIVAL_OEM", name="Rival Compressors"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("MINUTE 1-2 — the manufacturer signs in and sees its fleet")
    print("=" * 74)
    c, body = POST("/oem/login", None, {"username": demo_aeron.OEM_ADMIN,
                                        "password": PW})
    step("AERON's admin signs in", c == 200 and body.get("access_token"), f"{c} {body}")
    oem = body.get("access_token")
    step("...and is branded as Aeron", body.get("branding", {}).get("name") == "Aeron",
         str(body.get("branding")))

    c, fleet = GET("/oem/fleet", oem)
    step("the fleet is empty before the story starts",
         c == 200 and fleet.get("total") == 0, f"{c} {fleet}")

    print()
    print("=" * 74)
    print("MINUTE 2-3 — it registers the machine it just built")
    print("=" * 74)
    c, models_list = GET("/oem/models", oem)
    step("its own catalogue is listed", c == 200 and models_list, f"{c} {models_list}")
    model_id = next((m["id"] for m in models_list
                     if m["model_code"] == "ACX-75"), None)
    step("the ACX-75 is in it", model_id is not None, str(models_list))

    c, reg = POST("/oem/machines", oem, {"serial_number": demo_aeron.DEMO_SERIAL,
                                         "model_id": model_id})
    step(f"{demo_aeron.DEMO_SERIAL} is registered", c == 200, f"{c} {reg}")
    inst_id = reg.get("installation_id")
    step("...and belongs to NO customer yet", reg.get("factory") is None, str(reg))

    print()
    print("=" * 74)
    print("MINUTE 3-4 — it issues an installation code")
    print("=" * 74)
    c, claim = POST(f"/oem/machines/{inst_id}/claim", oem, {"expires_in_days": 30})
    step("a claim code is produced", c == 200 and claim.get("claim_code"),
         f"{c} {claim}")
    code = claim.get("claim_code")
    step("...with a QR target the sticker can carry",
         bool(claim.get("claim_url")) and code in (claim.get("claim_url") or ""),
         str(claim.get("claim_url")))
    step("...shown once, and never retrievable again",
         all("claim_code" not in c2 for c2 in GET("/oem/claims", oem)[1]["claims"]),
         "a listing handed the code back")

    # ------------------------------------------------------ minutes 4-6 ----
    print()
    print("=" * 74)
    print("MINUTE 4-5 — the customer claims the machine")
    print("=" * 74)
    c, body = POST("/login", None, {"username": demo_aeron.FACTORY_ADMIN,
                                    "password": PW})
    step("the customer's admin signs in", c == 200 and body.get("access_token"),
         f"{c} {body}")
    fac = body.get("access_token")

    c, prev = GET(f"/connected-equipment/claim/{code}", fac)
    step("the code is looked up — and claims nothing", c == 200, f"{c} {prev}")
    step("...it names Aeron Compressor Systems",
         prev.get("manufacturer_name") == "Aeron Compressor Systems",
         str(prev.get("manufacturer_name")))
    step("...the ACX-75", prev.get("model_code") == "ACX-75", str(prev.get("model_code")))
    step(f"...and {demo_aeron.DEMO_SERIAL}",
         prev.get("serial_number") == demo_aeron.DEMO_SERIAL,
         str(prev.get("serial_number")))

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    still = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == inst_id).first()
    step("LOOKING IS NOT ACCEPTING: it still belongs to nobody",
         still.factory_tenant_code is None, str(still.factory_tenant_code))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("MINUTE 5-6 — the customer chooses what to share")
    print("=" * 74)
    CHOSEN = ["SHARE_MACHINE_HEALTH", "SHARE_OPERATING_HOURS",
              "SHARE_SERVICE_STATUS", "SHARE_ALARMS"]
    c, acc = POST(f"/connected-equipment/claim/{code}", fac, {"grants": CHOSEN})
    step("it accepts, granting FOUR categories of seven", c == 200, f"{c} {acc}")
    step("...and exactly those four", sorted(acc.get("granted", [])) == sorted(CHOSEN),
         str(acc.get("granted")))

    c, ce = GET("/connected-equipment", fac)
    avail = [g["key"] for g in ce.get("available_grants", [])]
    step("NOT everything is shared — three categories stay off",
         len(avail) == 7 and len(CHOSEN) == 4, f"{len(avail)} available")

    # ------------------------------------------------------ minutes 6-8 ----
    print()
    print("=" * 74)
    print("MINUTE 6-7 — the customer says which machine it is, and it connects")
    print("=" * 74)
    floor = next((m for m in ce["equipment"] if m["serial_number"] == demo_aeron.DEMO_SERIAL), None)
    step("it is on the customer's Connected Equipment screen", floor is not None,
         str(ce.get("equipment")))

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    machine_id = db.query(models.Machine).filter(
        models.Machine.tenant_code == demo_aeron.DEMO_TENANT,
        models.Machine.name == demo_aeron.FLOOR_MACHINE).first().id
    tenancy.reset_current_tenant(tok)
    db.close()

    c, link = POST(f"/connected-equipment/{inst_id}/link", fac,
                   {"machine_id": machine_id})
    step(f"it is linked to {demo_aeron.FLOOR_MACHINE}",
         c == 200 and link.get("machine_id") == machine_id, f"{c} {link}")

    # THE REAL INGEST PATH. Same handler the broker calls.
    mqtt_service.SessionLocal = SessionLocal
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    topic, sent = demo_aeron.publish(db, hours=demo_aeron.START_HOURS)
    tenancy.reset_current_tenant(tok)
    db.close()
    step("the compressor reports over MQTT, on its real topic",
         topic.endswith(f"/{demo_aeron.DEMO_TENANT}/-/machines"), topic)

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == inst_id).first()
    step("...and AMP records that it has been heard from",
         inst.last_seen_at is not None, "never seen")
    step("...with hours read through AERON's OWN tag name",
         inst.operating_hours == demo_aeron.START_HOURS, str(inst.operating_hours))
    tenancy.reset_current_tenant(tok)
    db.close()

    c, tr = POST(f"/oem/machines/{inst_id}/transition", oem, {"target": "Installed"})
    step("AERON records it as installed", c == 200, f"{c} {tr}")
    c, com = POST(f"/oem/machines/{inst_id}/commission", oem)
    step("AERON commissions it", c == 200, f"{c} {com}")
    checks = com.get("commissioning", {})
    step("...with EVERY commissioning check passing", checks.get("ready") is True,
         json.dumps(checks.get("checks")))
    step("...leaving the machine Active", com.get("to") == "Active", str(com.get("to")))

    # ------------------------------------------------------ minutes 7-9 ----
    print()
    print("=" * 74)
    print("MINUTE 7-8 — the manufacturer sees the machine at its customer")
    print("=" * 74)
    c, fleet = GET("/oem/fleet", oem)
    step("the fleet now holds one machine", c == 200 and fleet.get("total") == 1,
         f"{c} {fleet}")
    row = fleet["machines"][0]
    step("...at the demo customer", row.get("customer") == demo_aeron.DEMO_TENANT,
         str(row.get("customer")))
    step("...reporting the hours the customer agreed to share",
         row.get("operating_hours") == demo_aeron.START_HOURS,
         str(row.get("operating_hours")))
    step("...and its health", row.get("machine_status") == "Running",
         str(row.get("machine_status")))
    step("...while what was NOT shared is named, not silently missing",
         "SHARE_TELEMETRY" in (GET(f"/oem/machines/{inst_id}", oem)[1]
                               .get("not_shared") or []),
         str(GET(f"/oem/machines/{inst_id}", oem)[1].get("not_shared")))

    print()
    print("=" * 74)
    print("MINUTE 8-9 — the machine runs past its service interval")
    print("=" * 74)
    OVER = demo_aeron.SERVICE_INTERVAL_HOURS + 120.0
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    demo_aeron.publish(db, hours=OVER)
    tenancy.reset_current_tenant(tok)
    db.close()

    c, svc = GET("/oem/service", oem)
    step("the service queue raises it", c == 200 and svc.get("total", 0) >= 1,
         f"{c} {svc}")
    rec = (svc.get("recommendations") or [{}])[0]
    step("...naming the machine", demo_aeron.DEMO_SERIAL in json.dumps(svc),
         json.dumps(svc)[:200])
    step("...with the evidence that justifies it", bool(rec.get("evidence")),
         str(rec))
    step("...and NO prediction it cannot support", rec.get("confidence") is None,
         f"confidence={rec.get('confidence')}")

    c, ms = GET(f"/oem/machines/{inst_id}/service", oem)
    step("the machine's own service state says overdue",
         c == 200 and ms.get("service", {}).get("state") == "overdue",
         str(ms.get("service")))

    c, done = POST(f"/oem/machines/{inst_id}/service", oem, {"service_hours": OVER})
    step("the engineer records the completed service", c == 200, f"{c} {done}")
    step("...and it is no longer overdue",
         done.get("service", {}).get("state") != "overdue", str(done.get("service")))

    c, ce2 = GET("/connected-equipment", fac)
    row2 = next(m for m in ce2["equipment"] if m["serial_number"] == demo_aeron.DEMO_SERIAL)
    step("THE CUSTOMER sees the updated service state too",
         row2.get("service", {}).get("state") != "overdue", str(row2.get("service")))

    # -------------------------------------------------------- minute 9-10 --
    print()
    print("=" * 74)
    print("MINUTE 9-10 — the customer withdraws one permission")
    print("=" * 74)
    c, before = GET("/oem/fleet", oem)
    step("CONTROL: AERON can see operating hours right now",
         before["machines"][0].get("operating_hours") is not None,
         str(before["machines"][0].get("operating_hours")))

    remaining = [g for g in CHOSEN if g != "SHARE_OPERATING_HOURS"]
    c, upd = PUT("/connected-equipment/sharing", fac,
                 {"oem_code": "AERON", "grants": remaining})
    step("the customer switches OFF operating hours", c == 200, f"{c} {upd}")

    c, after = GET("/oem/fleet", oem)
    step("AERON LOSES THE FIELD ON ITS VERY NEXT REQUEST",
         after["machines"][0].get("operating_hours") is None,
         str(after["machines"][0].get("operating_hours")))
    c, one = GET(f"/oem/machines/{inst_id}", oem)
    step("...and is told it is not shared, rather than shown a zero",
         "SHARE_OPERATING_HOURS" in (one.get("not_shared") or []),
         str(one.get("not_shared")))

    # THE SCREEN THE PROSPECT IS ACTUALLY LOOKING AT. The fleet row losing its
    # hours was already checked above, and used to be all this asserted — while
    # the service queue two inches higher on the same page went on printing
    # "4120.0 h run" in its evidence line. Anything that survives here is a
    # number the customer switched off, being read out loud during the demo
    # whose entire point is that it cannot be.
    c, svc_after = GET("/oem/service", oem)
    hours_text = str(int(OVER))
    step("THE SERVICE QUEUE LOSES THE HOURS TOO, prose included",
         hours_text not in json.dumps(svc_after), json.dumps(svc_after)[:300])
    c, ms_after = GET(f"/oem/machines/{inst_id}/service", oem)
    step("...and so does the machine's own service panel",
         hours_text not in json.dumps(ms_after), json.dumps(ms_after)[:300])
    step("...while the service VERDICT, which the customer did grant, stands",
         ms_after.get("service", {}).get("state") not in (None, "not_shared"),
         str(ms_after.get("service")))
    step("...while the machine itself is still visible",
         after["machines"][0].get("serial_number") == demo_aeron.DEMO_SERIAL,
         str(after["machines"][0]))
    step("...and the health it still may see is unaffected",
         after["machines"][0].get("machine_status") == "Running",
         str(after["machines"][0].get("machine_status")))

    print()
    print("=" * 74)
    print("THE DEMO SHOWS NOBODY ELSE'S BUSINESS")
    print("=" * 74)
    # The demo runs beside real customers. A prospect watching this screen must
    # never see a row belonging to one of them, and AERON must never see a rival
    # manufacturer's equipment.
    c, floor_machines = GET("/machines", fac)
    names = [m.get("name") for m in (floor_machines or [])]
    step("the demo factory's floor holds only demo machines",
         c == 200 and "THEIR-SECRET-LINE" not in names, str(names))
    step("CONTROL: it does see its own", demo_aeron.FLOOR_MACHINE in names, str(names))

    c, fleet_final = GET("/oem/fleet", oem)
    blob = json.dumps(fleet_final)
    step("AERON's fleet names no other customer", "REAL_CUSTOMER" not in blob,
         "another tenant appears in the OEM's fleet")
    step("...and no rival manufacturer", "RIVAL_OEM" not in blob, blob[:200])

    print()
    print("=" * 74)
    print(f"{STEPS[0]} steps")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("   *", f)
        return 1
    print("THE TEN-MINUTE DEMO WORKS, END TO END, THROUGH THE REAL API")
    return 0


if __name__ == "__main__":
    sys.exit(main())
