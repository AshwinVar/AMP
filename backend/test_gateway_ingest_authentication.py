"""The real handler, refusing a gateway that speaks for somebody else's factory.

test_gateway_authorisation.py drives the RULE with fake credentials. This drives
the HANDLER, against a real database, through `mqtt_service.on_message` — which
is what a gateway actually talks to, and the only place "the rule is right" and
"the rule is wired in" can be told apart.

  1. A WORKSPACE WITH NO CREDENTIAL is unchanged. Every existing deployment is
     in that state, and a change that silently required signatures would take
     their telemetry offline the moment it shipped.
  2. ONE CREDENTIAL CLOSES THE WORKSPACE, and REVOKING IT DOES NOT RE-OPEN IT.
  3. THE ATTACK, twice: once at another site, and once at the SAME site name in
     another workspace — the second isolates the workspace comparison, which the
     site comparison would otherwise mask.
  4. A REVOKED GATEWAY stops on the very next packet.
  5. A REFUSAL IS RECORDED where a human will see it, once however many packets.
  6. A RETRIED PRODUCTION MESSAGE does not double-count, and one customer's
     record id never blocks another's.

A PRIVATE ENGINE, not database.engine, and everything inside a function. The
coverage job collects every suite into ONE pytest process; a module-level
drop_all on the shared engine would take the database out from under every
other suite in collection order, which test_mqtt_tenant_identity.py documents at
length and which this file originally did.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_gateway_ingest_authentication.py
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "edge"))

from sqlalchemy import create_engine          # noqa: E402
from sqlalchemy.orm import sessionmaker       # noqa: E402
from sqlalchemy.pool import StaticPool        # noqa: E402

import gateway_auth      # noqa: E402
import models            # noqa: E402
import mqtt_service      # noqa: E402
import tenancy           # noqa: E402
from ampedge import signing   # noqa: E402
from database import Base     # noqa: E402

failures = []

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

PREFIX = mqtt_service.TOPIC_PREFIX
# Generated, not written down. Two reasons: the pre-commit hook rightly refuses
# a literal assigned to a secret-shaped name, and a key of the real shape (256
# bits of hex from issue_secret) exercises the real thing rather than a short
# string that happens to work.
ACME_KEY = gateway_auth.issue_secret()
VICTIM_KEY = gateway_auth.issue_secret()
SPARE_KEY = gateway_auth.issue_secret()
BODY = {"machine": "CNC-01", "status": "Running", "utilization": 80, "downtime": "0 min"}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class Msg:
    def __init__(self, topic, body):
        self.topic = topic
        self.payload = json.dumps(body).encode()


def fresh(sites=(("ACME", "plant-1"), ("VICTIM", "plant-9"))):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    for tenant, site in sites:
        db.add(models.TenantConfig(tenant_code=tenant))
        db.add(models.Machine(tenant_code=tenant, site=site, name="CNC-01", status="Idle",
                              utilization=0, downtime="0 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def credential(db, gateway_id, tenant, site, secret=ACME_KEY, active=True):
    tok = tenancy.set_current_tenant(None)
    db.add(models.GatewayCredential(gateway_id=gateway_id, tenant_code=tenant, site=site,
                                    secret=secret, is_active=active))
    db.commit()
    tenancy.reset_current_tenant(tok)


def status_of(db, tenant):
    tok = tenancy.set_current_tenant(None)
    try:
        m = db.query(models.Machine).filter(models.Machine.tenant_code == tenant,
                                            models.Machine.name == "CNC-01").first()
        db.refresh(m)
        return m.status
    finally:
        tenancy.reset_current_tenant(tok)


def set_status(db, tenant, status):
    tok = tenancy.set_current_tenant(None)
    m = db.query(models.Machine).filter(models.Machine.tenant_code == tenant).first()
    m.status = status
    db.commit()
    tenancy.reset_current_tenant(tok)


def count(db, model, tenant):
    tok = tenancy.set_current_tenant(None)
    try:
        return db.query(model).filter(model.tenant_code == tenant).count()
    finally:
        tenancy.reset_current_tenant(tok)


def signed(tenant, site, gateway_id, key, **extra):
    return signing.sign({**BODY, "tenant": tenant, "site": site, **extra}, gateway_id, key)


def publish(tenant, site, body):
    mqtt_service.on_message(None, None, Msg(f"{PREFIX}/{tenant}/{site}/machines", body))


def test_gateway_ingest_authentication():
    """One function, because the sections build on each other and because
    pytest's single-process collection must import this file without running
    drop_all against a database every other suite is sharing."""
    # Bound HERE, not at import: on_message opens its own session from
    # mqtt_service.SessionLocal, and other MQTT suites point that at their own
    # engines when they RUN.
    #
    # AND RESTORED AFTERWARDS, which the other MQTT suites do not bother with.
    # They get away with it: every one of them sorts AFTER
    # test_live_broadcast_bridge, which calls the real safe_broadcast and
    # asserts what it does. This file sorts BEFORE it ("ga" < "li"), so leaving
    # a no-op behind made that suite fail in the single-process coverage job
    # while passing in the per-file backend job -- a difference that is
    # miserable to debug from a red tick.
    original = (mqtt_service.SessionLocal, mqtt_service.safe_broadcast)
    mqtt_service.SessionLocal = SessionLocal
    mqtt_service.safe_broadcast = lambda event: None
    try:
        _run_sections()
    finally:
        mqtt_service.SessionLocal, mqtt_service.safe_broadcast = original


def _run_sections():

    # ── 1. no credential: nothing changes ───────────────────────────
    section("1. A WORKSPACE WITH NO CREDENTIAL KEEPS THE BEHAVIOUR IT HAS")
    db = fresh()
    publish("ACME", "plant-1", BODY)
    check("an unsigned packet is still accepted where no gateway is registered",
          status_of(db, "ACME") == "Running", status_of(db, "ACME"))

    # ── 2. one credential closes it, and revocation does not re-open ─
    section("2. REGISTERING ONE GATEWAY CLOSES THE WORKSPACE, FOR GOOD")
    db = fresh()
    credential(db, "gw-acme-1", "ACME", "plant-1")
    publish("ACME", "plant-1", BODY)
    check("an UNSIGNED packet is now refused", status_of(db, "ACME") == "Idle",
          status_of(db, "ACME"))

    publish("ACME", "plant-1", signed("ACME", "plant-1", "gw-acme-1", ACME_KEY))
    check("...and a correctly signed one is accepted", status_of(db, "ACME") == "Running",
          status_of(db, "ACME"))

    publish("VICTIM", "plant-9", BODY)
    check("another workspace, with no gateway registered, is unaffected",
          status_of(db, "VICTIM") == "Running", status_of(db, "VICTIM"))

    # REVOKING THE ONLY GATEWAY MUST NOT RE-OPEN THE WORKSPACE. Counting only
    # ACTIVE credentials would mean an operator revoking a compromised gateway
    # hands the workspace back to unsigned traffic -- the exact opposite of what
    # they intended, and a fail-open.
    tok = tenancy.set_current_tenant(None)
    db.query(models.GatewayCredential).filter(
        models.GatewayCredential.gateway_id == "gw-acme-1").first().is_active = False
    db.commit()
    tenancy.reset_current_tenant(tok)
    set_status(db, "ACME", "Idle")
    publish("ACME", "plant-1", BODY)
    check("revoking the LAST gateway leaves the workspace closed, not open",
          status_of(db, "ACME") == "Idle", status_of(db, "ACME"))

    # ── 3. the attack, both ways round ──────────────────────────────
    section("3. A VALID GATEWAY CANNOT PUBLISH INTO ANOTHER CUSTOMER'S FACTORY")
    db = fresh()
    credential(db, "gw-acme-1", "ACME", "plant-1")
    credential(db, "gw-victim-1", "VICTIM", "plant-9", secret=VICTIM_KEY)

    # (a) A DIFFERENT SITE. The attacker owns gw-acme-1 and its key, points the
    # topic at VICTIM and re-signs -- which they can, it is their own key.
    publish("VICTIM", "plant-9",
            signed("VICTIM", "plant-9", "gw-acme-1", ACME_KEY, status="Breakdown"))
    check("the victim's machine was NOT touched", status_of(db, "VICTIM") == "Idle",
          status_of(db, "VICTIM"))
    check("...no downtime log was written into their workspace",
          count(db, models.DowntimeLog, "VICTIM") == 0,
          str(count(db, models.DowntimeLog, "VICTIM")))
    check("...and no machine event", count(db, models.MachineEvent, "VICTIM") == 0,
          str(count(db, models.MachineEvent, "VICTIM")))

    # (b) THE SAME SITE NAME, in another workspace. Without this the site
    # comparison masks the workspace comparison entirely: a mutation deleting
    # the tenant check survived the whole suite until this case existed, because
    # every attack in it also crossed a site boundary.
    db = fresh(sites=(("ACME", "plant-1"), ("VICTIM", "plant-1")))
    credential(db, "gw-acme-1", "ACME", "plant-1")
    credential(db, "gw-victim-1", "VICTIM", "plant-1", secret=VICTIM_KEY)
    publish("VICTIM", "plant-1",
            signed("VICTIM", "plant-1", "gw-acme-1", ACME_KEY, status="Breakdown"))
    check("a gateway at plant-1 cannot publish to ANOTHER WORKSPACE's plant-1",
          status_of(db, "VICTIM") == "Idle", status_of(db, "VICTIM"))
    check("...and the victim's own gateway still works",
          (publish("VICTIM", "plant-1", signed("VICTIM", "plant-1", "gw-victim-1", VICTIM_KEY))
           or status_of(db, "VICTIM")) == "Running", status_of(db, "VICTIM"))

    # ── 4. revocation takes effect on the next packet ───────────────
    section("4. REVOKING A GATEWAY STOPS IT ON THE NEXT PACKET")
    db = fresh()
    credential(db, "gw-acme-1", "ACME", "plant-1")
    credential(db, "gw-acme-2", "ACME", "plant-1", secret=SPARE_KEY)
    tok = tenancy.set_current_tenant(None)
    db.query(models.GatewayCredential).filter(
        models.GatewayCredential.gateway_id == "gw-acme-1").first().is_active = False
    db.commit()
    tenancy.reset_current_tenant(tok)

    publish("ACME", "plant-1", signed("ACME", "plant-1", "gw-acme-1", ACME_KEY))
    check("a revoked gateway's perfectly-signed packet is refused",
          status_of(db, "ACME") == "Idle", status_of(db, "ACME"))
    publish("ACME", "plant-1", signed("ACME", "plant-1", "gw-acme-2", SPARE_KEY))
    check("...while the replacement gateway works", status_of(db, "ACME") == "Running",
          status_of(db, "ACME"))

    # ── 5. the refusal reaches a human, once ────────────────────────
    section("5. A REFUSAL REACHES A HUMAN, AND ONLY ONCE")
    db = fresh()
    credential(db, "gw-acme-1", "ACME", "plant-1")
    credential(db, "gw-victim-1", "VICTIM", "plant-9", secret=VICTIM_KEY)
    # THREE packets, not one: a gateway publishes every second, and an alert
    # that floods is an alert nobody reads. One attack could not tell a working
    # deduplication from a missing one.
    for _ in range(3):
        publish("VICTIM", "plant-9", signed("VICTIM", "plant-9", "gw-acme-1", ACME_KEY))

    tok = tenancy.set_current_tenant(None)
    notes = db.query(models.Notification).filter(
        models.Notification.tenant_code == "VICTIM",
        models.Notification.notification_type == "gateway_auth").all()
    tenancy.reset_current_tenant(tok)
    check("the refused attack was recorded in the TARGET workspace", len(notes) >= 1,
          str(len(notes)))
    check("...exactly once, however many packets arrive", len(notes) == 1, str(len(notes)))
    check("...and tells them what it means if they did not install a gateway",
          notes and "someone is publishing to your workspace" in notes[0].message,
          notes[0].message if notes else "none")
    check("...without leaking the key", notes and ACME_KEY not in notes[0].message,
          "the notification leaked a secret")

    # ── 6. production is written once, per workspace ────────────────
    section("6. A RETRIED PRODUCTION MESSAGE DOES NOT DOUBLE-COUNT THE SHIFT")
    db = fresh()
    production = {"machine": "CNC-01", "status": "Running", "utilization": 80,
                  "downtime": "0 min", "total_count": 40, "good_count": 38,
                  "rejected_count": 2, "planned_minutes": 30, "runtime_minutes": 28,
                  "ideal_cycle_time_seconds": 45, "record_id": "rec-abc-123"}

    for _ in range(3):
        publish("ACME", "plant-1", production)
    check("three deliveries of the same message write ONE production record",
          count(db, models.ProductionRecord, "ACME") == 1,
          str(count(db, models.ProductionRecord, "ACME")))

    tok = tenancy.set_current_tenant(None)
    total = sum(r.total_count for r in db.query(models.ProductionRecord).filter(
        models.ProductionRecord.tenant_code == "ACME"))
    tenancy.reset_current_tenant(tok)
    check("...so the shift's output is 40, not 120", total == 40, str(total))

    publish("ACME", "plant-1", dict(production, record_id="rec-abc-124"))
    check("a different record id IS a new record",
          count(db, models.ProductionRecord, "ACME") == 2,
          str(count(db, models.ProductionRecord, "ACME")))

    # ONE CUSTOMER'S RECORD ID MUST NOT BLOCK ANOTHER'S. Gateways choose their
    # own ids; two customers colliding is ordinary, not adversarial. A dedup
    # query without a tenant filter survived the whole suite until this case
    # existed, and would have silently discarded a second customer's production.
    publish("VICTIM", "plant-9", production)
    check("the SAME record id in another workspace still writes",
          count(db, models.ProductionRecord, "VICTIM") == 1,
          str(count(db, models.ProductionRecord, "VICTIM")))

    # And the paths that carry no record id at all are untouched by any of this.
    no_id = {k: v for k, v in production.items() if k != "record_id"}
    publish("ACME", "plant-1", no_id)
    publish("ACME", "plant-1", no_id)
    check("two messages with NO record id both write, as they always have",
          count(db, models.ProductionRecord, "ACME") == 4,
          str(count(db, models.ProductionRecord, "ACME")))
    db.close()


if __name__ == "__main__":
    test_gateway_ingest_authentication()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")
