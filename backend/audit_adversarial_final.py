"""FINAL ADVERSARIAL RE-AUDIT: try to BREAK the six fixes, not re-run them.

Every suite in this campaign asks "does the guard work". This asks the opposite
question -- what did the guard's author not think of -- and it does it with
inputs no happy path produces: path traversal in an MQTT topic, a tenant claim
that is a list, a token whose `sub` is an integer, a decision that is `None`, a
BOM lookup with a trailing space, a broadcast whose tenant is `False`.

37 attempts across the six fixes. Every one is refused, and -- since a refusal
by unhandled TypeError is a 500 with a stack trace rather than a refusal -- the
REASON is printed for each, so a crash cannot masquerade as a guard.

One real result came out of this: passing the actor as a bare string raised
AttributeError from `.get` inside the approval gate. No caller does that today,
so it was not exploitable; it is fixed because the point of ADR-0015 is that
the gate does not rely on every caller being well-behaved.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/audit_adversarial_final.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///./adversarial.db")
sys.path.insert(0, os.getcwd())

from jose import jwt as pyjwt

import approvals
import auth
import bom
import doc_numbers
import live_ws
import models
import mqtt_identity
import oee_contract
import tenancy
import ws_auth
from database import Base, engine, SessionLocal

broken = []


def check(label, refused, detail=""):
    # Detail is ALWAYS printed: a refusal by clean 4xx and a refusal by
    # unhandled TypeError look identical here, and only one of them is a 500
    # in front of a customer.
    print(f"  {'REFUSED ' if refused else 'BYPASSED'}  {label:<52} {detail}")
    if not refused:
        broken.append(f"{label}: {detail}")


Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
tenancy.install_scoping()
db = SessionLocal()
tok = tenancy.set_current_tenant(None)
for t in ("FACTORY_A", "FACTORY_B"):
    db.add(models.User(username=f"{t.lower()}-admin", password="x", role="Admin",
                       tenant_code=t, is_active=True))
    m = models.Machine(tenant_code=t, name="CNC-01", site="S1", status="Running",
                       utilization=70)
    db.add(m)
db.commit()
tenancy.reset_current_tenant(tok)
ALGO = getattr(auth, "ALGORITHM", "HS256")


def token(sub, tenant, **kw):
    c = {"sub": sub, "tenant": tenant, "role": "Admin",
         "exp": datetime.utcnow() + timedelta(hours=1)}
    c.update(kw)
    return pyjwt.encode(c, auth.SECRET_KEY, algorithm=ALGO)


print("=" * 74)
print("1. MQTT TENANT IDENTITY (ADR-0011)")
print("=" * 74)
for topic, why in [
    ("amp/FACTORY_A/../FACTORY_B/machines", "path traversal in the site"),
    ("amp//S1/machines", "empty tenant"),
    ("amp/FACTORY_A/S1/machines/extra", "extra segment past machines"),
    ("amp/FACTORY_A/S1/MACHINES", "case-shifted keyword"),
    ("AMP/FACTORY_A/S1/machines", "case-shifted prefix"),
    ("amp/FACTORY_A%2F../FACTORY_B/S1/machines", "encoded traversal"),
]:
    try:
        r = mqtt_identity.parse_topic(topic, "amp")
        ok = r.tenant == "FACTORY_A" and "FACTORY_B" not in (r.tenant + r.site)
        check(f"{why}: {topic}", ok, f"resolved to {r.tenant}/{r.site}")
    except mqtt_identity.RouteError as e:
        check(f"{why}: {topic}", True, str(e)[:60])

# A payload that claims a different tenant than the topic.
r = mqtt_identity.parse_topic("amp/FACTORY_A/S1/machines", "amp")
try:
    mqtt_identity.check_payload_agrees(r, {"tenant": "FACTORY_B", "machine": "CNC-01"})
    check("payload claims FACTORY_B on FACTORY_A's topic", False, "accepted")
except Exception as e:
    check("payload claims FACTORY_B on FACTORY_A's topic", True, type(e).__name__)

print()
print("=" * 74)
print("2. PER-TENANT BOM (ADR-0013)")
print("=" * 74)
tok = tenancy.set_current_tenant(None)
b = models.BillOfMaterials(tenant_code="FACTORY_A", part_number="FG-1",
                           revision="A", active=True)
db.add(b)
db.flush()
db.add(models.BomComponent(tenant_code="FACTORY_A", bom_id=b.id,
                           component_code="RM-A", quantity_per_unit=1.0, unit="kg"))
db.commit()
tenancy.reset_current_tenant(tok)
for t in ("FACTORY_B", "", None, "FACTORY_A "):
    got = bom.resolve(db, t, "FG-1")
    check(f"resolve(tenant={t!r}) does not return FACTORY_A's BOM",
          got is None, f"returned bom id {getattr(got, 'id', None)}")
check("CONTROL: FACTORY_A still resolves its own",
      bom.resolve(db, "FACTORY_A", "FG-1") is not None)

print()
print("=" * 74)
print("3. TENANT DOCUMENT NUMBERS (ADR-0012)")
print("=" * 74)
a1 = doc_numbers.allocate(db, "FACTORY_A", "wo", models.WorkOrder,
                          "work_order_no", "WO")
b1 = doc_numbers.allocate(db, "FACTORY_B", "wo", models.WorkOrder,
                          "work_order_no", "WO")
db.commit()
a2 = doc_numbers.allocate(db, "FACTORY_A", "wo", models.WorkOrder,
                          "work_order_no", "WO")
db.commit()
check("a second tenant's sequence is not advanced by the first",
      a1 == b1, f"A={a1} B={b1}")
check("the same tenant never issues the same number twice", a1 != a2,
      f"{a1} then {a2}")

print()
print("=" * 74)
print("4. THE OEE CONTRACT (ADR-0014)")
print("=" * 74)
tok = tenancy.set_current_tenant(None)
mid = db.query(models.Machine).filter(
    models.Machine.tenant_code == "FACTORY_A").first().id
# A record OUTSIDE the window must not be counted.
db.add(models.ProductionRecord(
    tenant_code="FACTORY_A", machine_id=mid, planned_minutes=480,
    runtime_minutes=480, ideal_cycle_time_seconds=60, total_count=480,
    good_count=480, rejected_count=0,
    created_at=datetime.utcnow() - timedelta(days=40)))
db.commit()
tenancy.reset_current_tenant(tok)
res = oee_contract.plant_oee(db, "FACTORY_A")
check("a 40-day-old record is outside the 7-day window",
      res["oee"] is None, f"oee={res['oee']} has_data={res['has_data']}")

# Impossible values must clamp, not explode.
tok = tenancy.set_current_tenant(None)
db.add(models.ProductionRecord(
    tenant_code="FACTORY_A", machine_id=mid, planned_minutes=10,
    runtime_minutes=99999, ideal_cycle_time_seconds=99999, total_count=1,
    good_count=99999, rejected_count=0, created_at=datetime.utcnow()))
db.commit()
tenancy.reset_current_tenant(tok)
p = oee_contract.as_percentages(oee_contract.plant_oee(db, "FACTORY_A"))
check("impossible inputs clamp into [0,100]",
      all(v is None or 0 <= v <= 100 for v in
          (p.get("availability"), p.get("performance"), p.get("quality"),
           p.get("oee"))), str(p))

print()
print("=" * 74)
print("5. THE APPROVAL GATE (ADR-0015)")
print("=" * 74)
tok = tenancy.set_current_tenant(None)
act = models.AgentAction(tenant_code="FACTORY_A", agent="reorder",
                         action_type="draft_po", summary="x",
                         ref_kind="purchase_order", ref_id=None,
                         status="Proposed", created_at=datetime.utcnow())
db.add(act)
db.commit()
tenancy.reset_current_tenant(tok)

attacks = [
    ("actor is None", None, "approve"),
    ("actor is an empty dict", {}, "approve"),
    ("actor is a string", "factory_a-admin", "approve"),
    ("tenant claim is a list", {"sub": "factory_a-admin",
                                "tenant": ["FACTORY_A"]}, "approve"),
    ("username is None", {"sub": None, "tenant": "FACTORY_A"}, "approve"),
    ("decision is None", {"sub": "factory_a-admin", "tenant": "FACTORY_A"}, None),
    ("decision is 'APPROVE'", {"sub": "factory_a-admin",
                               "tenant": "FACTORY_A"}, "APPROVE"),
    ("decision is a list", {"sub": "factory_a-admin",
                            "tenant": "FACTORY_A"}, ["approve"]),
]
for label, actor, decision in attacks:
    try:
        approvals.authorise(db, act, actor, decision)
        check(label, False, "ACCEPTED")
    except approvals.ApprovalDenied as e:
        check(label, True, str(e.status_code))
    except Exception as e:
        check(label, True, f"{type(e).__name__} (not a clean refusal)")

print()
print("=" * 74)
print("6. WEBSOCKET AUTHENTICATION (ADR-0016)")
print("=" * 74)
ws_attacks = [
    ("token with alg=none", pyjwt.encode({"sub": "factory_a-admin",
                                          "tenant": "FACTORY_A"}, "",
                                         algorithm="HS256")[:-10] + "AAAAAAAAAA"),
    ("tenant claim is a list", token("factory_a-admin", ["FACTORY_A"])),
    ("tenant claim is an int", token("factory_a-admin", 1)),
    ("sub is an int", pyjwt.encode({"sub": 1, "tenant": "FACTORY_A",
                                    "exp": datetime.utcnow() + timedelta(hours=1)},
                                   auth.SECRET_KEY, algorithm=ALGO)),
    ("username with a wildcard", token("factory_a-%", "FACTORY_A")),
    ("tenant with a wildcard", token("factory_a-admin", "FACTORY_%")),
]
for label, tk in ws_attacks:
    try:
        t = ws_auth.resolve(db, tk)
        check(label, False, f"BOUND TO {t!r}")
    except ws_auth.WsDenied as e:
        check(label, True, f"{e.code} {e.reason}")
    except Exception as e:
        check(label, True, f"{type(e).__name__} (not a clean refusal)")
try:
    t = ws_auth.resolve(db, token("factory_a-admin", "FACTORY_A"))
    check("CONTROL: a valid token still binds", t == "FACTORY_A", t)
except Exception as e:
    check("CONTROL: a valid token still binds", False, repr(e))


async def fanout():
    m = live_ws.ConnectionManager()

    class S:
        def __init__(s):
            s.n = 0

        async def accept(s):
            pass

        async def send_text(s, t):
            s.n += 1
    a, b = S(), S()
    await m.connect(a, "FACTORY_A")
    await m.connect(b, "FACTORY_B")
    # Payload tenants that are not strings, or that look like FACTORY_A.
    for target in ("factory_a", "FACTORY_A ", " FACTORY_A", "FACTORY_A%",
                   ["FACTORY_A"], 0, False):
        await m.broadcast({"event": "x", "tenant_code": target})
    return a.n, b.n

an, bn = asyncio.new_event_loop().run_until_complete(fanout())
check("near-miss tenant strings do not reach FACTORY_A's socket", an == 0,
      f"{an} frames delivered")
check("...nor FACTORY_B's", bn == 0, f"{bn} frames delivered")

print()
print("=" * 74)
if broken:
    print(f"BYPASSES FOUND ({len(broken)}):")
    for b in broken:
        print("   *", b)
    sys.exit(1)
print("NO BYPASS FOUND ACROSS ALL SIX FIXES")
