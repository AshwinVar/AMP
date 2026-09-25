"""A gateway claims the machine you already created; it never registers a second one.

THE SEAM THIS CLOSES. `Machine` is UNIQUE(tenant_code, site, name) and `site`
was written ONLY by the MQTT path, from the topic. So every machine added by
hand or by CSV carried an empty site, and the first gateway packet published
under a real site did not match it — it inserted a SECOND machine of the same
name. A commissioning engineer would watch their machine list double on the
first packet, which is the worst possible moment.

  1. ADOPTION: a hand-made siteless machine is claimed by its gateway, keeping
     its id, its history and everything pointing at it
  2. NO DUPLICATE: the fleet does not grow
  3. THE ORDINARY CASE still creates: a machine AMP has genuinely never seen
  4. AMBIGUITY IS REFUSED, never guessed — and the packet is dropped rather
     than turned into a duplicate
  5. A REFUSAL IS RECORDED where a human will see it, and only once
  6. TENANT ISOLATION: adoption never reaches across a workspace
  7. A site is settable by a human, and only as a topic segment

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_identity_adoption.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import mqtt_service  # noqa: E402
import schemas  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402
from mqtt_identity import Route as MachineRoute  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def fresh():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)


def add_machine(db, tenant, site, name, status="Idle"):
    tok = tenancy.set_current_tenant(None)
    try:
        m = models.Machine(tenant_code=tenant, site=site, name=name, status=status,
                           utilization=0, downtime="0 min")
        db.add(m)
        db.commit()
        db.refresh(m)
        return m
    finally:
        tenancy.reset_current_tenant(tok)


def count(db, tenant, name=None):
    tok = tenancy.set_current_tenant(None)
    try:
        q = db.query(models.Machine).filter(models.Machine.tenant_code == tenant)
        if name:
            q = q.filter(models.Machine.name == name)
        return q.count()
    finally:
        tenancy.reset_current_tenant(tok)


route = MachineRoute(tenant="ACME", site="plant-1")

# ── 1-2. adoption, and no duplicate ────────────────────────────────
section("1. THE GATEWAY CLAIMS THE MACHINE YOU ALREADY CREATED")
Session = fresh()
db = Session()
typed_in = add_machine(db, "ACME", "", "CNC-01", status="Running")
original_id = typed_in.id

resolved = mqtt_service.get_or_create_machine(db, route, "CNC-01")
check("the gateway resolves to the machine that already existed",
      resolved.id == original_id, f"{resolved.id} vs {original_id}")
check("...and that machine now has the gateway's site", resolved.site == "plant-1", resolved.site)
check("NO second machine was registered", count(db, "ACME", "CNC-01") == 1,
      str(count(db, "ACME", "CNC-01")))
check("...and the status it was created with survived adoption",
      resolved.status == "Running", resolved.status)

# The id is what everything else points at: production records, downtime logs,
# maintenance tasks, the twin. Adoption that created a new row and "migrated"
# would orphan all of it.
check("adoption keeps the id, so nothing pointing at the machine is orphaned",
      resolved.id == original_id)

# A second packet is now an ordinary exact match.
again = mqtt_service.get_or_create_machine(db, route, "CNC-01")
check("the next packet matches exactly, with no further adoption",
      again.id == original_id and count(db, "ACME", "CNC-01") == 1)

# ── 3. the ordinary case still creates ─────────────────────────────
section("2. A MACHINE AMP HAS NEVER SEEN IS STILL CREATED")
made = mqtt_service.get_or_create_machine(db, route, "BRAND-NEW-01")
check("a genuinely new machine is registered", made.id is not None and made.site == "plant-1",
      f"{made.id}/{made.site}")
check("...at the gateway's site", made.site == "plant-1", made.site)

# ── 4. ambiguity is refused, not guessed ───────────────────────────
section("3. AMBIGUITY IS REFUSED, AND THE PACKET IS NOT TURNED INTO A DUPLICATE")
Session2 = fresh()
db2 = Session2()
add_machine(db2, "ACME", "plant-1", "PRESS-01")     # already somewhere real
add_machine(db2, "ACME", "", "PRESS-01")            # and one typed in with no site
before = count(db2, "ACME", "PRESS-01")

raised = None
try:
    mqtt_service.get_or_create_machine(db2, MachineRoute(tenant="ACME", site="plant-2"), "PRESS-01")
except mqtt_service.AmbiguousMachineIdentity as e:
    raised = str(e)
check("AMP refuses rather than choosing a machine", raised is not None, "it resolved one anyway")
check("...and says why, naming the situation", raised and "will not guess" in raised, str(raised))
check("NO machine was created to dodge the question", count(db2, "ACME", "PRESS-01") == before,
      f"{before} -> {count(db2, 'ACME', 'PRESS-01')}")

# The unambiguous half of the same fixture still works: plant-1 is an exact match.
ok = mqtt_service.get_or_create_machine(db2, MachineRoute(tenant="ACME", site="plant-1"), "PRESS-01")
check("...while the packet that IS unambiguous still resolves", ok.site == "plant-1", ok.site)

# ── 5. the refusal is recorded, once ───────────────────────────────
section("4. A REFUSAL IS RECORDED WHERE A HUMAN WILL SEE IT, AND ONLY ONCE")
tok = tenancy.set_current_tenant(None)
notes = db2.query(models.Notification).filter(
    models.Notification.tenant_code == "ACME",
    models.Notification.notification_type == "machine_identity").count()
tenancy.reset_current_tenant(tok)
check("nothing is recorded by the resolver itself (the caller decides)", notes == 0, str(notes))

mqtt_service._record_identity_conflict(db2, MachineRoute(tenant="ACME", site="plant-2"),
                                       "PRESS-01", "two machines share the name")
mqtt_service._record_identity_conflict(db2, MachineRoute(tenant="ACME", site="plant-2"),
                                       "PRESS-01", "two machines share the name")
tok = tenancy.set_current_tenant(None)
rows = db2.query(models.Notification).filter(
    models.Notification.tenant_code == "ACME",
    models.Notification.notification_type == "machine_identity").all()
tenancy.reset_current_tenant(tok)
check("the conflict is recorded", len(rows) >= 1, str(len(rows)))
# A gateway publishes every second. An alert that floods is an alert nobody reads.
check("...exactly once, however many packets arrive", len(rows) == 1, str(len(rows)))
check("...and it tells the engineer what to do about it",
      "Set the site on the machine you meant" in rows[0].message, rows[0].message)
check("...without claiming the reading was kept",
      "dropped the reading" in rows[0].message, rows[0].message)

# ── 6. tenant isolation ────────────────────────────────────────────
section("5. ADOPTION NEVER REACHES ACROSS A WORKSPACE")
Session3 = fresh()
db3 = Session3()
theirs = add_machine(db3, "OTHER", "", "SHARED-01")
mine = mqtt_service.get_or_create_machine(db3, MachineRoute(tenant="ACME", site="plant-1"), "SHARED-01")
check("a siteless machine in ANOTHER workspace is not adopted", mine.id != theirs.id,
      f"{mine.id} vs {theirs.id}")
check("...it stays siteless and untouched",
      db3.query(models.Machine).filter(models.Machine.id == theirs.id).one().site == "",
      "the other workspace's machine was modified")
check("...and the packet's own workspace got its machine", mine.tenant_code == "ACME", mine.tenant_code)

# ── 7. a human can set a site, and only a legal one ────────────────
section("6. A HUMAN CAN SET THE SITE, AND ONLY AS A TOPIC SEGMENT")
ok_site = schemas.MachineCreate(name="X", status="Running", utilization=0, downtime="0 min",
                                site="plant-1")
check("a plain site is accepted", ok_site.site == "plant-1", ok_site.site)
check("the field defaults to empty, so every existing caller still works",
      schemas.MachineCreate(name="X", status="Running", utilization=0, downtime="0 min").site == "")
for bad in ("plant/1", "+", "#", "plant 1", "/"):
    refused = False
    try:
        schemas.MachineCreate(name="X", status="Running", utilization=0, downtime="0 min", site=bad)
    except Exception:
        refused = True
    # A slash would address a different topic level; + and # are MQTT wildcards,
    # and a site of "+" would subscribe a gateway to every site there is.
    check(f"a site of {bad!r} is refused", refused, bad)

# ── 7. the WHOLE HANDLER, not just the resolver ────────────────────
#
# Sections 1-6 call get_or_create_machine directly. The mutation harness showed
# what that misses: deleting the conflict record from the caller, and deleting
# the `return` that stops a refused packet falling through into the duplicate
# insert, both survived every check above. The resolver being right does not
# make the handler right, and the handler is what a gateway actually talks to.
section("7. A CONFLICTING PACKET, DRIVEN THROUGH THE REAL MESSAGE HANDLER")
import json  # noqa: E402

from database import SessionLocal, engine  # noqa: E402


class Msg:
    """The two fields paho's on_message callback reads off a message."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
real = SessionLocal()
tok = tenancy.set_current_tenant(None)
real.add(models.TenantConfig(tenant_code="ACME"))
real.add(models.Machine(tenant_code="ACME", site="plant-1", name="MILL-01", status="Idle",
                        utilization=0, downtime="0 min"))
real.add(models.Machine(tenant_code="ACME", site="", name="MILL-01", status="Idle",
                        utilization=0, downtime="0 min"))
real.commit()
tenancy.reset_current_tenant(tok)

prefix = mqtt_service.TOPIC_PREFIX
before_machines = count(real, "ACME", "MILL-01")
mqtt_service.on_message(None, None, Msg(
    f"{prefix}/ACME/plant-2/machines",
    {"machine": "MILL-01", "status": "Running", "utilization": 80, "downtime": "0 min"}))
real.expire_all()

check("the handler created NO duplicate for an ambiguous packet",
      count(real, "ACME", "MILL-01") == before_machines,
      f"{before_machines} -> {count(real, 'ACME', 'MILL-01')}")

tok = tenancy.set_current_tenant(None)
raised_notes = real.query(models.Notification).filter(
    models.Notification.tenant_code == "ACME",
    models.Notification.notification_type == "machine_identity").all()
tenancy.reset_current_tenant(tok)
check("...and recorded the conflict for a human", len(raised_notes) == 1, str(len(raised_notes)))
check("...naming the machine and the site the packet claimed",
      raised_notes and "MILL-01" in raised_notes[0].title and "plant-2" in raised_notes[0].title,
      raised_notes[0].title if raised_notes else "none")

# The UNAMBIGUOUS packet still flows all the way through the handler: the
# siteless machine is adopted and its status actually changes.
tok = tenancy.set_current_tenant(None)
real.query(models.Machine).filter(models.Machine.tenant_code == "ACME",
                                  models.Machine.site == "plant-1",
                                  models.Machine.name == "MILL-01").delete()
real.commit()
tenancy.reset_current_tenant(tok)
mqtt_service.on_message(None, None, Msg(
    f"{prefix}/ACME/plant-2/machines",
    {"machine": "MILL-01", "status": "Running", "utilization": 80, "downtime": "0 min"}))
real.expire_all()
tok = tenancy.set_current_tenant(None)
survivors = real.query(models.Machine).filter(models.Machine.tenant_code == "ACME",
                                              models.Machine.name == "MILL-01").all()
tenancy.reset_current_tenant(tok)
check("with the ambiguity removed, the packet adopts rather than duplicating",
      len(survivors) == 1, str([(m.site, m.status) for m in survivors]))
check("...the adopted machine carries the gateway's site",
      survivors and survivors[0].site == "plant-2", str(survivors[0].site) if survivors else "none")
check("...and the reading actually landed on it",
      survivors and survivors[0].status == "Running", str(survivors[0].status) if survivors else "none")
real.close()

# ── 8. the guard for a database without the constraint ─────────────
#
# UNIQUE(tenant_code, site, name) means only ONE row can have an empty site for
# a given name, so `len(siteless) > 1` is unreachable on a correct schema — and
# the harness rightly refused to let that stand as untested. It is not dead
# code: a pre-0002 deployment or a restored dump can lack the constraint, and
# resolving by "whichever row came first" is the exact coin-flip this module's
# docstring records for name-only lookups. Driven here through a fake session,
# because the guard's whole point is a database this suite cannot create.
section("8. WITHOUT THE UNIQUE CONSTRAINT, TWO SITELESS ROWS ARE REFUSED")


class _TwoRows:
    """A session that returns two siteless machines of one name."""

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *a, **k):
            return self

        def all(self):
            return self._rows

        def count(self):
            return len(self._rows)

    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return self._Q(self._rows)


two = _TwoRows([object(), object()])
refused = None
try:
    mqtt_service.adopt_candidate(two, MachineRoute(tenant="ACME", site="plant-1"), "GHOST-01")
except mqtt_service.AmbiguousMachineIdentity as e:
    refused = str(e)
check("two siteless rows of one name are refused, not resolved by order",
      refused is not None, "one of them was adopted")
check("...and the refusal says how many it found", refused and "2 machines" in refused, str(refused))

# ── 9. a refusal is a clean exit, not a swallowed crash ────────────
#
# THIS SECTION EXISTS BECAUSE I MISLABELLED A MUTATION. Deleting the `return`
# after the conflict is recorded does not create a duplicate, as I first claimed
# -- `machine` is assigned only inside the try that raised, so the next line
# raises UnboundLocalError instead, and on_message's outer `except Exception`
# swallows it. Same visible outcome, which is why every check above passed.
#
# It still matters. A swallowed UnboundLocalError in the hot path is how an
# ingest listener quietly stops working: the generic handler eats it, the log
# says "MQTT service error" with no hint that identity was the cause, and a
# commissioning engineer reading that log cannot tell which side is broken --
# the one thing the sprint's diagnostics rule says they must be able to do.
#
# So the difference IS observable, in the log, and that is what this pins.
section("9. A REFUSED PACKET LEAVES CLEANLY, WITHOUT TRIPPING THE CATCH-ALL")
import logging  # noqa: E402


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())   # already interpolated; do not re-apply args


# Section 7 ends by REMOVING the ambiguity, so this needs its own: one LATHE-09
# with a site and one without, which is exactly the shape a customer lands in
# after typing a machine in and then pointing a gateway at a second site.
fresh = SessionLocal()
tok = tenancy.set_current_tenant(None)
fresh.add(models.Machine(tenant_code="ACME", site="plant-1", name="LATHE-09", status="Idle",
                         utilization=0, downtime="0 min"))
fresh.add(models.Machine(tenant_code="ACME", site="", name="LATHE-09", status="Idle",
                         utilization=0, downtime="0 min"))
fresh.commit()
tenancy.reset_current_tenant(tok)
before_lathes = count(fresh, "ACME", "LATHE-09")

cap = Captured()
mqtt_log = logging.getLogger("mqtt_service")
mqtt_log.addHandler(cap)
previous_level = mqtt_log.level
mqtt_log.setLevel(logging.DEBUG)
try:
    mqtt_service.on_message(None, None, Msg(
        f"{prefix}/ACME/plant-3/machines",
        {"machine": "LATHE-09", "status": "Running", "utilization": 55, "downtime": "0 min"}))
finally:
    mqtt_log.removeHandler(cap)
    mqtt_log.setLevel(previous_level)
fresh.expire_all()

said = chr(10).join(cap.lines)
check("the refusal is logged as an identity conflict, naming tenant, site and machine",
      "identity conflict" in said and "ACME" in said and "LATHE-09" in said, said[:200])
check("...and the generic catch-all did NOT fire, so the log names the real cause",
      "FastAPI MQTT service error" not in said, said[:300])
check("...no UnboundLocalError was swallowed on the way out",
      "UnboundLocal" not in said, said[:300])
check("...the reading was NOT applied to a machine AMP could not identify",
      "accepted" not in said, said[:300])
check("...and still no duplicate", count(fresh, "ACME", "LATHE-09") == before_lathes,
      str(count(fresh, "ACME", "LATHE-09")))
fresh.close()

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
