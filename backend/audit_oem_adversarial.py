"""TWO OEMs, THREE FACTORIES, ONE PLATFORM. Try to break every boundary.

This is the audit that decides whether AMP can sit between machine manufacturers
and their customers' factories. Everything before it proved a rule in a unit; this
attacks a POPULATED, MULTI-PARTY world through the surfaces a real attacker has.

    OEM_ALPHA  sells into FACTORY_A and FACTORY_B
    OEM_BETA   sells into FACTORY_B and FACTORY_C

Every factory runs machines called COMP-001 / COMP-002 / LINE-01. Every serial is
globally distinct. FACTORY_B is the interesting one: two manufacturers have
equipment on the same shop floor, so "my machines at this customer" and "this
customer's machines" must never be the same set.

WHAT IS ATTACKED
----------------
    A  the factory <-> factory boundary          (must be unchanged by all this)
    B  the OEM <-> factory boundary              (operational data)
    C  the OEM <-> OEM boundary                  (a competitor's fleet)
    D  the consent boundary                      (granted vs not granted)
    E  identity forgery                          (tokens, claims, roles)
    F  enumeration                               (ids, serials, 403-vs-404)
    G  revocation                                (does it take effect NOW)
    H  the transport surfaces                    (HTTP, WebSocket, MQTT)

Run: python backend/audit_oem_adversarial.py [port]     (PostgreSQL only)
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []
CHECKS = [0]


def check(label, refused, detail=""):
    CHECKS[0] += 1
    print(f"  {'REFUSED ' if refused else 'BREACHED'}  {label}"
          + (f"   [{detail}]" if detail else ""))
    if not refused:
        FAILURES.append(f"{label}: {detail}")


def ok(label, condition, detail=""):
    CHECKS[0] += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    import pg_scratch
    version = pg_scratch.ensure(port, "amp_oem_adversarial")
    url = pg_scratch.scratch_url(port, "amp_oem_adversarial")
    os.environ["DATABASE_URL"] = url
    print(version.split(",")[0])
    print("OEM_ALPHA -> FACTORY_A, FACTORY_B    OEM_BETA -> FACTORY_B, FACTORY_C")
    print()

    from jose import jwt as pyjwt
    import auth
    import live_ws
    import main as amp
    import models
    import mqtt_identity
    import oem_auth
    import oem_sharing
    import tenancy
    from database import Base, engine, SessionLocal

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()
    ALGO = getattr(auth, "ALGORITHM", "HS256")

    # ---------------------------------------------------------------- seed --
    tok = tenancy.set_current_tenant(None)
    for code, name in (("OEM_ALPHA", "Alpha Compressors"), ("OEM_BETA", "Beta Systems")):
        db.add(models.OemOrganization(oem_code=code, name=name))
    for code, user, role in (("OEM_ALPHA", "alpha-admin", "OEM_ADMIN"),
                             ("OEM_ALPHA", "alpha-eng", "OEM_SERVICE_ENGINEER"),
                             ("OEM_ALPHA", "alpha-view", "OEM_VIEWER"),
                             ("OEM_BETA", "beta-admin", "OEM_ADMIN")):
        db.add(models.OemUser(oem_code=code, username=user, password="x", role=role))
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        db.add(models.User(username=f"{t.lower()}-admin", password="x",
                           role="Admin", tenant_code=t, is_active=True))
    db.flush()

    mods = {}
    for code in ("OEM_ALPHA", "OEM_BETA"):
        m = models.MachineModel(oem_code=code, family="Compressor",
                                model_code="X200", name=f"{code} X200",
                                service_interval_hours=2000, warranty_months=24)
        db.add(m)
        db.flush()
        mods[code] = m

    machines = {}
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        for name in ("COMP-001", "COMP-002", "LINE-01"):
            mc = models.Machine(tenant_code=t, site="Plant 1", name=name,
                                status="Running", utilization=70)
            db.add(mc)
            db.flush()
            machines[(t, name)] = mc
        # Confidential operational data, per factory.
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001",
                                part_number=f"{t}-SECRET-PART", batch_number="B1",
                                target_quantity=100, status="In Progress"))
        db.add(models.InventoryItem(tenant_code=t, item_code="INV-001",
                                    item_name=f"{t} raw stock", category="Raw",
                                    unit="kg", current_stock=500, reorder_level=10))
        db.add(models.CostRecord(tenant_code=t, cost_no=f"C-{t}", cost_type="Labour",
                                 description=f"{t} confidential cost", amount=9999))
        db.add(models.ProductionRecord(
            tenant_code=t, machine_id=machines[(t, "COMP-001")].id,
            planned_minutes=480, runtime_minutes=400, ideal_cycle_time_seconds=30,
            total_count=600, good_count=570, rejected_count=30))
        db.add(models.BillOfMaterials(tenant_code=t, part_number="FG-1",
                                      revision="A", active=True))

    installs = {}
    for oem, t, serial, mname in (
            ("OEM_ALPHA", "FACTORY_A", "SN-ALPHA-0001", "COMP-001"),
            ("OEM_ALPHA", "FACTORY_A", "SN-ALPHA-0002", "COMP-002"),
            ("OEM_ALPHA", "FACTORY_B", "SN-ALPHA-0003", "COMP-001"),
            ("OEM_BETA", "FACTORY_B", "SN-BETA-0001", "COMP-002"),
            ("OEM_BETA", "FACTORY_C", "SN-BETA-0002", "COMP-001")):
        row = models.MachineInstallation(
            oem_code=oem, serial_number=serial, model_id=mods[oem].id,
            factory_tenant_code=t, site="Plant 1",
            machine_id=machines[(t, mname)].id, status="Active",
            operating_hours=1900.0, last_seen_at=datetime.utcnow(),
            warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1))
        db.add(row)
        db.flush()
        installs[serial] = row
    # FACTORY_A shares operating hours with ALPHA only. Nobody else shares anything.
    db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA", tenant_code="FACTORY_A",
                                       grants="SHARE_OPERATING_HOURS"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    print(f"seeded: 3 factories x 3 machines, 5 installations, 1 sharing policy")

    def tokfor(sub, oem=None, tenant=None, role=None, principal=None, **extra):
        c = {"sub": sub, "exp": datetime.utcnow() + timedelta(hours=1)}
        if role:
            c["role"] = role
        if principal:
            c["principal"] = principal
        if oem:
            c["oem"] = oem
        if tenant:
            c["tenant"] = tenant
        c.update(extra)
        return pyjwt.encode(c, auth.SECRET_KEY, algorithm=ALGO)

    async def http(path, tk=None, headers=None, method="GET", payload=None):
        raw = [(b"host", b"testserver")]
        if tk:
            raw.append((b"authorization", f"Bearer {tk}".encode()))
        for k, v in (headers or {}).items():
            raw.append((k.encode(), v.encode()))
        sent = b"" if payload is None else json.dumps(payload).encode()
        if payload is not None:
            raw.append((b"content-type", b"application/json"))
            raw.append((b"content-length", str(len(sent)).encode()))
        p, _, q = path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": p, "raw_path": p.encode(),
                 "query_string": q.encode(), "root_path": "", "headers": raw,
                 "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
        body, status = [], {}

        async def receive():
            return {"type": "http.request", "body": sent, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            elif msg["type"] == "http.response.body":
                body.append(msg.get("body", b""))
        await amp.app(scope, receive, send)
        blob = b"".join(body)
        try:
            return status.get("code"), json.loads(blob or b"{}"), blob.decode("utf-8", "replace")
        except Exception:
            return status.get("code"), None, blob.decode("utf-8", "replace")

    SECRETS = ["FACTORY_A-SECRET-PART", "FACTORY_B-SECRET-PART",
               "FACTORY_C-SECRET-PART", "confidential cost", "raw stock"]

    async def run():
        alpha = tokfor("alpha-admin", oem="OEM_ALPHA", role="OEM_ADMIN", principal="oem")
        beta = tokfor("beta-admin", oem="OEM_BETA", role="OEM_ADMIN", principal="oem")
        fac_a = tokfor("factory_a-admin", tenant="FACTORY_A", role="Admin")
        fac_b = tokfor("factory_b-admin", tenant="FACTORY_B", role="Admin")

        # ---------------------------------------------------- A: factory<->factory
        print("\n" + "=" * 74)
        print("A. THE EXISTING FACTORY BOUNDARY IS UNCHANGED")
        print("=" * 74)
        code, body, raw = await http("/machines", fac_a)
        names = {m.get("name") for m in (body or [])}
        ok("FACTORY_A still sees its own three machines",
           code == 200 and names == {"COMP-001", "COMP-002", "LINE-01"},
           f"{code} {names}")
        leaked = [s for s in SECRETS if s in raw and not s.startswith("FACTORY_A")]
        check("FACTORY_A's machine list carries no other factory's data",
              not leaked, str(leaked))
        code, body, raw = await http("/work-orders", fac_b)
        check("FACTORY_B sees no FACTORY_A or FACTORY_C part numbers",
              "FACTORY_A-SECRET-PART" not in raw and "FACTORY_C-SECRET-PART" not in raw,
              raw[:110])

        # ---------------------------------------------------- B: OEM<->factory
        print("\n" + "=" * 74)
        print("B. AN OEM CANNOT REACH FACTORY OPERATIONAL DATA")
        print("=" * 74)
        factory_routes = ["/machines", "/work-orders", "/inventory/items",
                          "/downtime-logs", "/analytics/summary",
                          "/analytics/executive-oee", "/oee/summary",
                          "/agent-actions", "/notifications", "/suppliers",
                          "/purchase-orders", "/quality/inspections",
                          "/quality-summary", "/downtime-summary",
                          "/inventory-summary", "/reports/inventory.csv",
                          "/reports/work-orders.csv", "/reports/quality.csv"]
        for path in factory_routes:
            code, body, raw = await http(path, alpha)
            hit = [s for s in SECRETS if s in raw]
            empty = body in ([], {}, None) or (isinstance(body, list) and not body)
            # A 404 is a route that does not exist, not a leak — treating it
            # as a breach was a bug in the first version of this audit.
            check(f"OEM on {path}",
                  (code in (401, 403, 404) or empty) and not hit,
                  f"{code} {raw[:70]}")

        # Every OEM endpoint, scanned for factory secrets.
        oem_paths = ["/oem/me", "/oem/models", "/oem/fleet", "/oem/customers",
                     "/oem/sharing",
                     f"/oem/machines/{installs['SN-ALPHA-0001'].id}"]
        for path in oem_paths:
            code, body, raw = await http(path, alpha)
            hit = [s for s in SECRETS if s in raw]
            check(f"no factory secret in {path}", not hit, str(hit))

        # ---------------------------------------------------- C: OEM<->OEM
        print("\n" + "=" * 74)
        print("C. ONE MANUFACTURER CANNOT SEE ANOTHER'S FLEET")
        print("=" * 74)
        code, body, _ = await http("/oem/fleet", alpha)
        aser = sorted(m["serial_number"] for m in body["machines"])
        ok("OEM_ALPHA sees exactly its three machines",
           aser == ["SN-ALPHA-0001", "SN-ALPHA-0002", "SN-ALPHA-0003"], str(aser))
        code, body, _ = await http("/oem/fleet", beta)
        bser = sorted(m["serial_number"] for m in body["machines"])
        ok("OEM_BETA sees exactly its two machines",
           bser == ["SN-BETA-0001", "SN-BETA-0002"], str(bser))

        # FACTORY_B has BOTH manufacturers' equipment.
        code, body, _ = await http("/oem/fleet?customer=FACTORY_B", alpha)
        shared = [m["serial_number"] for m in body["machines"]]
        ok("at the SHARED customer, ALPHA sees only its own machine",
           shared == ["SN-ALPHA-0003"], str(shared))
        check("...and BETA's serial is absent from ALPHA's view of that site",
              "SN-BETA-0001" not in json.dumps(body), "leaked")

        for bad in ("SN-BETA-0001", "SN-BETA-0002"):
            code, body, raw = await http(f"/oem/fleet?customer={bad}", alpha)
            check(f"a competitor's serial as ?customer= returns nothing ({bad})",
                  body.get("total") == 0, str(body)[:90])
        code, _, _ = await http("/oem/fleet?customer=OEM_BETA", alpha)
        code2, body2, _ = await http("/oem/fleet?customer=OEM_BETA", alpha)
        check("a competitor's OEM CODE as ?customer= returns nothing",
              body2.get("total") == 0, str(body2)[:90])

        # ---------------------------------------------------- F: enumeration
        print("\n" + "=" * 74)
        print("F. IDS AND SERIALS CANNOT BE ENUMERATED")
        print("=" * 74)
        beta_id = installs["SN-BETA-0001"].id
        code_comp, _, _ = await http(f"/oem/machines/{beta_id}", alpha)
        code_none, _, _ = await http("/oem/machines/99999", alpha)
        check("a competitor's installation id is 404", code_comp == 404, str(code_comp))
        check("...and indistinguishable from a nonexistent id",
              code_comp == code_none, f"{code_comp} vs {code_none}")
        code_own, _, _ = await http(f"/oem/machines/{installs['SN-ALPHA-0001'].id}", alpha)
        ok("CONTROL: its own installation resolves", code_own == 200, str(code_own))
        # Sweep every id: ALPHA must only ever get 200 for its own three.
        got200 = []
        for row in db.query(models.MachineInstallation).all():
            c, _, _ = await http(f"/oem/machines/{row.id}", alpha)
            if c == 200:
                got200.append(row.serial_number)
        ok("sweeping EVERY installation id yields only ALPHA's three",
           sorted(got200) == ["SN-ALPHA-0001", "SN-ALPHA-0002", "SN-ALPHA-0003"],
           str(sorted(got200)))

        # ---------------------------------------------------- D: consent
        print("\n" + "=" * 74)
        print("D. CONSENT IS PER FACTORY, PER FIELD")
        print("=" * 74)
        code, body, _ = await http("/oem/fleet", alpha)
        by_serial = {m["serial_number"]: m for m in body["machines"]}
        ok("FACTORY_A granted hours -> hours visible",
           by_serial["SN-ALPHA-0001"]["operating_hours"] == 1900.0,
           str(by_serial["SN-ALPHA-0001"]["operating_hours"]))
        check("...but machine health was NOT granted, so status stays hidden",
              by_serial["SN-ALPHA-0001"]["machine_status"] is None,
              str(by_serial["SN-ALPHA-0001"]["machine_status"]))
        check("FACTORY_B granted nothing -> no hours for ALPHA's machine there",
              by_serial["SN-ALPHA-0003"]["operating_hours"] is None,
              str(by_serial["SN-ALPHA-0003"]["operating_hours"]))
        code, body, _ = await http("/oem/fleet", beta)
        bmap = {m["serial_number"]: m for m in body["machines"]}
        check("FACTORY_A's grant to ALPHA gives BETA nothing at ITS factories",
              all(m["operating_hours"] is None for m in bmap.values()),
              str([(k, v["operating_hours"]) for k, v in bmap.items()]))

        # ---------------------------------------------------- G: revocation
        print("\n" + "=" * 74)
        print("G. REVOCATION TAKES EFFECT ON THE NEXT REQUEST")
        print("=" * 74)
        tok2 = tenancy.set_current_tenant(None)
        pol = (db.query(models.OemDataSharingPolicy)
                 .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
                         models.OemDataSharingPolicy.tenant_code == "FACTORY_A").one())
        pol.grants = ""
        db.commit()
        tenancy.reset_current_tenant(tok2)
        code, body, _ = await http("/oem/fleet", alpha)
        after = {m["serial_number"]: m for m in body["machines"]}
        check("the very next request shows no hours",
              after["SN-ALPHA-0001"]["operating_hours"] is None,
              str(after["SN-ALPHA-0001"]["operating_hours"]))
        ok("...and the OEM still sees the machine it sold",
           after["SN-ALPHA-0001"]["serial_number"] == "SN-ALPHA-0001")

        # Revoke the whole relationship: the installation is unassigned.
        tok2 = tenancy.set_current_tenant(None)
        installs["SN-ALPHA-0003"].factory_tenant_code = None
        installs["SN-ALPHA-0003"].machine_id = None
        db.commit()
        tenancy.reset_current_tenant(tok2)
        code, body, _ = await http("/oem/fleet?customer=FACTORY_B", alpha)
        check("after unassignment, ALPHA has no machines at FACTORY_B",
              body.get("total") == 0, str(body)[:90])

        # ---------------------------------------------------- E: forgery
        print("\n" + "=" * 74)
        print("E. IDENTITY CANNOT BE FORGED")
        print("=" * 74)
        forgeries = [
            ("a factory admin claiming to be an OEM",
             tokfor("factory_a-admin", oem="OEM_ALPHA", role="OEM_ADMIN", principal="oem")),
            ("an OEM user claiming another OEM",
             tokfor("alpha-admin", oem="OEM_BETA", role="OEM_ADMIN", principal="oem")),
            ("an OEM token with a factory role",
             tokfor("alpha-admin", oem="OEM_ALPHA", role="Admin", principal="oem")),
            ("a token signed with the wrong secret",
             pyjwt.encode({"sub": "alpha-admin", "oem": "OEM_ALPHA",
                           "principal": "oem", "role": "OEM_ADMIN",
                           "exp": datetime.utcnow() + timedelta(hours=1)},
                          "wrong-secret-wrong-secret", algorithm=ALGO)),
            ("an expired OEM token",
             tokfor("alpha-admin", oem="OEM_ALPHA", role="OEM_ADMIN",
                    principal="oem", exp=datetime.utcnow() - timedelta(hours=1))),
        ]
        for label, tk in forgeries:
            code, body, raw = await http("/oem/fleet", tk)
            # "an OEM token with a factory role" is a legitimate session whose
            # role is read from the DATABASE — it must succeed as OEM_ADMIN and
            # still only see its own fleet.
            if "factory role" in label:
                sers = sorted(m["serial_number"] for m in (body or {}).get("machines", []))
                check(f"{label} -> role from DB, own fleet only",
                      code == 200 and all(s.startswith("SN-ALPHA") for s in sers),
                      f"{code} {sers}")
            else:
                check(f"{label} -> refused", code in (401, 403), f"{code} {raw[:70]}")

        # A factory admin cannot reach the OEM portal at all.
        for path in ("/oem/fleet", "/oem/customers", "/oem/sharing"):
            code, _, _ = await http(path, fac_a)
            check(f"a FACTORY admin on {path}", code in (401, 403), str(code))

        # ---------------------------------------------------- H: transports
        print("\n" + "=" * 74)
        print("H. THE OTHER TRANSPORTS")
        print("=" * 74)

        # MQTT: an OEM code is not a tenant. Publishing under one must not reach
        # any factory's machines — EITHER by being refused outright, OR by
        # resolving to a tenant that owns nothing. Both are safe; asserting only
        # one of them was an error in the first version of this audit, which
        # expected a resolve and got the stricter refusal.
        for topic in ("amp/OEM_ALPHA/Plant1/machines",
                      "amp/OEM_ALPHA/../FACTORY_A/machines"):
            try:
                route = mqtt_identity.parse_topic(topic, "amp")
                resolved = route.tenant
            except Exception as e:
                resolved = f"refused:{type(e).__name__}"
            reached = 0
            if not str(resolved).startswith("refused:"):
                tok3 = tenancy.set_current_tenant(resolved)
                reached = db.query(models.Machine).count()
                tenancy.reset_current_tenant(tok3)
            check(f"MQTT under an OEM code reaches no factory machine ({topic})",
                  str(resolved).startswith("refused:") or
                  (resolved not in ("FACTORY_A", "FACTORY_B", "FACTORY_C")
                   and reached == 0),
                  f"resolved={resolved} machines={reached}")
        # CONTROL: a REAL factory topic still resolves and still reaches that
        # factory's machines, so the checks above are not passing because the
        # parser rejects everything.
        route = mqtt_identity.parse_topic("amp/FACTORY_A/Plant1/machines", "amp")
        tok3 = tenancy.set_current_tenant(route.tenant)
        reachable = db.query(models.Machine).count()
        tenancy.reset_current_tenant(tok3)
        ok("CONTROL: a real factory topic still resolves and reaches its machines",
           route.tenant == "FACTORY_A" and reachable == 3,
           f"{route.tenant} {reachable}")

        # WebSocket: an OEM sentinel receives no factory broadcast.
        class Sock:
            def __init__(s):
                s.n = 0

            async def accept(s):
                pass

            async def send_text(s, t):
                s.n += 1

        m = live_ws.ConnectionManager()
        oem_sock, fac_sock = Sock(), Sock()
        bound_oem = await m.connect(oem_sock, oem_auth.sentinel_tenant("OEM_ALPHA"))
        bound_fac = await m.connect(fac_sock, "FACTORY_A")
        await m.broadcast({"event": "machine_update", "tenant_code": "FACTORY_A",
                           "machine": {"name": "COMP-001"}})
        check("an OEM-sentinel socket receives no FACTORY_A broadcast",
              oem_sock.n == 0, f"{oem_sock.n} frames")
        ok("CONTROL: the FACTORY_A socket did receive it", fac_sock.n == 1,
           f"{fac_sock.n} frames")

        # Exports: the CSV surface must respect the same boundary.
        import reports_routes
        for name, fn in (("inventory", reports_routes.export_inventory_csv),
                         ("work orders", reports_routes.export_work_orders_csv)):
            try:
                tok4 = tenancy.set_current_tenant(oem_auth.sentinel_tenant("OEM_ALPHA"))
                resp = fn(db=db, current_user={"sub": "alpha-admin",
                                               "tenant": oem_auth.sentinel_tenant("OEM_ALPHA"),
                                               "role": "Admin"})
                tenancy.reset_current_tenant(tok4)
                text = resp.body.decode() if hasattr(resp, "body") else ""
            except Exception as e:
                text = ""
            hit = [s for s in SECRETS if s in text]
            check(f"the {name} CSV export yields no factory data under an OEM scope",
                  not hit, str(hit))

    # ================================================================== I: WRITES
    async def attack_the_writes():
        print("\n" + "=" * 74)
        print("I. THE WRITE ENDPOINTS (added after the first 60 checks)")
        print("=" * 74)
        alpha = tokfor("alpha-admin", oem="OEM_ALPHA", role="OEM_ADMIN",
                       principal="oem")
        viewer = tokfor("alpha-view", oem="OEM_ALPHA", role="OEM_VIEWER",
                        principal="oem")
        fac_a = tokfor("factory_a-admin", tenant="FACTORY_A", role="Admin")
        mine = installs["SN-ALPHA-0001"].id
        theirs = installs["SN-BETA-0001"].id

        # A write against a competitor's row must be indistinguishable from a
        # write against a row that does not exist. Anything else is an oracle.
        for verb, payload in (("transition", {"target": "Commissioning"}),
                              ("commission", {}),
                              ("service", {"service_hours": 10})):
            c_rival, _, _ = await http(f"/oem/machines/{theirs}/{verb}", alpha,
                                       method="POST", payload=payload)
            c_ghost, _, _ = await http(f"/oem/machines/999999/{verb}", alpha,
                                       method="POST", payload=payload)
            check(f"POST {verb} on a COMPETITOR's machine is refused",
                  c_rival == 404, str(c_rival))
            check(f"...and {verb} is indistinguishable from a nonexistent id",
                  c_rival == c_ghost, f"{c_rival} vs {c_ghost}")

        # A viewer may look and must not act.
        for verb, payload in (("transition", {"target": "Commissioning"}),
                              ("commission", {}),
                              ("service", {"service_hours": 10})):
            c, _, _ = await http(f"/oem/machines/{mine}/{verb}", viewer,
                                 method="POST", payload=payload)
            check(f"a read-only OEM user cannot {verb}", c == 403, str(c))

        # THE ASSIGNMENT VECTOR. An OEM that could set factory_tenant_code could
        # plant equipment at a factory it never sold to.
        # The transition must SUCCEED, or this proves nothing: a body that is
        # ignored because the whole request was rejected is not evidence. The
        # first version of this check fired against a machine whose state made
        # the transition illegal, got a 400, and "passed".
        row = db.query(models.MachineInstallation).filter(
            models.MachineInstallation.id == mine).first()
        row.status = "Installed"
        db.commit()
        before = row.factory_tenant_code
        code_w, _, _ = await http(f"/oem/machines/{mine}/transition", alpha,
                                  method="POST",
                                  payload={"target": "Commissioning",
                                           "factory_tenant_code": "FACTORY_C",
                                           "oem_code": "OEM_BETA",
                                           "machine_id": 99999,
                                           "last_service_hours": 99999})
        ok("CONTROL: the poisoned transition was ACCEPTED, so the body was read",
           code_w == 200, str(code_w))
        db.expire_all()
        row = db.query(models.MachineInstallation).filter(
            models.MachineInstallation.id == mine).first()
        check("a write body cannot move a machine to another customer",
              row.factory_tenant_code == before,
              f"{before} -> {row.factory_tenant_code}")
        check("...nor reassign it to another manufacturer",
              row.oem_code == "OEM_ALPHA", str(row.oem_code))
        check("...nor relink it to another factory's machine",
              row.machine_id != 99999, str(row.machine_id))
        check("...nor forge a service reading through an unrelated endpoint",
              row.last_service_hours != 99999, str(row.last_service_hours))

        # A factory token must not reach the OEM write surface at all.
        for name, tok_ in (("FACTORY_A admin", fac_a),):
            for verb in ("transition", "commission", "service"):
                c, _, _ = await http(f"/oem/machines/{mine}/{verb}", tok_,
                                     method="POST", payload={"target": "Sold"})
                check(f"a {name} token cannot POST {verb}", c in (401, 403), str(c))

        # A legitimate write must not touch anything the FACTORY owns.
        machine_before = db.query(models.Machine).filter(
            models.Machine.tenant_code == "FACTORY_A").first()
        status_before = machine_before.status if machine_before else None
        wo_before = db.query(models.WorkOrder).count()
        c, _, _ = await http(f"/oem/machines/{mine}/service", alpha,
                             method="POST", payload={"service_hours": 123})
        ok("CONTROL: the machine's OWN maker can record a service", c == 200, str(c))
        db.expire_all()
        machine_after = db.query(models.Machine).filter(
            models.Machine.tenant_code == "FACTORY_A").first()
        check("an OEM write leaves the factory's machine status untouched",
              (machine_after.status if machine_after else None) == status_before,
              f"{status_before} -> {machine_after and machine_after.status}")
        check("...and creates or alters no work order",
              db.query(models.WorkOrder).count() == wo_before, "work orders changed")

        # THE EVENT TENANCY. A write's history must land in the CUSTOMER's log,
        # never the founder's and never another factory's.
        logs = db.query(models.EventLog).filter(
            models.EventLog.event_type.in_(
                ["MachineInstalled", "MachineCommissioned", "ServiceCompleted"])).all()
        check("no OEM event was filed under DEFAULT (the founder workspace)",
              not [r for r in logs if r.tenant_code == "DEFAULT"],
              str([r.tenant_code for r in logs]))
        check("no OEM event was filed under an OEM code",
              not [r for r in logs if r.tenant_code in ("OEM_ALPHA", "OEM_BETA")],
              str([r.tenant_code for r in logs]))
        for r in logs:
            body = json.loads(r.payload)
            check(f"[{body['serial_number']}] its event is in ITS customer's log",
                  r.tenant_code == body["tenant_code"], f"{r.tenant_code}")
        hit = [r for r in logs if any(s in (r.payload or "") for s in SECRETS)]
        check("no OEM event payload carries a factory secret", not hit, str(len(hit)))

        # And the notifications raised from them.
        notes = db.query(models.Notification).filter(
            models.Notification.notification_type.like("equipment_%")).all()
        check("no equipment notification landed in DEFAULT",
              not [n for n in notes if n.tenant_code == "DEFAULT"],
              str([n.tenant_code for n in notes]))
        blob = " ".join((n.message or "") + (n.title or "") for n in notes)
        hit = [s for s in SECRETS if s in blob]
        check("no equipment notification carries a factory secret", not hit, str(hit))
        for n in notes:
            others = [t for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C")
                      if t != n.tenant_code and t in (n.message or "")]
            check(f"a {n.tenant_code} notification names no other customer",
                  not others, str(others))

    asyncio.new_event_loop().run_until_complete(run())
    asyncio.new_event_loop().run_until_complete(attack_the_writes())

    db.close()
    engine.dispose()
    print()
    print("=" * 74)
    print(f"{CHECKS[0]} checks")
    if FAILURES:
        print(f"BREACHES ({len(FAILURES)}):")
        for f in FAILURES:
            print("   *", f)
        return 1
    print("NO BREACH FOUND: 2 OEMs, 3 FACTORIES, EVERY BOUNDARY HELD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
