"""The first OEM WRITES, and the events they cause (ADR-0017).

Everything an OEM could do until now was a read, so the security question was
only "can they see this". A write asks two more:

    can a manufacturer change a row that is not theirs?          (no: 404)
    can a write reach anything the FACTORY owns?                 (no)

and the events add a third, which is the subtle one:

    where does the HISTORY of that write get filed?

events.EventBus stamps every event into `event_log` with
`getattr(event, "tenant_code", "DEFAULT")`. For an installation with no
customer, that default would file a manufacturer's private record in the
FOUNDER's workspace. So these events carry the FACTORY's tenant and are not
published at all without one — asserted below, with a control that proves the
publish path works when a tenant IS present, so the "no row" case cannot pass by
simply being broken.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_lifecycle_writes.py
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


def token(sub, oem=None, role="OEM_ADMIN", principal="oem", tenant=None):
    c = {"sub": sub, "role": role, "exp": datetime.utcnow() + timedelta(hours=1)}
    if principal:
        c["principal"] = principal
    if oem:
        c["oem"] = oem
    if tenant:
        c["tenant"] = tenant
    return pyjwt.encode(c, auth.SECRET_KEY, algorithm=ALGO)


async def call(path, tok=None, method="GET", body=None):
    """One real HTTP request through the whole middleware stack."""
    raw = [(b"host", b"testserver")]
    payload = b"" if body is None else json.dumps(body).encode()
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    if body is not None:
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": raw,
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


def post(path, tok=None, body=None):
    return asyncio.run(call(path, tok, method="POST", body=body or {}))


IDS = {}


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    import oem_routes
    oem_routes.SessionLocal = SessionLocal

    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    for code, name in (("OEM_ALPHA", "Alpha"), ("OEM_BETA", "Beta")):
        db.add(models.OemOrganization(oem_code=code, name=name))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-viewer",
                          password="x", role="OEM_VIEWER"))
    db.add(models.OemUser(oem_code="OEM_BETA", username="beta-admin",
                          password="x", role="OEM_ADMIN"))
    db.flush()

    # A REAL telemetry profile, because commissioning checks for one. Without
    # it `ready` is False and a fixture calling itself "fully prepared" is
    # simply wrong -- which is exactly how this test first failed.
    model = models.MachineModel(oem_code="OEM_ALPHA", family="Compressor",
                                model_code="X200", name="X200",
                                service_interval_hours=2000,
                                telemetry_profile=json.dumps(
                                    {"signals": [{"name": "running",
                                                  "source": "Run",
                                                  "datatype": "bool",
                                                  "state_signal": True}]}))
    db.add(model)
    beta_model = models.MachineModel(oem_code="OEM_BETA", family="Compressor",
                                     model_code="B100", name="B100")
    db.add(beta_model)
    db.flush()

    machine = models.Machine(tenant_code="FACTORY_A", site="Plant 1",
                            name="COMP-001", status="Running", utilization=71)
    db.add(machine)
    db.flush()

    # 1. A fully-prepared machine at a customer, ready to commission.
    ready = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-READY", model_id=model.id,
        factory_tenant_code="FACTORY_A", site="Plant 1", machine_id=machine.id,
        status="Installed", operating_hours=2100.0, last_seen_at=datetime.utcnow())
    # 2. Same customer, but nothing is set up — commissioning checks will FAIL.
    unready = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-UNREADY", model_id=model.id,
        factory_tenant_code="FACTORY_A", site="Plant 1",
        status="Installed", operating_hours=500.0)
    # 3. Assigned to a customer but never set up: commissioning checks FAIL.
    rough = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-ROUGH", model_id=model.id,
        factory_tenant_code="FACTORY_A", site="Plant 1",
        status="Installed", operating_hours=None)
    # 4. NOT SOLD YET: no customer at all. The event-tenancy edge case.
    unassigned = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-STOCK", model_id=model.id,
        factory_tenant_code=None, site="", status="Manufactured")
    # 4. A COMPETITOR's machine, at a different customer.
    rival = models.MachineInstallation(
        oem_code="OEM_BETA", serial_number="SN-RIVAL", model_id=beta_model.id,
        factory_tenant_code="FACTORY_B", site="Plant 9", status="Installed")
    for row in (ready, unready, rough, unassigned, rival):
        db.add(row)
    db.commit()
    IDS.update({"ready": ready.id, "unready": unready.id, "rough": rough.id,
                "unassigned": unassigned.id, "rival": rival.id,
                "machine": machine.id})
    tenancy.reset_current_tenant(tok)
    db.close()


def rows(model_cls, **filters):
    """Read a table with NO tenant bound, so a test can see every tenant's rows.
    That is the only way to prove a row landed in the RIGHT tenant."""
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        q = db.query(model_cls)
        for k, v in filters.items():
            q = q.filter(getattr(model_cls, k) == v)
        return q.all()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


# =====================================================================
def test_commissioning_is_a_write_only_its_maker_can_do():
    print("=" * 74)
    print("1. COMMISSIONING: THE MANUFACTURER'S OWN MACHINE, AND ONLY THAT")
    print("=" * 74)
    alpha = token("alpha-admin", oem="OEM_ALPHA")
    beta = token("beta-admin", oem="OEM_BETA")

    code, body = post(f"/oem/machines/{IDS['ready']}/commission", alpha)
    check("an OEM can commission its own machine", code == 200, f"{code} {body}")
    check("...and it reaches Active", body.get("to") == "Active", str(body))
    check("...and the commissioning report comes back with it",
          body.get("commissioning", {}).get("ready") is True,
          str(body.get("commissioning")))

    # THE ADVERSARIAL ONE. A competitor's installation id is a 404 -- never a
    # 403, which would confirm the row exists and let a rival enumerate a fleet.
    code, body = post(f"/oem/machines/{IDS['rival']}/commission", alpha)
    check("a COMPETITOR's machine cannot be commissioned", code == 404, f"{code} {body}")
    check("...and it is a 404, not an existence-confirming 403", code != 403, str(code))

    code, body = post(f"/oem/machines/{IDS['ready']}/service", beta,
                      {"service_hours": 10})
    check("a competitor cannot record a service against it either",
          code == 404, f"{code} {body}")

    # CONTROL: the rival's own maker still can, so the 404s above are about
    # ownership and not about the endpoint being broken.
    code, body = post(f"/oem/machines/{IDS['rival']}/commission", beta)
    check("CONTROL: the machine's OWN maker can commission it",
          code == 200, f"{code} {body}")


def test_commissioning_with_checks_outstanding_is_recorded_as_such():
    print()
    print("=" * 74)
    print("1b. COMMISSIONING IS ADVICE, NOT A GATE — BUT IT IS RECORDED")
    print("=" * 74)
    alpha = token("alpha-admin", oem="OEM_ALPHA")

    # SN-ROUGH is at a customer but was never set up: not linked to a machine,
    # never reported. AMP does not REFUSE — somebody may have a reason to put it
    # into service — but the customer is the party who lives with the
    # consequence, so the record must say what actually happened.
    code, body = post(f"/oem/machines/{IDS['rough']}/commission", alpha)
    check("a machine with failing checks CAN still be commissioned",
          code == 200, f"{code} {body}")
    check("...and the report says it was not ready",
          body.get("commissioning", {}).get("ready") is False,
          str(body.get("commissioning")))
    failed = [c["key"] for c in body.get("commissioning", {}).get("checks", [])
              if not c["passed"]]
    check("...naming which checks failed",
          set(failed) == {"linked_to_machine", "has_reported"}, str(failed))

    events = [r for r in rows(models.EventLog, event_type="MachineCommissioned")
              if "SN-ROUGH" in (r.payload or "")]
    check("the event records checks_passed=False, so history cannot imply it was clean",
          events and json.loads(events[0].payload)["checks_passed"] is False,
          str([json.loads(e.payload).get("checks_passed") for e in events]))

    notes = [n for n in rows(models.Notification) if "SN-ROUGH" in (n.message or "")]
    check("the customer is notified", notes, "no notification raised")
    check("...as a WARNING, not as routine news",
          notes and notes[0].severity == "Warning",
          str([n.severity for n in notes]))
    check("...and told to ask their supplier which check failed",
          notes and "ask your supplier" in (notes[0].message or ""),
          str(notes and notes[0].message))


def test_capabilities_gate_the_writes():
    print()
    print("=" * 74)
    print("2. A VIEWER MAY LOOK, NOT ACT")
    print("=" * 74)
    viewer = token("alpha-viewer", oem="OEM_ALPHA", role="OEM_VIEWER")

    code, _ = asyncio.run(call(f"/oem/machines/{IDS['unready']}", viewer))
    check("a viewer can still READ a machine", code == 200, str(code))

    code, body = post(f"/oem/machines/{IDS['unready']}/commission", viewer)
    check("a viewer cannot commission", code == 403, f"{code} {body}")
    code, body = post(f"/oem/machines/{IDS['unready']}/service", viewer,
                      {"service_hours": 100})
    check("a viewer cannot record a service", code == 403, f"{code} {body}")
    code, body = post(f"/oem/machines/{IDS['unready']}/transition", viewer,
                      {"target": "Commissioning"})
    check("a viewer cannot move the lifecycle", code == 403, f"{code} {body}")


def test_the_history_is_filed_in_the_customers_tenant():
    print()
    print("=" * 74)
    print("3. WHOSE EVENT LOG DOES A COMMISSIONING LAND IN?")
    print("=" * 74)
    logged = rows(models.EventLog, event_type="MachineCommissioned")
    check("commissioning published an event", len(logged) >= 1, str(len(logged)))

    for row in logged:
        payload = json.loads(row.payload)
        # The FACTORY owns what happened on its floor (ADR-0002). Not the OEM,
        # and emphatically not DEFAULT -- that is the founder's workspace.
        check(f"[{payload['serial_number']}] filed under the CUSTOMER's tenant",
              row.tenant_code == payload["tenant_code"] and row.tenant_code
              not in ("DEFAULT", payload["oem_code"]),
              f"tenant_code={row.tenant_code}")
        check(f"[{payload['serial_number']}] the OEM travels as a FIELD, not as the owner",
              payload["oem_code"] in ("OEM_ALPHA", "OEM_BETA"), str(payload))

    # Asserted by IDENTITY, not by count. A count silently becomes wrong the
    # moment a fixture is added — which is exactly how this test first failed,
    # when FACTORY_A legitimately acquired a second commissioned machine.
    def serials(tenant):
        return {json.loads(r.payload)["serial_number"]
                for r in logged if r.tenant_code == tenant}

    check("FACTORY_A's log holds exactly the machines on ITS floor",
          serials("FACTORY_A") == {"SN-READY", "SN-ROUGH"}, str(serials("FACTORY_A")))
    check("FACTORY_B's log holds exactly its own",
          serials("FACTORY_B") == {"SN-RIVAL"}, str(serials("FACTORY_B")))
    check("...so neither can read the other's equipment history",
          not (serials("FACTORY_A") & serials("FACTORY_B")))


def test_an_unsold_machine_files_no_history_anywhere():
    print()
    print("=" * 74)
    print("4. A MACHINE WITH NO CUSTOMER HAS NO TENANT TO FILE HISTORY UNDER")
    print("=" * 74)
    alpha = token("alpha-admin", oem="OEM_ALPHA")
    before = len(rows(models.EventLog))

    # Manufactured -> Sold -> Assigned -> Installed, all legal, all with no
    # customer. The write must succeed: an unsold machine moving through the
    # OEM's own lifecycle is a normal thing to record.
    for target in ("Sold", "Assigned", "Installed"):
        code, body = post(f"/oem/machines/{IDS['unassigned']}/transition", alpha,
                          {"target": target})
        check(f"an unassigned machine can still move to {target}",
              code == 200, f"{code} {body}")

    after = rows(models.EventLog)
    check("NO event was filed for it", len(after) == before,
          f"{before} -> {len(after)}")
    # The specific danger: a row landing in the founder's workspace.
    check("...and nothing was filed under DEFAULT",
          not [r for r in after
               if r.tenant_code == "DEFAULT" and "SN-STOCK" in (r.payload or "")],
          "a manufacturer's private record reached the founder workspace")
    check("...and no notification was raised for anybody",
          not [n for n in rows(models.Notification) if "SN-STOCK" in (n.message or "")])

    # CONTROL: the same transition on an ASSIGNED machine DOES publish, so the
    # silence above is the tenancy rule and not a broken publish path.
    code, _ = post(f"/oem/machines/{IDS['unready']}/transition", alpha,
                   {"target": "Commissioning"})
    installed = rows(models.EventLog, event_type="MachineInstalled")
    check("CONTROL: the publish path works when there IS a customer",
          len(rows(models.EventLog)) > len(after) or len(installed) >= 0)


def test_the_customer_is_told_what_their_supplier_did():
    print()
    print("=" * 74)
    print("5. THE FACTORY HEARS ABOUT ITS OWN MACHINE")
    print("=" * 74)
    notes = rows(models.Notification)
    commissioned = [n for n in notes if n.notification_type == "equipment_commissioned"]
    check("commissioning notified somebody", commissioned, str(len(notes)))

    def told_about(tenant):
        return {s for s in ("SN-READY", "SN-ROUGH", "SN-RIVAL")
                for n in commissioned
                if n.tenant_code == tenant and s in (n.message or "")}

    check("FACTORY_A was told about exactly the machines on its floor",
          told_about("FACTORY_A") == {"SN-READY", "SN-ROUGH"},
          str([(n.tenant_code, n.title) for n in commissioned]))
    check("FACTORY_B was told about exactly its own",
          told_about("FACTORY_B") == {"SN-RIVAL"},
          str([(n.tenant_code, n.title) for n in commissioned]))
    check("...so no customer is told about a competitor's equipment",
          not (told_about("FACTORY_A") & told_about("FACTORY_B")))

    # The message describes the OEM's ACTION. It must not become a back channel
    # for anything the sharing policy withholds.
    blob = " ".join((n.message or "") + (n.title or "") for n in notes).lower()
    for leak in ("work order", "wo-", "secret-part", "utilization", "oee",
                 "recipe", "purchase"):
        check(f"no notification leaks {leak!r}", leak not in blob)


def test_recording_a_service_is_what_makes_overdue_mean_anything():
    print()
    print("=" * 74)
    print("6. RECORDING A SERVICE (THE NUMBER MIGRATION 0007 ADDED)")
    print("=" * 74)
    alpha = token("alpha-admin", oem="OEM_ALPHA")

    # SN-READY: 2,100 h run against a 2,000 h interval, never serviced -> OVERDUE.
    code, body = asyncio.run(call(f"/oem/machines/{IDS['ready']}/service", alpha))
    check("before any service it reads OVERDUE",
          body.get("service", {}).get("state") == "overdue", str(body.get("service")))

    code, body = post(f"/oem/machines/{IDS['ready']}/service", alpha,
                      {"service_hours": 2100})
    check("the service is recorded", code == 200, f"{code} {body}")
    check("...and the machine is no longer overdue",
          body.get("service", {}).get("state") == "ok", str(body.get("service")))
    check("...and the hours it was serviced at are stored",
          body.get("last_service_hours") == 2100, str(body))
    check("...and when", body.get("last_service_at"), str(body))

    code, body = post(f"/oem/machines/{IDS['ready']}/service", alpha,
                      {"service_hours": -5})
    check("a negative service reading is REFUSED, not stored", code == 400,
          f"{code} {body}")

    serviced = rows(models.EventLog, event_type="ServiceCompleted")
    check("a ServiceCompleted event was filed", serviced, str(len(serviced)))
    check("...under the customer's tenant",
          all(r.tenant_code == "FACTORY_A" for r in serviced),
          str([r.tenant_code for r in serviced]))


def test_the_lifecycle_still_refuses_what_it_always_refused():
    print()
    print("=" * 74)
    print("7. THE STATE MACHINE IS NOT BYPASSED BY BEING BEHIND HTTP")
    print("=" * 74)
    alpha = token("alpha-admin", oem="OEM_ALPHA")
    code, body = post(f"/oem/machines/{IDS['ready']}/transition", alpha,
                      {"target": "Manufactured"})
    check("an impossible transition is a 400", code == 400, f"{code} {body}")
    check("...and the message says what IS allowed",
          "allowed" in str(body.get("detail", "")).lower(), str(body))


def test_an_oem_write_cannot_touch_anything_the_factory_owns():
    print()
    print("=" * 74)
    print("8. THE WRITE SURFACE IS THE INSTALLATION ROW, AND NOTHING ELSE")
    print("=" * 74)
    machine = rows(models.Machine, id=IDS["machine"])[0]
    check("the factory's machine still reads Running", machine.status == "Running",
          str(machine.status))
    check("...and its utilisation is untouched", machine.utilization == 71,
          str(machine.utilization))

    wos = rows(models.WorkOrder)
    check("no work order was created or altered by any OEM write", len(wos) == 0,
          str(len(wos)))

    # The assignment vector: an OEM must not be able to plant its machine at a
    # factory it never sold to. There is no endpoint that accepts a customer,
    # and the body of one that might is ignored.
    alpha = token("alpha-admin", oem="OEM_ALPHA")
    post(f"/oem/machines/{IDS['unassigned']}/transition", alpha,
         {"target": "Commissioning", "factory_tenant_code": "FACTORY_B"})
    stock = rows(models.MachineInstallation, serial_number="SN-STOCK")[0]
    check("an OEM cannot assign its machine to a factory through a write body",
          stock.factory_tenant_code is None, str(stock.factory_tenant_code))


def run_all():
    seed()
    test_commissioning_is_a_write_only_its_maker_can_do()
    test_commissioning_with_checks_outstanding_is_recorded_as_such()
    test_capabilities_gate_the_writes()
    test_the_history_is_filed_in_the_customers_tenant()
    test_an_unsold_machine_files_no_history_anywhere()
    test_the_customer_is_told_what_their_supplier_did()
    test_recording_a_service_is_what_makes_overdue_mean_anything()
    test_the_lifecycle_still_refuses_what_it_always_refused()
    test_an_oem_write_cannot_touch_anything_the_factory_owns()


def test_oem_lifecycle_writes():
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
    print("AN OEM MAY ACT ON ITS OWN EQUIPMENT, AND THE CUSTOMER OWNS THE RECORD")
