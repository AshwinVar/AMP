"""How this workspace gets data into AMP, without claiming anything AMP does not do.

`mqtt_service` is a REAL ingest path — it writes status, utilization, downtime,
machine events and production records, and auto-creates machines from
`{prefix}/{tenant}/{site}/machines`. It works, and nothing in the product ever
told a customer the topic to publish to. A working ingest nobody can discover
is, from the customer's side, the same as no ingest.

What this pins is mostly what the screen must NOT do:

  1. it never returns a credential, and does not pretend they are self-service
  2. it never claims AMP connects to a machine — an edge agent publishes
  3. it says "nothing has arrived" as exactly that, not as failure or success
  4. THE DUPLICATE TRAP: a machine is (tenant, site, name), `site` is written
     only by the gateway path, so hand-added machines have an empty one and a
     gateway will REGISTER A SECOND SET rather than update them. The warning
     must give advice a customer can follow — the first version told them to
     publish under a site none of their machines had
  5. tenant isolation

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ingest_guide.py
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import mqtt_service  # noqa: E402
import tenancy  # noqa: E402
from ai.ingest_guide import _duplicate_warning, build_ingest_guide  # noqa: E402
from database import Base  # noqa: E402

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


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)


def seed(Session):
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        db.add(models.Machine(tenant_code="A", site="plant-1", name="CNC-01", status="Running",
                              utilization=0, downtime="0 min"))
        db.add(models.Machine(tenant_code="A", site="", name="TYPED-IN", status="Running",
                              utilization=0, downtime="0 min"))
        db.add(models.Machine(tenant_code="B", site="", name="OTHERS-01", status="Running",
                              utilization=0, downtime="0 min"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return Session


def guide(Session, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return build_ingest_guide(db, tenant)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


Session = seed(session())
configured = mqtt_service.mqtt_is_configured()

# ── 1. it never hands out a secret ─────────────────────────────────
section("1. NO CREDENTIAL, EVER")
a = guide(Session, "A")
blob = json.dumps(a).lower()
for word in ("password", "secret", "token", "mqtt_username", "mqtt_password"):
    check(f"the payload carries no {word!r}", word not in blob, word)
check("it says who issues credentials rather than implying self-service",
      "operator" in a["gateway"]["credentials"].lower()
      and "never shows them" in a["gateway"]["credentials"], a["gateway"]["credentials"])

# ── 2. it claims no connection AMP does not make ───────────────────
section("2. AMP CONNECTS TO NOTHING, SAID OUT LOUD")
check("it says AMP opens no connection to the machines",
      "opens no connection" in a["gateway"]["amp_connects_to_nothing"],
      a["gateway"]["amp_connects_to_nothing"])
check("...and names what does the reading instead",
      "edge agent" in a["gateway"]["amp_connects_to_nothing"],
      a["gateway"]["amp_connects_to_nothing"])

# ── 3. nothing arrived is stated as that ───────────────────────────
section("3. NOTHING ARRIVED IS NOT A FAILURE AND NOT A SUCCESS")
check("state reflects whether a broker is configured at all",
      a["state"] in ("NO DATA", "NOT CONFIGURED"), a["state"])
if configured:
    check("with nothing received, the headline says so plainly",
          "nothing has arrived" in a["headline"].lower(), a["headline"])
    check("...and the count is a real 0, not an absence", a["gateway"]["readings_received"] == 0,
          str(a["gateway"]["readings_received"]))
else:
    check("with no broker, the topic is not invented", a["gateway"]["topic"] is None,
          str(a["gateway"]["topic"]))
    check("...and the manual paths are offered instead",
          "type production in" in a["headline"].lower(), a["headline"])

# ── 4. the duplicate trap ──────────────────────────────────────────
section("4. THE DUPLICATE TRAP, WITH ADVICE A CUSTOMER CAN FOLLOW")
# Some machines carry a site, so there IS a real answer: publish under it.
some = _duplicate_warning(True, 8, 3, ["plant-1"])
check("with a known site, it names the site to publish under", 'plant-1' in some, some)
check("...and warns they would be registered again", "REGISTER THOSE AGAIN" in some, some)

# NONE carry a site. The first version told the customer to "publish under the
# site these machines already use" — advice nobody can follow, and the common
# case, because Machine.site is reachable from no form and no route.
none_have = _duplicate_warning(True, 8, 8, [])
check("with NO site anywhere, it does not tell them to use one they do not have",
      "already use" not in none_have, none_have)
check("...it says plainly that no screen can set one yet",
      "no screen can set one yet" in none_have, none_have)
check("...and gives a step that exists: ask the operator",
      "operator" in none_have, none_have)

check("no warning when every machine has a site", _duplicate_warning(True, 8, 0, ["plant-1"]) is None)
check("no warning when there is no gateway path at all", _duplicate_warning(False, 8, 8, []) is None)
if configured:
    check("the live guide warns, because one machine was typed in",
          a["gateway"]["duplicate_warning"] is not None, str(a["gateway"]["duplicate_warning"]))
    check("...and the count is that machine only, not the whole fleet",
          "1 of your 2 machines" in a["gateway"]["duplicate_warning"],
          a["gateway"]["duplicate_warning"])

# ── 5. tenant isolation ────────────────────────────────────────────
section("5. TENANT ISOLATION")
b = guide(Session, "B")
check("A counts only its own machines", a["machines"] == 2, str(a["machines"]))
check("B counts only its own", b["machines"] == 1, str(b["machines"]))
check("A's sites do not appear in B's guide", "plant-1" not in json.dumps(b), json.dumps(b)[:200])
if configured:
    check("each workspace is told its OWN topic", f"/{'A'}/" in (a["gateway"]["topic"] or "")
          and f"/{'B'}/" in (b["gateway"]["topic"] or ""),
          f"{a['gateway']['topic']} / {b['gateway']['topic']}")

# ── 6. every path it offers is real ────────────────────────────────
section("6. EVERY PATH IT OFFERS IS A ROUTE THAT EXISTS")
import main  # noqa: E402

paths = {getattr(r, "path", "") for r in main.app.routes}
for m in a["manual"]:
    check(f"{m['route']} is a real route", m["route"] in paths, m["route"])

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
