"""A device's MQTT topic is not used for routing, and the API says so instead of storing it.

THE DEFECT
----------
`POST /industrial/devices` accepted a `topic`, the row kept it, and the
connection drawer printed it beside the device (`· flowmes/plant/press`) as if
it configured where AMP listens. Nothing read it. MQTT routes per tenant and
site — `{prefix}/{tenant}/{site}/machines` (ADR-0011, mqtt_identity) — because
the topic is the one part of a message a broker can enforce, and a free-text
per-device topic could only route by breaking that. Recorded as verified-open
in CHIEF-ENGINEER-STATE (P3, "accepted-but-inert input", the #593 shape).

THE RULE THIS FILE PINS
-----------------------
  1. THE SCHEMA     a non-empty topic is refused with the reason (how routing
                    really works); none or blank stays NULL.
  2. THE ROUTE      over HTTP the refusal is a 422 carrying that sentence; a
                    payload without a topic registers the device with NULL.
  3. THE READ MODEL the connection drawer's device rows carry no topic, so a
                    value stored before the refusal is not shown as
                    configuration; the drawer's source renders none.
  4. THE CLAIM      the justification is checked, not assumed: the MQTT service
                    and identity module never touch IndustrialDevice, and PATCH
                    cannot set a topic either.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_device_topic_is_not_routing.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import pydantic
from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import http_security
import industrial_iot_routes
import main
import models
import plan_gate
import schemas
import tenancy
from ai import connectivity
from database import Base

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)
failures = []
HERE = os.path.dirname(os.path.abspath(__file__))
PATCHED = (database, main, industrial_iot_routes, plan_gate)
_real = {}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def install():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    for mod in PATCHED:
        _real.setdefault(mod, mod.SessionLocal)
        mod.SessionLocal = SessionLocal
    plan_gate._licence_cache.clear()


def uninstall():
    for mod, real in _real.items():
        mod.SessionLocal = real
    _real.clear()
    plan_gate._licence_cache.clear()


def token(sub, role, tenant):
    claims = {"sub": sub, "role": role, "tenant": tenant, "exp": datetime.utcnow() + timedelta(hours=1)}
    return pyjwt.encode(claims, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


async def _call(method, path, tok, body=None):
    raw = [(b"host", b"testserver"), (b"authorization", f"Bearer {tok}".encode())]
    payload = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        raw += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": raw, "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    chunks, status, sent = [], {}, {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
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
        return status.get("code"), {"_raw": data[:300].decode("utf-8", "replace")}


def call(method, path, tok, body=None):
    http_security._window.reset()
    return asyncio.run(_call(method, path, tok, body))


def _create(**kw):
    kw.setdefault("device_code", "PRESS-PLC-1")
    kw.setdefault("device_name", "Press PLC")
    return schemas.IndustrialDeviceCreate(**kw)


# ----------------------------------------------------------------------------
def section_schema():
    print("=" * 74)
    print("1. THE SCHEMA: A TOPIC IS REFUSED WITH THE REASON; NONE OR BLANK STAYS NULL")
    print("=" * 74)
    try:
        _create(topic="flowmes/plant1/press")
        check("a non-empty topic is refused", False, "no ValidationError")
    except pydantic.ValidationError as exc:
        msg = str(exc)
        check("a non-empty topic is refused", True)
        check("...with the reason: routing is per tenant and site, per ADR-0011",
              "tenant" in msg and "site" in msg and "ADR-0011" in msg, msg[:300])
        check("...and it tells the caller what to do instead",
              "Leave `topic` empty" in msg and "publish to the tenant's topic" in msg, msg[:300])
        check("...the sentence is the schema's own constant", schemas.DEVICE_TOPIC_NOT_ROUTING in msg, msg[:300])
    check("no topic -> None", _create().topic is None)
    check("an explicit null -> None", _create(topic=None).topic is None)
    check("a blank topic is not a topic -> None", _create(topic="   ").topic is None)
    check("the other fields still register as before",
          _create(protocol="Modbus TCP", ip_address="192.168.10.22:502").status == "Registered")


# ----------------------------------------------------------------------------
def section_route():
    print()
    print("=" * 74)
    print("2. THE ROUTE: 422 WITH THE SENTENCE; A PAYLOAD WITHOUT A TOPIC REGISTERS THE DEVICE")
    print("=" * 74)
    install()
    admin = token("ta-admin", "Admin", "TA")
    body = {"device_code": "COMP-01", "device_name": "Customer compressor", "protocol": "Modbus TCP",
            "ip_address": "192.168.10.22:502", "topic": "flowmes/plant1/compressor"}
    code, resp = call("POST", "/industrial/devices", admin, body)
    check("POST with a topic -> 422", code == 422, f"{code} {str(resp)[:200]}")
    text = json.dumps(resp)
    check("...and the response carries the reason", "per tenant" in text and "ADR-0011" in text, text[:300])
    db = SessionLocal()
    check("...and nothing was stored", db.query(models.IndustrialDevice).count() == 0)
    db.close()

    body.pop("topic")
    code, resp = call("POST", "/industrial/devices", admin, body)
    check("POST without a topic -> registered", code == 200 and resp.get("device_code") == "COMP-01",
          f"{code} {str(resp)[:200]}")
    check("...with topic null in the response", "topic" in resp and resp.get("topic") is None, str(resp)[:200])
    db = SessionLocal()
    tok0 = tenancy.set_current_tenant("TA")
    row = db.query(models.IndustrialDevice).filter_by(device_code="COMP-01").first()
    tenancy.reset_current_tenant(tok0)
    check("...and NULL in the row", row is not None and row.topic is None)
    db.close()

    body["topic"] = ""
    body["device_code"] = "COMP-02"
    code, resp = call("POST", "/industrial/devices", admin, body)
    check("a blank topic in the payload is accepted as none", code == 200 and resp.get("topic") is None,
          f"{code} {str(resp)[:200]}")
    uninstall()


# ----------------------------------------------------------------------------
def section_read_model():
    print()
    print("=" * 74)
    print("3. THE READ MODEL: A STORED TOPIC IS NOT SHOWN AS CONFIGURATION")
    print("=" * 74)
    install()
    db = SessionLocal()
    tok0 = tenancy.set_current_tenant(None)
    db.add(models.Machine(id=1, name="IC-01", status="Running", utilization=90, line="IC", tenant_code="DEFAULT"))
    # A row written before the API refused a topic, as an old deployment may hold.
    db.add(models.IndustrialDevice(device_code="OLD-1", device_name="Old PLC", device_type="PLC", protocol="MQTT",
                                   status="Online", linked_machine_id=1, topic="flowmes/plant1/old",
                                   tenant_code="DEFAULT"))
    db.commit()
    tenancy.reset_current_tenant(tok0)
    tok1 = tenancy.set_current_tenant("DEFAULT")
    try:
        detail = connectivity.build_connection_detail(db, "DEFAULT", 1)
    finally:
        tenancy.reset_current_tenant(tok1)
    devices = detail.get("devices") or []
    check("the drawer's device row is there", len(devices) == 1 and devices[0].get("device_code") == "OLD-1",
          str(devices)[:200])
    check("...and carries no topic key at all", devices and "topic" not in devices[0], str(devices[0])[:200])
    check("...while the row still holds what was stored (honest data, not shown as config)",
          db.query(models.IndustrialDevice).first().topic == "flowmes/plant1/old")
    db.close()
    uninstall()

    drawer = os.path.join(HERE, "..", "frontend", "components", "ConnectionDrawer.tsx")
    with open(drawer, encoding="utf-8") as f:
        src = f.read()
    check("the drawer renders no device topic", "dv.topic" not in src and "topic:" not in src.split("type Device")[1].split("};")[0])


# ----------------------------------------------------------------------------
def section_claim():
    print()
    print("=" * 74)
    print("4. THE CLAIM BEHIND THE REFUSAL, CHECKED: NOTHING ROUTES BY A DEVICE'S TOPIC")
    print("=" * 74)
    for name in ("mqtt_service.py", "mqtt_identity.py", "industrial_adapters.py"):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            src = f.read()
        reads = [ln.strip() for ln in src.splitlines() if ".topic" in ln and "msg.topic" not in ln
                 and "IndustrialDevice" in ln]
        check(f"{name} never reads IndustrialDevice.topic", not reads and "IndustrialDevice.topic" not in src, str(reads))
    check("mqtt_service routes through mqtt_identity.parse_topic (tenant and site from the topic)",
          "mqtt_identity.parse_topic(" in open(os.path.join(HERE, "mqtt_service.py"), encoding="utf-8").read())
    check("PATCH cannot set a topic (IndustrialDeviceUpdate has no such field)",
          "topic" not in schemas.IndustrialDeviceUpdate.model_fields)
    check("the create schema still names the field, so a caller is told rather than silently ignored",
          "topic" in schemas.IndustrialDeviceCreate.model_fields)


def main_():
    try:
        section_schema()
        section_route()
        section_read_model()
        section_claim()
    finally:
        uninstall()
    check("every module has its real session factory back", all(mod.SessionLocal is not SessionLocal for mod in PATCHED))
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_device_topic_is_not_routing():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
