"""The service queue obeys the factory's consent (ADR-0017).

THE DEFECT THIS PINS
--------------------
`/oem/fleet` withheld `operating_hours` correctly. `/oem/service` printed the
same number, in words, on the same screen — "4120.0 h run, never serviced,
4000 h interval, -120.0 h to the next service" — for a manufacturer whose
customer had switched that grant OFF. With every grant withdrawn it printed it
still. `SHARE_SERVICE_STATUS` was offered to factories as a checkbox and
enforced nowhere in the codebase.

It was not an oversight. It was two deliberate decisions contradicting each
other about the SAME column: oem_routes.service_queue argued the hour meter is
the OEM's own record and needs no grant, while oem_sharing.fleet_row gated that
identical column behind SHARE_OPERATING_HOURS. Only one could stand, and the UI
decided which: the factory is shown a checkbox labelled "Operating and loaded
hours" under the promise "Nothing is shared until you share it". A consent
control that does not control is worse than none.

WHY THESE ASSERTIONS SCAN THE WHOLE BODY
----------------------------------------
The number did not leak through a field called `operating_hours`. It leaked
inside a free-text `evidence` string that a per-field assertion would sail past
— which is exactly why the existing suites, which check `/oem/fleet` and
`/oem/machines/{id}` after revocation, all passed. So these checks serialise the
ENTIRE response and search it for the digits. A field added later that embeds
the hours in prose fails this test without anybody remembering to update it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_service_consent.py
"""
import asyncio
import json
import os
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

# Deliberately distinctive so a substring search cannot collide with an id, a
# count, a port or a timestamp fragment.
HOURS = 7777.0            # what the machine reported
LAST_SERVICE = 1111.0     # recorded BY THE OEM — its own record
INTERVAL = 4000           # on the OEM's OWN model — its own record
SINCE = 6666.0            # 7777 - 1111, disclosed only with the hours grant
REMAINING = -2666.0       # 4000 - 6666, ditto
SEEN_AT = datetime(2026, 2, 3, 4, 5, 6)
SEEN_ISO = SEEN_AT.isoformat()

# Every rendering of the hour meter, including the ones arithmetic derives from
# it. `-2666.0` covers `2666` by substring, and `.0`/`.1` roundings are covered
# because we search for the integer part.
HOUR_DIGITS = ("7777", "6666", "2666")

# A second customer of the same manufacturer, which shares nothing, ever.
OTHER_HOURS = 8888.0
OTHER_DIGITS = ("8888", "4888")   # 8888 run; 4000 - 8888 = -4888 remaining


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def token(sub, oem="OEM_ALPHA", role="OEM_ADMIN"):
    return pyjwt.encode({"sub": sub, "role": role, "principal": "oem", "oem": oem,
                         "exp": datetime.utcnow() + timedelta(hours=1)},
                        auth.SECRET_KEY, algorithm=ALGO)


async def call(path, tok, method="GET", body=None):
    raw = [(b"host", b"testserver"), (b"authorization", f"Bearer {tok}".encode())]
    payload = b"" if body is None else json.dumps(body).encode()
    if body is not None:
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": raw, "client": ("127.0.0.1", 5000),
             "server": ("testserver", 80)}
    chunks, status = [], {}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await main.app(scope, receive, send)
    body = b"".join(chunks)
    try:
        return status.get("code"), json.loads(body or b"{}")
    except Exception:
        return status.get("code"), {"_raw": body[:400].decode("utf-8", "replace")}


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
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN"))
    model = models.MachineModel(oem_code="OEM_ALPHA", family="Compressor",
                                model_code="X200", name="Alpha X200",
                                service_interval_hours=INTERVAL,
                                telemetry_profile=json.dumps(
                                    [{"name": "operating_hours", "source": "HrsRun",
                                      "datatype": "number"}]))
    db.add(model)
    machine = models.Machine(tenant_code="FACTORY_A", site="Plant 1",
                             name="COMP-001", status="Running", utilization=71)
    db.add(machine)
    db.flush()
    inst = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-A1", model_id=model.id,
        factory_tenant_code="FACTORY_A", site="Plant 1", machine_id=machine.id,
        status="Active", operating_hours=HOURS, last_service_hours=LAST_SERVICE,
        last_seen_at=SEEN_AT,
        warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1))
    db.add(inst)
    # A SECOND customer of the same manufacturer, which never shares anything.
    # The queue reads grants once per customer and caches them; a cache keyed
    # wrongly — or shared across the loop — hands this factory's hour meter over
    # under the FIRST factory's consent, and a single-customer fixture cannot
    # see that at all.
    other = models.MachineInstallation(
        oem_code="OEM_ALPHA", serial_number="SN-QUIET", model_id=model.id,
        factory_tenant_code="FACTORY_QUIET", site="Plant 9", status="Active",
        operating_hours=OTHER_HOURS, last_service_hours=0.0,
        last_seen_at=SEEN_AT)
    db.add(other)
    db.commit()
    inst_id = inst.id
    tenancy.reset_current_tenant(tok)
    db.close()
    return inst_id


def set_grants(grants):
    """Rewrite the policy, the way the factory's Save sharing button does."""
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    row = (db.query(models.OemDataSharingPolicy)
             .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
                     models.OemDataSharingPolicy.tenant_code == "FACTORY_A").first())
    if row is None:
        row = models.OemDataSharingPolicy(oem_code="OEM_ALPHA",
                                          tenant_code="FACTORY_A", grants="")
        db.add(row)
    row.grants = ",".join(grants)
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()


def leaked(body, needles):
    """Which forbidden strings appear ANYWHERE in the serialised response."""
    blob = json.dumps(body)
    return [n for n in needles if n in blob]


async def run_all():
    inst_id = seed()
    tok = token("alpha-admin")
    queue = "/oem/service"
    detail = f"/oem/machines/{inst_id}/service"

    print("=" * 74)
    print("1. CONTROL — WITH CONSENT, THE MANUFACTURER SEES EVERYTHING")
    print("=" * 74)
    # Without this the rest of the suite could pass on a route that returns 500.
    set_grants(["SHARE_SERVICE_STATUS", "SHARE_OPERATING_HOURS",
                "SHARE_MACHINE_HEALTH"])
    code, q = await call(queue, tok)
    check("the queue answers 200", code == 200, str(code))
    check("...and the hours ARE disclosed when the factory shared them",
          leaked(q, HOUR_DIGITS), json.dumps(q)[:200])
    check("...and the machine is called overdue",
          any(r["kind"] == "service_due" and r["severity"] == "high"
              for r in q.get("recommendations", [])), json.dumps(q)[:200])
    code, d = await call(detail, tok)
    check("the per-machine view answers 200", code == 200, str(code))
    check("...and discloses the hours too", leaked(d, HOUR_DIGITS), str(code))
    check("...and the commissioning timestamp", SEEN_ISO in json.dumps(d),
          json.dumps(d.get("commissioning"))[:200])

    print()
    print("=" * 74)
    print("2. THE HOURS GRANT IS WITHDRAWN — THE DEFECT")
    print("=" * 74)
    set_grants(["SHARE_SERVICE_STATUS", "SHARE_MACHINE_HEALTH"])
    code, q = await call(queue, tok)
    check("the queue still answers 200", code == 200, str(code))
    check("THE HOUR METER IS GONE FROM THE QUEUE, prose included",
          not leaked(q, HOUR_DIGITS), str(leaked(q, HOUR_DIGITS)) + " " + json.dumps(q)[:300])
    code, d = await call(detail, tok)
    check("...and gone from the per-machine view",
          not leaked(d, HOUR_DIGITS),
          str(leaked(d, HOUR_DIGITS)) + " " + json.dumps(d)[:300])

    # The factory granted "Service due / overdue status" and withheld the
    # figures. Both halves of that sentence have to hold, or the fix has simply
    # traded a leak for a blank screen.
    check("the manufacturer is STILL told the machine is overdue",
          any(r["kind"] == "service_due" for r in q.get("recommendations", [])),
          json.dumps(q)[:300])
    check("...and the interval, which is on the OEM'S OWN model, is still there",
          "4000" in json.dumps(d), json.dumps(d.get("service"))[:200])

    print()
    print("=" * 74)
    print("3. THE SERVICE-STATUS GRANT IS WITHDRAWN")
    print("=" * 74)
    set_grants(["SHARE_OPERATING_HOURS", "SHARE_MACHINE_HEALTH"])
    code, q = await call(queue, tok)
    check("no service recommendation survives without SHARE_SERVICE_STATUS",
          not any(r["kind"] in ("service_due", "service_projection")
                  for r in q.get("recommendations", [])), json.dumps(q)[:300])
    code, d = await call(detail, tok)
    check("...and the per-machine service verdict says NOT SHARED",
          d.get("service", {}).get("state") == "not_shared",
          json.dumps(d.get("service"))[:200])
    # "not shared" must be distinguishable from "this machine has no interval
    # configured" and from "it has never reported" — the same distinction
    # `shareable()` exists for on the fleet screen.
    check("...and says so in words, rather than looking like missing data",
          "not shared" in (d.get("service", {}).get("reason") or "").lower(),
          json.dumps(d.get("service"))[:200])

    print()
    print("=" * 74)
    print("4. CONNECTIVITY IS WITHDRAWN")
    print("=" * 74)
    set_grants(["SHARE_SERVICE_STATUS", "SHARE_OPERATING_HOURS"])
    code, d = await call(detail, tok)
    check("the last-seen timestamp is gone from commissioning",
          SEEN_ISO not in json.dumps(d), json.dumps(d.get("commissioning"))[:300])
    check("...and the check reports UNKNOWN rather than failed",
          any(c["key"] == "has_reported" and c["passed"] is None
              for c in d.get("commissioning", {}).get("checks", [])),
          json.dumps(d.get("commissioning", {}).get("checks"))[:300])
    check("...and readiness is unknown too, not a confident yes",
          d.get("commissioning", {}).get("ready") is None,
          str(d.get("commissioning", {}).get("ready")))

    print()
    print("=" * 74)
    print("5. EVERYTHING IS WITHDRAWN — the 'Withdraw everything' button")
    print("=" * 74)
    set_grants([])
    code, q = await call(queue, tok)
    check("the queue answers 200 rather than erroring", code == 200, str(code))
    check("NOTHING of the hour meter remains", not leaked(q, HOUR_DIGITS),
          str(leaked(q, HOUR_DIGITS)))
    check("...no service verdict either",
          not any(r["kind"] in ("service_due", "service_projection")
                  for r in q.get("recommendations", [])), json.dumps(q)[:300])
    check("...and no connectivity", SEEN_ISO not in json.dumps(q),
          json.dumps(q)[:300])

    code, d = await call(detail, tok)
    check("the per-machine view holds none of it either",
          not leaked(d, HOUR_DIGITS) and SEEN_ISO not in json.dumps(d),
          json.dumps(d)[:400])

    print()
    print("=" * 74)
    print("6. WHAT THE OEM OWNS IS NEVER TAKEN AWAY")
    print("=" * 74)
    # The counter-test. A fix that hides everything is not a fix — it would make
    # the portal useless and, worse, would hide the manufacturer's OWN records
    # behind a customer's checkbox.
    check("its own serial survives with no grants at all",
          d.get("serial_number") == "SN-A1", json.dumps(d)[:200])
    check("...and the warranty IT recorded", d.get("warranty", {}).get("state"),
          json.dumps(d.get("warranty"))[:200])
    check("...and the telemetry profile from its OWN model",
          d.get("telemetry_signals") == ["operating_hours"],
          str(d.get("telemetry_signals")))
    check("...and the lifecycle status it set itself",
          d.get("lifecycle_status") == "Active", str(d.get("lifecycle_status")))
    check("...and a warranty recommendation, which needs no grant",
          any(r["kind"].startswith("warranty") for r in q.get("recommendations", []))
          or d.get("warranty", {}).get("state") == "active",
          json.dumps(q.get("recommendations"))[:300])

    print()
    print("=" * 74)
    print("7. ONE CUSTOMER'S CONSENT IS NOT ANOTHER'S")
    print("=" * 74)
    # The queue caches grants per customer. FACTORY_A now shares EVERYTHING, so
    # a cache that is keyed wrongly, or reused across the loop, hands
    # FACTORY_QUIET's hour meter over under FACTORY_A's consent — and the
    # manufacturer is the same manufacturer, so no tenant filter would stop it.
    set_grants(["SHARE_SERVICE_STATUS", "SHARE_OPERATING_HOURS",
                "SHARE_MACHINE_HEALTH"])
    code, q = await call(queue, tok)
    check("CONTROL: the sharing customer's machine IS in the queue",
          any(r["machine"] == "SN-A1" for r in q.get("recommendations", [])),
          json.dumps(q)[:200])
    check("THE OTHER CUSTOMER'S HOURS DO NOT RIDE IN ON THIS ONE'S CONSENT",
          not leaked(q, OTHER_DIGITS), str(leaked(q, OTHER_DIGITS)))
    check("...and it gets no service verdict either",
          not any(r["machine"] == "SN-QUIET"
                  and r["kind"] in ("service_due", "service_projection")
                  for r in q.get("recommendations", [])), json.dumps(q)[:300])
    check("...while its warranty item, which needs no grant, still appears",
          any(r["machine"] == "SN-QUIET" and r["kind"].startswith("warranty")
              for r in q.get("recommendations", [])), json.dumps(q)[:400])

    print()
    print("=" * 74)
    print("8. THE WRITE THAT READ")
    print("=" * 74)
    # Gating the READ routes would have left this wide open. POST a service
    # record with NO hours: the endpoint falls back to what the machine last
    # reported, and used to hand that figure straight back in the response. An
    # OEM whose customer shares nothing could call this with an empty body and
    # read the hour meter out of `last_service_hours`.
    set_grants([])
    code, w = await call(detail, tok, method="POST", body={})
    check("recording a service still works with no grants", code == 200, str(w)[:200])
    check("THE EMPTY-BODY WRITE DOES NOT HAND BACK THE HOUR METER",
          not leaked(w, HOUR_DIGITS), str(leaked(w, HOUR_DIGITS)) + " " + json.dumps(w)[:300])
    check("...and it is reported as not-shared rather than as zero",
          w.get("last_service_hours") is None, str(w.get("last_service_hours")))
    check("...while the service itself IS on the record",
          w.get("last_service_at"), str(w.get("last_service_at")))
    # And it really was stored, not dropped — otherwise the fix would have
    # silently broken service tracking for every unshared customer.
    db = SessionLocal()
    t = tenancy.set_current_tenant(None)
    stored = db.query(models.MachineInstallation).filter_by(id=inst_id).first()
    check("...at the TRUE meter reading, in the database",
          stored.last_service_hours == HOURS, str(stored.last_service_hours))
    stored.last_service_hours = LAST_SERVICE     # restore for the checks below
    db.commit()
    tenancy.reset_current_tenant(t)
    db.close()

    # Supplying a figure at all is a separate question, and it now depends on
    # the same grant — see section 9, which is where the reason lives. The
    # echo-back of a caller's OWN number is checked there, under the grant that
    # permits sending one.

    print()
    print("=" * 74)
    print("9. THE WRITE THAT SEARCHED")
    print("=" * 74)
    # The sharpest version, and the one gating the READ side does nothing about.
    # The service verdict is `operating_hours - last_service_hours` against the
    # interval — and `last_service_hours` is not the factory's column, the
    # CALLER supplies it. So an OEM holding only the verdict could POST a probe,
    # read which side of the threshold it landed on, and bisect. 24 requests
    # recovered a meter of 6421.7 to 6421.70 before this refusal existed.
    set_grants(["SHARE_SERVICE_STATUS"])
    code, probe = await call(detail, tok, method="POST",
                             body={"service_hours": 3000.0})
    check("a probe value is REFUSED when hours are not shared", code == 403,
          f"{code} {str(probe)[:200]}")
    check("...and the refusal says what to send instead",
          "without `service_hours`" in str(probe.get("detail", "")),
          str(probe.get("detail"))[:200])

    # The refusal must not depend on the VALUE, or it is an oracle in itself:
    # "400 for a value above the meter, 200 below" would be the same bisection.
    for probe_value in (1.0, 999999.0):
        code, _ = await call(detail, tok, method="POST",
                             body={"service_hours": probe_value})
        check(f"...identically for {probe_value:g}, so the refusal is not itself "
              f"an oracle", code == 403, str(code))

    # The bisection, run for real against the fixed route. It must learn nothing.
    lo, hi, answers = 0.0, 100000.0, set()
    for _ in range(24):
        mid = (lo + hi) / 2
        c, r = await call(detail, tok, method="POST", body={"service_hours": mid})
        answers.add((c, json.dumps(r.get("service", {}), sort_keys=True)))
        hi = mid
    check("A 24-STEP BISECTION LEARNS EXACTLY ONE ANSWER",
          len(answers) == 1, f"{len(answers)} distinct answers: {list(answers)[:2]}")

    # And the machine's true service record was not corrupted by the probing.
    db = SessionLocal()
    t = tenancy.set_current_tenant(None)
    still = db.query(models.MachineInstallation).filter_by(id=inst_id).first()
    check("...and the probes wrote nothing to the service clock",
          still.last_service_hours == LAST_SERVICE, str(still.last_service_hours))
    tenancy.reset_current_tenant(t)
    db.close()

    # CONTROL: a customer that DOES share hours has taken away no capability.
    set_grants(["SHARE_SERVICE_STATUS", "SHARE_OPERATING_HOURS"])
    code, ok = await call(detail, tok, method="POST",
                          body={"service_hours": 5000.0})
    check("CONTROL: with hours shared, an explicit figure is accepted",
          code == 200 and ok.get("last_service_hours") == 5000.0,
          f"{code} {str(ok)[:200]}")

    print()
    print("=" * 74)
    print("10. AN OMITTED FIELD IS A DISCLOSURE TOO")
    print("=" * 74)
    # `service_state` omits `interval_hours` on the branch where the machine has
    # never reported hours, and includes it on every other. Copying it through
    # therefore carried one bit of the withheld meter: whether the customer's
    # machine has ever reported at all. The fleet row is byte-identical either
    # way; this route has to be too.
    set_grants(["SHARE_SERVICE_STATUS"])
    code, with_hours = await call(detail, tok)
    db = SessionLocal()
    t = tenancy.set_current_tenant(None)
    db.query(models.MachineInstallation).filter_by(id=inst_id).update(
        {"operating_hours": None})
    db.commit()
    tenancy.reset_current_tenant(t)
    db.close()
    code, without = await call(detail, tok)
    check("the SERVICE INTERVAL is present whether or not the meter has a value",
          with_hours["service"]["interval_hours"] == INTERVAL
          and without["service"]["interval_hours"] == INTERVAL,
          f'{with_hours["service"].get("interval_hours")} vs '
          f'{without["service"].get("interval_hours")}')
    check("...and the two responses' service blocks differ in NO key",
          set(with_hours["service"]) == set(without["service"]),
          str(set(with_hours["service"]) ^ set(without["service"])))

    db = SessionLocal()
    t = tenancy.set_current_tenant(None)
    db.query(models.MachineInstallation).filter_by(id=inst_id).update(
        {"operating_hours": HOURS, "last_service_hours": LAST_SERVICE})
    db.commit()
    tenancy.reset_current_tenant(t)
    db.close()

    print()
    print("=" * 74)
    print("11. NO POLICY ROW AT ALL IS THE SAME AS AN EMPTY ONE")
    print("=" * 74)
    # Default deny is the ABSENCE of a row (oem_sharing). A fix that keys off
    # `row.grants == ""` and forgets `row is None` would pass everything above
    # and leak for every customer who has never opened the screen.
    db = SessionLocal()
    t = tenancy.set_current_tenant(None)
    db.query(models.OemDataSharingPolicy).delete()
    db.commit()
    tenancy.reset_current_tenant(t)
    db.close()
    code, q = await call(queue, tok)
    check("a customer who never opened the sharing screen shares nothing",
          not leaked(q, HOUR_DIGITS)
          and not any(r["kind"] == "service_due" for r in q.get("recommendations", [])),
          json.dumps(q)[:300])

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_all()))
