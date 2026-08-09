"""Every MQTT message resolves to exactly one tenant, or it is dropped.

THE BUG THIS LOCKS DOWN
-----------------------
Measured on master before the fix, with FACTORY_A/CNC-01 and FACTORY_B/CNC-01
both present and a single packet published for CNC-01:

    which machine did it hit?
       FACTORY_A  CNC-01   status=Breakdown  util=3     <- wrong customer
       FACTORY_B  CNC-01   status=Running    util=50
    child rows and their tenant:
       ProductionRecord   tenant_code=DEFAULT
       DowntimeLog        tenant_code=DEFAULT
       MachineEvent       tenant_code=DEFAULT

Two distinct defects: resolution by name alone picked an arbitrary customer,
and the child rows named a tenant that was not either of them.

Run: DATABASE_URL="sqlite:///./ci.db" python test_mqtt_tenant_identity.py
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_mqtt_identity.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import mqtt_identity  # noqa: E402
import mqtt_service  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402

# A PRIVATE engine, not database.engine. fresh_db() calls drop_all between
# sections, and under the single-process pytest run (the coverage job) the
# shared engine points at a database every other suite is also using — dropping
# its tables mid-session would fail unrelated suites in collection order, which
# is a miserable thing to debug. This suite is the only one in the repo that
# drops tables, so it owns its own database.
#
# PostgreSQL is honoured when DATABASE_URL names one, because running these
# exact assertions against real Postgres is the point (NULL != NULL, real FK
# enforcement, real UNIQUE). Nobody runs pytest against Postgres, so the
# drop_all hazard does not apply there — and audit runs point it at a
# disposable scratch database.
_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

# main.py installs this at boot; a bare test import does not, and without it the
# ADR-0002 hook is simply not registered. The row-level sections below filter by
# tenant_code explicitly and so were sound either way, but the OEE section reads
# the way a REQUEST does — relying on the hook — and read the pooled figure for
# all three factories until this call was here. That is the honest reason it is
# present: the test was measuring nothing.
tenancy.install_scoping()

TENANTS = ["FACTORY_A", "FACTORY_B", "FACTORY_C"]
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class Msg:
    """The two fields paho's on_message callback reads off a message."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = (payload if isinstance(payload, bytes)
                        else json.dumps(payload).encode())


# Every session this file opens, so fresh_db() can close them before dropping.
# On SQLite an abandoned session is harmless. On PostgreSQL it sits
# idle-in-transaction holding locks, and the next drop_all() blocks on it
# forever — measured: this suite hung past a 10-minute timeout against real
# PostgreSQL until fresh_db() closed the previous session. Exactly the kind of
# divergence that makes "the tests pass on SQLite" not mean very much.
_open_sessions = []


def fresh_db():
    while _open_sessions:
        try:
            _open_sessions.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    _open_sessions.append(db)
    # Provision the three tenants the way onboarding does, so ingest is allowed
    # to accept them at all.
    #
    # DEFAULT is provisioned too, and that is deliberate rather than incidental.
    # Every real deployment has a DEFAULT tenant — it is the seeded factory — so
    # a test database WITHOUT one lets the provisioning gate catch mistakes that
    # production would wave through. Measured: a mutation making the legacy topic
    # fall back to DEFAULT survived the whole suite until this row existed,
    # because the fallback tenant happened not to exist in the fixture. It does
    # exist in production, which is precisely where it would have mattered.
    for t in TENANTS + ["DEFAULT"]:
        db.add(models.TenantConfig(tenant_code=t))
    db.commit()
    return db


def publish(topic, payload):
    mqtt_service.on_message(None, None, Msg(topic, payload))


def telemetry(machine, status, util, total, good, rejected, downtime="0 min"):
    return {"machine": machine, "status": status, "utilization": util,
            "downtime": downtime, "total_count": total, "good_count": good,
            "rejected_count": rejected}


def rows(db, model, tenant=None):
    tok = tenancy.set_current_tenant(None)
    q = db.query(model)
    if tenant:
        q = q.filter(model.tenant_code == tenant)
    out = q.all()
    tenancy.reset_current_tenant(tok)
    return out


def test_mqtt_tenant_identity():
    """One test function, because the sections build on each other and the
    repo's runner contract is `python test_X.py` — see docs/TESTING.md. Kept as
    a function rather than module-level statements so pytest's single-process
    collection imports this file without executing it."""
    # Bound HERE, not at import time. on_message opens its own session from
    # mqtt_service.SessionLocal, and test_mqtt_resilience / test_mqtt_service
    # both point that at their own engines when they RUN. Under the per-file
    # runner only one suite exists per process so import-time assignment worked;
    # under pytest they run first (alphabetically) and this suite then wrote
    # into their database and read from its own — every assertion failed with
    # "nothing was written", which is exactly what a vacuous test looks like.
    mqtt_service.SessionLocal = SessionLocal
    mqtt_service.safe_broadcast = lambda event: None
    # =====================================================================
    print("=" * 70)
    print("1. THREE FACTORIES, THE SAME MACHINE NAME, DIFFERENT TELEMETRY")
    print("=" * 70)

    db = fresh_db()
    for t in TENANTS:
        db.add(models.Machine(name="CNC-01", site="", status="Running",
                              utilization=50, tenant_code=t))
    db.commit()

    # Deliberately different in every field, so a leak cannot be mistaken for a
    # coincidence: if FACTORY_A ends up at 11% it can ONLY have come from A's packet.
    PROFILE = {
        "FACTORY_A": dict(status="Running",   util=11, total=100, good=100, rejected=0),
        "FACTORY_B": dict(status="Breakdown", util=22, total=200, good=180, rejected=20),
        "FACTORY_C": dict(status="Idle",      util=33, total=300, good=270, rejected=30),
    }
    for t, p in PROFILE.items():
        publish(f"flowmes/{t}/-/machines",
                telemetry("CNC-01", p["status"], p["util"], p["total"], p["good"],
                          p["rejected"], downtime="45 min"))

    db.expire_all()
    for t, p in PROFILE.items():
        m = [x for x in rows(db, models.Machine, t) if x.name == "CNC-01"][0]
        check(f"{t}/CNC-01 has its own status+utilization",
              m.status == p["status"] and m.utilization == p["util"],
              f"got status={m.status} util={m.utilization}, expected "
              f"{p['status']}/{p['util']}")

    print()
    print("  child rows carry the owning tenant, not DEFAULT:")
    for t, p in PROFILE.items():
        prod = rows(db, models.ProductionRecord, t)
        check(f"{t} production record exists and is its own",
              len(prod) == 1 and prod[0].total_count == p["total"],
              f"n={len(prod)} counts={[r.total_count for r in prod]}")

    check("no child row landed under DEFAULT",
          not rows(db, models.ProductionRecord, "DEFAULT")
          and not rows(db, models.DowntimeLog, "DEFAULT")
          and not rows(db, models.MachineEvent, "DEFAULT"),
          f"prod={len(rows(db, models.ProductionRecord, 'DEFAULT'))} "
          f"down={len(rows(db, models.DowntimeLog, 'DEFAULT'))} "
          f"evt={len(rows(db, models.MachineEvent, 'DEFAULT'))}")

    # Only FACTORY_B transitioned INTO Breakdown, so only B may have a downtime log.
    downs = {t: len(rows(db, models.DowntimeLog, t)) for t in TENANTS}
    check("only the factory that broke down has a DowntimeLog",
          downs == {"FACTORY_A": 0, "FACTORY_B": 1, "FACTORY_C": 0}, str(downs))

    # CONTROL: the assertions above are also satisfied by "nothing was written at
    # all". Prove the pipeline actually ran.
    total_prod = len(rows(db, models.ProductionRecord))
    check("CONTROL: ingest actually wrote something (not vacuously clean)",
          total_prod == 3, f"expected 3 production records, got {total_prod}")

    # ---------------------------------------------------------------------
    # The campaign asks for the DERIVED values too, not just the rows: an OEE
    # figure is what the customer actually looks at, and it is computed by pooling
    # production records. If ingest misfiled even one record, the number on
    # somebody's dashboard is wrong even though every row "exists".
    print()
    print("  the OEE each factory reads is computed from its own records only:")
    oee_db = fresh_db()
    for t in TENANTS:
        oee_db.add(models.Machine(name="CNC-01", site="", status="Running",
                                  utilization=50, tenant_code=t))
    oee_db.commit()

    # Chosen so all three OEE values are DIFFERENT and none is zero. Equal values
    # would be satisfied by a leak; a zero would be satisfied by no data at all.
    #   A: avail 480/480=1.00  perf (60*480)/(480*60)=1.00  qual 480/480=1.00 -> 100
    #   B: avail 240/480=0.50  perf (60*240)/(240*60)=1.00  qual 120/240=0.50 ->  25
    #   C: avail 120/480=0.25  perf (60*60)/(120*60)=0.50   qual  60/60 =1.00 ->  12
    #      (0.125 -> 12.5 -> 12: pooled_oee uses round(), which is banker's rounding,
    #       so an exact .5 goes to the EVEN integer. Written out because 13 is the
    #       intuitive answer and this test asserted it until measurement said 12.)
    OEE_INPUT = {
        "FACTORY_A": dict(planned=480, runtime=480, total=480, good=480, expected=100),
        "FACTORY_B": dict(planned=480, runtime=240, total=240, good=120, expected=25),
        "FACTORY_C": dict(planned=480, runtime=120, total=60,  good=60,  expected=12),
    }
    for t, v in OEE_INPUT.items():
        publish(f"flowmes/{t}/-/machines", {
            "machine": "CNC-01", "status": "Running", "utilization": 50,
            "downtime": "0 min", "planned_minutes": v["planned"],
            "runtime_minutes": v["runtime"], "ideal_cycle_time_seconds": 60,
            "total_count": v["total"], "good_count": v["good"],
            "rejected_count": v["total"] - v["good"]})

    import analytics_engine  # noqa: E402

    oee_db.expire_all()
    measured = {}
    for t, v in OEE_INPUT.items():
        # Read exactly the way a request does: bound to the tenant, letting the
        # ADR-0002 hook scope the query rather than filtering by hand.
        tok = tenancy.set_current_tenant(t)
        measured[t] = analytics_engine.pooled_oee(
            oee_db.query(models.ProductionRecord).all())["oee"]
        tenancy.reset_current_tenant(tok)
        check(f"{t} reads OEE {v['expected']}, computed from its own production",
              measured[t] == v["expected"], f"got {measured[t]}")

    check("CONTROL: the three factories read three DIFFERENT numbers",
          len(set(measured.values())) == 3, str(measured))
    oee_db.close()

    # =====================================================================
    print()
    print("=" * 70)
    print("2. THE SAME MACHINE NAME AT TWO SITES OF ONE CUSTOMER")
    print("=" * 70)

    db = fresh_db()
    publish("flowmes/FACTORY_A/PLANT1/machines", telemetry("LINE-01", "Running", 61, 10, 10, 0))
    publish("flowmes/FACTORY_A/PLANT2/machines", telemetry("LINE-01", "Idle", 12, 20, 20, 0))
    db.expire_all()

    a_machines = {(m.site, m.name): m for m in rows(db, models.Machine, "FACTORY_A")}
    check("two distinct machines were created, one per site",
          len(a_machines) == 2 and ("PLANT1", "LINE-01") in a_machines
          and ("PLANT2", "LINE-01") in a_machines, str(sorted(a_machines)))
    if len(a_machines) == 2:
        check("each site's machine kept its own reading",
              a_machines[("PLANT1", "LINE-01")].utilization == 61
              and a_machines[("PLANT2", "LINE-01")].utilization == 12,
              str({k: v.utilization for k, v in a_machines.items()}))

    # =====================================================================
    print()
    print("=" * 70)
    print("3. FAIL CLOSED — UNROUTABLE MESSAGES WRITE NOTHING")
    print("=" * 70)

    db = fresh_db()
    db.add(models.Machine(name="CNC-01", site="", status="Running", utilization=50,
                          tenant_code="FACTORY_A"))
    db.commit()
    before = len(rows(db, models.Machine))

    REJECTED = [
        ("legacy topic with no MQTT_LEGACY_TENANT configured", "flowmes/machines"),
        ("unprovisioned tenant", "flowmes/FACTORY_ZZZ/-/machines"),
        ("empty tenant segment", "flowmes//-/machines"),
        ("tenant containing a path traversal", "flowmes/../-/machines"),
        ("topic outside the configured prefix", "other/FACTORY_A/-/machines"),
        ("too few segments", "flowmes/FACTORY_A/machines"),
        ("wrong leaf segment", "flowmes/FACTORY_A/-/sensors"),
    ]
    for label, topic in REJECTED:
        publish(topic, telemetry("CNC-01", "Breakdown", 3, 50, 40, 10))
        db.expire_all()
        m = rows(db, models.Machine, "FACTORY_A")[0]
        check(f"rejected: {label}",
              m.status == "Running" and len(rows(db, models.Machine)) == before
              and not rows(db, models.ProductionRecord),
              f"status={m.status} machines={len(rows(db, models.Machine))} "
              f"prod={len(rows(db, models.ProductionRecord))}")

    # A payload that claims a different tenant than the broker authorised.
    publish("flowmes/FACTORY_A/-/machines",
            dict(telemetry("CNC-01", "Breakdown", 3, 50, 40, 10), tenant="FACTORY_B"))
    db.expire_all()
    check("rejected: payload tenant contradicts the topic",
          rows(db, models.Machine, "FACTORY_A")[0].status == "Running"
          and not rows(db, models.ProductionRecord),
          "a gateway rewrote its own tenant and was believed")

    # CONTROL: the same machine, same payload, on a VALID topic must be accepted —
    # otherwise every assertion above passes because ingest is simply broken.
    publish("flowmes/FACTORY_A/-/machines", telemetry("CNC-01", "Breakdown", 3, 50, 40, 10))
    db.expire_all()
    check("CONTROL: the valid form of the same message IS accepted",
          rows(db, models.Machine, "FACTORY_A")[0].status == "Breakdown"
          and len(rows(db, models.ProductionRecord, "FACTORY_A")) == 1,
          "the reject cases may be passing vacuously")

    # =====================================================================
    print()
    print("=" * 70)
    print("4. THE LEGACY SINGLE-TENANT TOPIC, ONLY WHEN CONFIGURED")
    print("=" * 70)

    db = fresh_db()
    db.add(models.Machine(name="CNC-01", site="", status="Running", utilization=50,
                          tenant_code="FACTORY_A"))
    db.commit()

    original = mqtt_service.LEGACY_TENANT
    try:
        mqtt_service.LEGACY_TENANT = "FACTORY_A"
        publish("flowmes/machines", telemetry("CNC-01", "Breakdown", 7, 50, 40, 10))
        db.expire_all()
        m = rows(db, models.Machine, "FACTORY_A")[0]
        prod = rows(db, models.ProductionRecord, "FACTORY_A")
        check("configured legacy tenant accepts the old topic",
              m.status == "Breakdown" and len(prod) == 1,
              f"status={m.status} prod={len(prod)}")
        check("legacy-routed child rows carry the legacy tenant, not DEFAULT",
              not rows(db, models.ProductionRecord, "DEFAULT"))
    finally:
        mqtt_service.LEGACY_TENANT = original

    check("the legacy filter is only subscribed when configured",
          mqtt_identity.topic_filters("flowmes", "") == ["flowmes/+/+/machines"]
          and "flowmes/machines" in mqtt_identity.topic_filters("flowmes", "ACME"),
          str(mqtt_identity.topic_filters("flowmes", "")))

    # =====================================================================
    print()
    print("=" * 70)
    print("5. THE DATABASE ITSELF REFUSES A DUPLICATE IDENTITY")
    print("=" * 70)

    db = fresh_db()
    db.add(models.Machine(name="CNC-01", site="", status="Running", utilization=50,
                          tenant_code="FACTORY_A"))
    db.commit()
    try:
        db.add(models.Machine(name="CNC-01", site="", status="Idle", utilization=0,
                              tenant_code="FACTORY_A"))
        db.commit()
        check("duplicate (tenant, site, name) rejected by the constraint", False,
              "the second insert succeeded")
    except Exception as exc:
        db.rollback()
        check("duplicate (tenant, site, name) rejected by the constraint",
              "uq_machine_identity" in str(exc) or "UNIQUE" in str(exc).upper(),
              repr(exc)[:120])

    # The constraint has to hold for a machine created WITHOUT naming a site, which
    # is how every non-MQTT path creates one (HTTP route, CSV import, seeding). If
    # `site` were nullable those rows would land NULL, and NULL != NULL in both
    # PostgreSQL and SQLite — the UNIQUE constraint would be present, visibly
    # correct in the schema, and enforcing nothing at all. This is the assertion
    # that fails if the column is ever loosened; the one above passes either way
    # because it sets site explicitly.
    check("site is NOT NULL, so the constraint cannot be voided by NULL != NULL",
          models.Machine.__table__.c.site.nullable is False)
    try:
        for _ in range(2):
            db.add(models.Machine(name="DRILL-7", status="Idle", utilization=0,
                                  tenant_code="FACTORY_C"))
            db.commit()
        check("two site-less machines with one name in one tenant are rejected",
              False, "both inserts succeeded — the constraint is not enforcing")
    except Exception as exc:
        db.rollback()
        check("two site-less machines with one name in one tenant are rejected",
              "uq_machine_identity" in str(exc) or "UNIQUE" in str(exc).upper(),
              repr(exc)[:120])

    # Same name, different tenant, must still be allowed — the constraint must not
    # have overshot into "machine names are globally unique", which would make it
    # impossible to onboard a second customer who owns a CNC-01.
    try:
        db.add(models.Machine(name="CNC-01", site="", status="Idle", utilization=0,
                              tenant_code="FACTORY_B"))
        db.commit()
        check("the same name in a DIFFERENT tenant is still allowed", True)
    except Exception as exc:
        db.rollback()
        check("the same name in a DIFFERENT tenant is still allowed", False,
              repr(exc)[:120])

    # =====================================================================
    print()
    print("=" * 70)
    print("5b. LOSING THE CREATE RACE DOES NOT DROP THE MESSAGE")
    print("=" * 70)

    # This failure mode is CREATED BY the uniqueness constraint above, so closing it
    # belongs to this change. AMP can run more than one instance, each with its own
    # MQTT listener, so two processes can both miss the SELECT for a newly-seen
    # machine and both attempt the insert. Without recovery the loser's whole packet
    # is rolled away - a permanent hole in that machine's history, occurring exactly
    # once per machine, during commissioning, while someone watches the screen to
    # check the integration works.
    #
    # The race is INJECTED rather than raced on the wall clock: a real two-thread
    # test would depend on timing and could pass on a fast machine while the bug
    # survived. Creating the row from a second session at the exact moment between
    # our SELECT and our INSERT reproduces the same interleaving every run.
    db = fresh_db()
    other = SessionLocal()
    _open_sessions.append(other)
    route = mqtt_identity.Route("FACTORY_A", "")
    real_add = db.add
    raced = {"done": False}


    def add_but_lose_the_race(obj):
        """Stand in for another instance committing the same identity first."""
        if not raced["done"] and isinstance(obj, models.Machine):
            raced["done"] = True
            tok2 = tenancy.set_current_tenant(None)
            other.add(models.Machine(tenant_code="FACTORY_A", site="", name="RACE-01",
                                     status="Running", utilization=77, downtime="0 min"))
            other.commit()
            tenancy.reset_current_tenant(tok2)
        real_add(obj)


    db.add = add_but_lose_the_race
    try:
        got = mqtt_service.get_or_create_machine(db, route, "RACE-01")
        check("the loser recovers and returns the winner's row",
              got is not None and got.utilization == 77,
              f"got {got and got.utilization}")
    except Exception as exc:
        check("the loser recovers and returns the winner's row", False,
              f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        db.add = real_add

    check("CONTROL: the race actually happened (not a silent no-op)", raced["done"])
    survivors = [m for m in rows(db, models.Machine, "FACTORY_A") if m.name == "RACE-01"]
    check("exactly one RACE-01 exists afterwards", len(survivors) == 1,
          f"{len(survivors)} rows")
    other.close()

    # =====================================================================
    print()
    print("=" * 70)
    print("6. THE BROADCAST NAMES ITS TENANT")
    print("=" * 70)

    db = fresh_db()
    captured = []
    original_broadcast = mqtt_service.safe_broadcast
    try:
        mqtt_service.safe_broadcast = captured.append
        publish("flowmes/FACTORY_C/-/machines", telemetry("WELD-9", "Running", 44, 10, 10, 0))
    finally:
        mqtt_service.safe_broadcast = original_broadcast

    check("a live event was broadcast", len(captured) == 1, str(len(captured)))
    if captured:
        ev = captured[0]
        check("the event names the owning tenant",
              ev.get("tenant_code") == "FACTORY_C", repr(ev.get("tenant_code")))
        check("the event names the site, so a name alone cannot address it",
              ev.get("machine", {}).get("site") == "", repr(ev.get("machine")))

    db.close()


    # =====================================================================
    print()
    print("=" * 70)
    print("7. A DUPLICATE NAME IS A 409, NOT A 500")
    print("=" * 70)

    # The constraint is new, so the error it produces is this change's
    # responsibility. An unhandled IntegrityError is a 500 AND a poisoned session:
    # every later query in the same request then fails with PendingRollbackError,
    # so one duplicate name would break the rest of the operator's page load.
    import machines_routes  # noqa: E402
    import schemas  # noqa: E402
    from fastapi import HTTPException  # noqa: E402

    db = fresh_db()
    ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


    def create(name):
        return machines_routes.create_machine(
            schemas.MachineCreate(name=name, status="Running", utilization=50,
                                  downtime="0 min"),
            db=db, current_user=ADMIN)


    first = create("DUPE-01")
    check("the first create succeeds", first is not None and first.name == "DUPE-01")

    try:
        create("DUPE-01")
        check("the duplicate create raises 409, not a 500", False,
              "the duplicate was accepted")
    except HTTPException as exc:
        check("the duplicate create raises 409, not a 500", exc.status_code == 409,
              f"status={exc.status_code} detail={exc.detail}")
        check("the 409 names the machine so the operator knows what to change",
              "DUPE-01" in str(exc.detail), str(exc.detail))
    except Exception as exc:
        check("the duplicate create raises 409, not a 500", False,
              f"raw {type(exc).__name__} escaped the route: {str(exc)[:100]}")

    # The session must still be usable. Without the rollback this raises
    # PendingRollbackError and every later read in the request dies with it.
    try:
        still_readable = len(rows(db, models.Machine))
        check("the session survives the rejected duplicate", still_readable == 1,
              f"{still_readable} machines visible")
    except Exception as exc:
        check("the session survives the rejected duplicate", False,
              f"{type(exc).__name__}: {str(exc)[:100]}")

    # CONTROL: a DIFFERENT name still creates, so the 409 is the constraint firing
    # rather than creates being broken outright.
    try:
        check("CONTROL: a differently-named machine still creates",
              create("DUPE-02").name == "DUPE-02")
    except Exception as exc:
        check("CONTROL: a differently-named machine still creates", False,
              f"{type(exc).__name__}: {str(exc)[:100]}")

    db.close()


    assert not failures, chr(10).join(failures)
    print()
    print("=" * 70)
    print("ALL MQTT TENANT-IDENTITY CHECKS PASSED")


if __name__ == "__main__":
    test_mqtt_tenant_identity()
