"""The AMP-native AI endpoints, over HTTP: RBAC, tenant, consent, plan gate, model cards (ADR-0020).

THE CHAIN, TESTED IN ORDER
--------------------------
USER -> AUTHENTICATION -> RBAC -> TENANT / CONSENT -> AMP TOOL -> DATA -> MODEL.

  1. RBAC MATRIX   failure-risk and anomaly: Admin + Supervisor. Model cards: any
                   signed-in factory user. OEM tokens and no token: refused.
  2. TENANT FIRST  another tenant's machine id is a 404 BEFORE consent is looked at
                   (the gate is spied: it is never called), so a probe learns
                   nothing about anyone's consent settings.
  3. CONSENT       no consent -> 403 {code: learning_consent_required, capability,
                   reason}. With consent: an honest 200 - "insufficient_history"
                   with a null score, or a real score labelled experimental.
  3b. PREVIEW      a founder previewing a customer (X-Tenant) is not that
                   customer's Admin or Supervisor, so the consented learning step
                   is refused (403 learning_not_from_preview) before consent or
                   telemetry is read; failure risk (no learning) still answers.
  4. PLAN GATE    /ai/native/* and /ai/models are Intelligence Pack; /ai-consent
                   is not gated.
  5. MODEL CARDS   looked up in a fixed registry, never a path; no "parameters"
                   anywhere in the card; the verdicts are the committed ones.
  6. TAMPERING     an artifact edited AND re-hashed (so its own embedded hash still
                   matches) is refused because the hash pinned in code does not
                   match: the card says unavailable and the endpoints say
                   model_unavailable - the failure-risk endpoint still shows the rule.
  7. THROTTLE      both endpoints are rate-limited, and scanning many machine ids
                   shares ONE budget rather than getting a fresh one per id. The
                   bucket is the verified token's principal: a new X-Forwarded-For
                   buys nothing, and requests without a valid token (none, junk,
                   or signed with the wrong key) are never counted, so they cannot
                   lock a signed-in user out by claiming the factory's address.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_routes.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import http_security
import main
import models
import native_ai_routes
import plan_gate
import platform_routes
import tenancy
from amp_ai import consent as C
from amp_ai import registry
from amp_ai.copilot_intent import classifier
from amp_ai.core.artifact import payload_hash
from amp_ai.core.contracts import CAPABILITY_TELEMETRY_BASELINE
from amp_ai.failure_risk import predict
from amp_ai.telemetry_anomaly import service
from amp_ai.telemetry_anomaly import synthetic as SY
from database import Base

CAP = CAPABILITY_TELEMETRY_BASELINE
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def install():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    for mod in (database, main, native_ai_routes, plan_gate, platform_routes):
        mod.SessionLocal = SessionLocal
    plan_gate._licence_cache.clear()


def token(sub, role, tenant, principal=None, oem=None):
    claims = {"sub": sub, "role": role, "exp": datetime.utcnow() + timedelta(hours=1)}
    if tenant is not None:
        claims["tenant"] = tenant
    if principal:
        claims["principal"] = principal
    if oem:
        claims["oem"] = oem
    return pyjwt.encode(claims, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


async def _call(method, path, tok=None, headers=None, body=None, client="127.0.0.1"):
    raw = [(b"host", b"testserver")]
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode()
        raw.append((b"content-type", b"application/json"))
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    path_only, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path_only,
             "raw_path": path_only.encode(), "query_string": query.encode(),
             "root_path": "", "headers": raw,
             "client": (client, 5000), "server": ("testserver", 80)}
    chunks, status = [], {}
    sent = {"done": False}

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


def call(method, path, tok=None, headers=None, body=None, reset=True, client="127.0.0.1"):
    if reset:
        http_security._window.reset()
    return asyncio.run(_call(method, path, tok, headers, body, client))


def keys_anywhere(obj, key):
    """Every path at which `key` appears as a dict key, however deep."""
    found = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == key:
                    found.append(path + [k])
                walk(v, path + [k])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + [i])

    walk(obj, [])
    return found


def seed_factories():
    """Two factories. TA: two machines with a little history. TB: one machine."""
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    now = datetime.utcnow()
    ids = {}
    for tenant, names in (("TA", ["TA-PRESS-1", "TA-PRESS-2"]), ("TB", ["TB-LATHE-1"])):
        for name in names:
            m = models.Machine(tenant_code=tenant, site="P1", name=name, status="Running", utilization=70)
            db.add(m)
            db.flush()
            ids[name] = m.id
            db.add(models.MachineEvent(tenant_code=tenant, machine_id=m.id, machine_name=name,
                                       old_status="Idle", new_status="Running", utilization=70,
                                       created_at=now - timedelta(days=10)))
            db.add(models.DowntimeLog(tenant_code=tenant, machine_id=m.id, reason="Breakdown",
                                      duration="45 min", created_at=now - timedelta(days=3)))
    db.add(models.TenantConfig(tenant_code="TS", plan="starter", enabled_modules="core",
                               subscription_status="active"))
    db.add(models.Machine(tenant_code="TS", site="P1", name="TS-1", status="Running", utilization=50))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return ids


def seed_telemetry(tenant, machine_id, seed):
    """Four days of one synthetic machine's 1-minute telemetry ending now, plus its state changes."""
    series = next(SY.generate_dataset(seed, "routes", n_series=1, windows_per_series=1, min_history_days=4,
                                      max_history_days=4, anomaly_fraction=0.0))
    anchor = series.windows[0].end_minute
    first = max(0, anchor - 4 * 24 * 60)
    now = datetime.utcnow()

    def ts(minute):
        return now - timedelta(minutes=anchor - minute) - timedelta(seconds=30)

    rows = []
    for name, values in series.signals.items():
        for minute in range(first, anchor):
            if values[minute] is not None:
                rows.append({"tenant_code": tenant, "machine_id": machine_id, "signal_name": name,
                             "signal_value": repr(values[minute]), "numeric_value": int(values[minute]),
                             "source": "MQTT", "created_at": ts(minute)})
    db = SessionLocal()
    db.execute(models.IoTTelemetry.__table__.insert(), rows)
    for minute, running in SY.state_transitions(series):
        if first <= minute < anchor:
            db.add(models.MachineEvent(tenant_code=tenant, machine_id=machine_id, machine_name="syn",
                                       old_status=SY.status_of(not running), new_status=SY.status_of(running),
                                       created_at=ts(minute)))
    db.commit()
    db.close()
    return len(rows)


def tampered_copy(src, edit):
    """A copy of an artifact with `edit` applied and its EMBEDDED hash recomputed to match."""
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    edit(data)
    data["sha256"] = payload_hash(data)
    folder = tempfile.mkdtemp(prefix="amp_ai_tamper_")
    path = os.path.join(folder, os.path.basename(src))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return folder, path


# ----------------------------------------------------------------------------
def section_rbac(ids):
    print("=" * 74)
    print("1. RBAC MATRIX")
    print("=" * 74)
    toks = {role: token(f"ta-{role.lower()}", role, "TA") for role in ("Admin", "Supervisor", "Operator")}
    oem = token("alpha-admin", "OEM_ADMIN", None, principal="oem", oem="OEM_ALPHA")
    mid = ids["TA-PRESS-1"]
    matrix = [
        ("/ai/native/failure-risk", {"Admin": 200, "Supervisor": 200, "Operator": 403}),
        (f"/ai/native/anomaly/machines/{mid}", {"Admin": 403, "Supervisor": 403, "Operator": 403}),
        ("/ai/models", {"Admin": 200, "Supervisor": 200, "Operator": 200}),
        ("/ai/models/failure_risk", {"Admin": 200, "Supervisor": 200, "Operator": 200}),
    ]
    # Anomaly without consent is 403 for Admin/Supervisor by CONSENT, and for the
    # Operator by ROLE. The body tells the two apart.
    for path, expected in matrix:
        for role, want in expected.items():
            code, body = call("GET", path, toks[role])
            check(f"{role:<10} GET {path} -> {want}", code == want, f"{code} {str(body)[:160]}")
            if path.startswith("/ai/native/anomaly") and role != "Operator":
                check(f"{role:<10} ...refused for CONSENT, not role",
                      body.get("code") == "learning_consent_required", str(body)[:200])
            if path.startswith("/ai/native/anomaly") and role == "Operator":
                check("Operator     ...refused for ROLE (no consent code leaks)",
                      body.get("code") is None, str(body)[:200])
        code, _ = call("GET", path, oem)
        check(f"OEM token    GET {path} -> 403", code == 403, str(code))
        code, _ = call("GET", path, None)
        check(f"no token     GET {path} -> refused", code in (401, 403), str(code))


def section_failure_risk(ids):
    print()
    print("=" * 74)
    print("2. FAILURE RISK: THE MODEL BESIDE THE RULE, THIS TENANT ONLY")
    print("=" * 74)
    code, body = call("GET", "/ai/native/failure-risk", token("ta-admin", "Admin", "TA"))
    check("200", code == 200, f"{code} {str(body)[:200]}")
    machines = body.get("machines", [])
    names = sorted(m.get("name") for m in machines)
    check("exactly TA's machines, none of TB's", names == ["TA-PRESS-1", "TA-PRESS-2"], str(names))
    check("status ok, from the pinned artifact", body.get("status") == "ok"
          and body.get("artifact_sha256") == predict.ARTIFACT_SHA256, str({k: body.get(k) for k in
                                                                           ("status", "artifact_sha256")}))
    check("the synthetic-only caveat travels with the answer",
          body.get("caveat") == predict.CAVEAT, str(body.get("caveat")))
    check("every machine carries the rule score beside the model",
          machines and all(isinstance(m.get("rule_score"), (int, float)) for m in machines), str(machines)[:200])
    check("...and says the tenant's data was read, not learned from",
          machines and all(m.get("data_basis", {}).get("learned_from") is False for m in machines))
    check("the adopted flag is the committed verdict", body.get("adopted") is True, str(body.get("adopted")))
    check("the response explains the rule number's basis", isinstance(body.get("rule_basis"), str)
          and body.get("rule_basis"), str(body.get("rule_basis")))
    code, body_b = call("GET", "/ai/native/failure-risk", token("tb-sup", "Supervisor", "TB"))
    check("TB sees only TB's machine", code == 200
          and [m.get("name") for m in body_b.get("machines", [])] == ["TB-LATHE-1"], str(body_b)[:200])


def section_anomaly(ids):
    print()
    print("=" * 74)
    print("3. ANOMALY: TENANT BEFORE CONSENT, CONSENT BEFORE DATA")
    print("=" * 74)
    ta_admin = token("ta-admin", "Admin", "TA")
    calls = []
    original = C.DbConsentGate.check

    def spy(self, db, tenant, capability):
        calls.append((tenant, capability))
        return original(self, db, tenant, capability)

    loads = []
    original_load = service.DT.load

    def load_spy(db, tenant, machine_id, **kw):
        loads.append((tenant, machine_id))
        return original_load(db, tenant, machine_id, **kw)

    C.DbConsentGate.check = spy
    service.DT.load = load_spy
    try:
        db = SessionLocal()
        C.set_consent(db, "TB", CAP, True, "tb-admin")      # B HAS consented; A has not
        db.close()
        calls.clear()
        code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TB-LATHE-1']}", ta_admin)
        check("TA asking about TB's machine -> 404", code == 404, f"{code} {body}")
        check("...and the consent gate was never consulted", calls == [], str(calls))
        check("...and the body mentions no consent", "consent" not in json.dumps(body).lower(), str(body))
        code, body = call("GET", "/ai/native/anomaly/machines/999999", ta_admin)
        check("a machine id that exists nowhere -> the same 404", code == 404 and calls == [], f"{code} {calls}")

        code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", ta_admin)
        check("TA's own machine without TA's consent -> 403 (TB's consent does not count)",
              code == 403, f"{code} {body}")
        check("...the gate was asked about TA only", calls == [("TA", CAP)], str(calls))
        check("...the body names the code, capability and a reason",
              body.get("code") == "learning_consent_required" and body.get("capability") == CAP
              and isinstance(body.get("reason"), str) and body.get("reason"), str(body))
        check("a tenant that has not opted in has NONE of its telemetry read (nothing to learn from)",
              loads == [], str(loads))
    finally:
        C.DbConsentGate.check = original
        service.DT.load = original_load

    db = SessionLocal()
    C.set_consent(db, "TA", CAP, True, "ta-admin")
    db.close()
    code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-2']}", ta_admin)
    check("with consent and no telemetry -> 200 insufficient_history", code == 200
          and body.get("status") == "insufficient_history", f"{code} {str(body)[:200]}")
    check("...score is null, never 0", "score" in body and body.get("score") is None, str(body.get("score")))
    check("...with what is needed and what there is", isinstance(body.get("needed"), dict)
          and isinstance(body.get("have"), dict), str(body)[:300])

    n = seed_telemetry("TA", ids["TA-PRESS-1"], seed=41)
    loads = []
    service.DT.load = load_spy
    try:
        code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", ta_admin)
    finally:
        service.DT.load = original_load
    check("CONTROL: once TA has opted in, the same request does read TA's telemetry (the spy can fire)",
          loads == [("TA", ids["TA-PRESS-1"])], str(loads))
    code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", ta_admin)
    check(f"with consent and 4 days of telemetry ({n} rows) -> 200 ok", code == 200
          and body.get("status") == "ok", f"{code} {str(body)[:300]}")
    score = body.get("score")
    check("...a score in [0, 1]", isinstance(score, (int, float)) and 0.0 <= score <= 1.0, str(score))
    ev = body.get("evaluation", {})
    check("...labelled experimental, because the committed evaluation was not adopted",
          ev.get("experimental") is True and ev.get("adopted") is False, str(ev))
    check("...with the synthetic-only caveat", ev.get("caveat") == service.CAVEAT, str(ev.get("caveat")))
    check("...and the telemetry source flag", "simulated_source" in body, str(sorted(body))[:200])
    db = SessionLocal()
    check("scoring persisted nothing", db.query(models.IoTTelemetry).count() == n)
    db.close()


def section_founder_preview(ids):
    """A platform operator previewing a customer (X-Tenant) is not that customer's Admin or Supervisor.

    The consent an Admin gives covers "an Admin or Supervisor" of THEIR company
    opening the anomaly check (consent.CAPABILITY_INFO). So the learning step
    must not run from a founder preview, even though TA has consented: refused
    before the consent row or any telemetry is read. Scoring without learning
    (failure risk) stays available in a preview, like every other factory read.
    """
    print()
    print("=" * 74)
    print("3b. FOUNDER PREVIEW: THE CONSENTED LEARNING STEP DOES NOT RUN FROM A PREVIEW")
    print("=" * 74)
    founder = token("founder", "Admin", "DEFAULT")
    preview = {"X-Tenant": "TA"}
    target = f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}"
    code, body = call("GET", target, token("ta-admin", "Admin", "TA"))
    check("CONTROL: TA has consented and has telemetry, so TA's own Admin gets a score",
          code == 200 and body.get("status") == "ok", f"{code} {str(body)[:200]}")

    db = SessionLocal()
    audits_before = db.query(models.AuditLog).count()
    db.close()
    calls, loads = [], []
    original, original_load = C.DbConsentGate.check, service.DT.load

    def spy(self, db, tenant, capability):
        calls.append((tenant, capability))
        return original(self, db, tenant, capability)

    def load_spy(db, tenant, machine_id, **kw):
        loads.append((tenant, machine_id))
        return original_load(db, tenant, machine_id, **kw)

    C.DbConsentGate.check, service.DT.load = spy, load_spy
    try:
        code, body = call("GET", target, founder, headers=preview)
        check("the founder previewing TA -> 403", code == 403, f"{code} {str(body)[:200]}")
        check("...with its own code, not the consent code (TA HAS consented)",
              body.get("code") == "learning_not_from_preview", str(body)[:200])
        check("...and a reason that names the preview", "preview" in str(body.get("reason", "")).lower(),
              str(body)[:300])
        check("...refused before TA's consent row was read", calls == [], str(calls))
        check("...and before any of TA's telemetry was read", loads == [], str(loads))
        check("...and without a score", "score" not in body or body.get("score") is None, str(body)[:200])
        code, body = call("GET", "/ai/native/anomaly/machines/999999", founder, headers=preview)
        check("a preview of a machine id that exists nowhere is the same 403 (nothing is looked up)",
              code == 403 and body.get("code") == "learning_not_from_preview" and calls == [] and loads == [],
              f"{code} {calls} {loads}")
    finally:
        C.DbConsentGate.check, service.DT.load = original, original_load
    db = SessionLocal()
    check("nothing was written to the audit trail by the refused previews",
          db.query(models.AuditLog).count() == audits_before)
    db.close()

    code, body = call("GET", target, token("ta-sup", "Supervisor", "TA"), headers={"X-Tenant": "TB"})
    check("CONTROL: a TA Supervisor's X-Tenant header is ignored (no preview), so the check runs for TA",
          code == 200 and body.get("status") == "ok", f"{code} {str(body)[:200]}")
    reads = C.CAPABILITY_INFO[CAP]["reads"].lower()
    check("the consent text an Admin agrees to says whose request it covers, and that a preview is not one",
          "own admins or supervisors" in reads and "preview" in reads, C.CAPABILITY_INFO[CAP]["reads"])
    code, body = call("GET", "/ai/native/failure-risk", founder, headers=preview)
    check("CONTROL: scoring without learning (failure risk) still works in a preview",
          code == 200 and sorted(m.get("name") for m in body.get("machines", [])) == ["TA-PRESS-1", "TA-PRESS-2"],
          f"{code} {str(body)[:200]}")


def section_plan_gate(ids):
    print()
    print("=" * 74)
    print("4. PLAN GATE")
    print("=" * 74)
    for path, pack in (("/ai/native/failure-risk", "intelligence"),
                       ("/ai/native/anomaly/machines/1", "intelligence"),
                       ("/ai/models", "intelligence"), ("/ai/models/failure_risk", "intelligence"),
                       ("/ai-consent", None), ("/ai-consent/telemetry_baseline", None)):
        check(f"pack_for_path({path}) == {pack}", plan_gate.pack_for_path(path) == pack,
              str(plan_gate.pack_for_path(path)))
    starter = token("ts-admin", "Admin", "TS")
    for path in ("/ai/native/failure-risk", "/ai/models"):
        code, body = call("GET", path, starter)
        check(f"a Starter tenant GET {path} -> 403 by plan", code == 403
              and "plan" in json.dumps(body).lower(), f"{code} {body}")


def section_cards():
    print()
    print("=" * 74)
    print("5. MODEL CARDS: A FIXED REGISTRY, METADATA ONLY")
    print("=" * 74)
    op = token("ta-op", "Operator", "TA")
    code, body = call("GET", "/ai/models", op)
    cards = {c.get("name"): c for c in body.get("models", [])}
    check("the list is exactly the registry", code == 200
          and sorted(cards) == ["copilot_intent", "failure_risk", "telemetry_anomaly"], str(sorted(cards)))
    check("no card contains a 'parameters' key at any depth",
          not keys_anywhere(body, "parameters"), str(keys_anywhere(body, "parameters"))[:200])
    expected = {"failure_risk": (True, "adopted", predict.ARTIFACT_SHA256),
                "telemetry_anomaly": (False, "experimental", service.EVAL_SHA256),
                "copilot_intent": (False, "experimental", classifier.ARTIFACT_SHA256)}
    for name, (adopted, status, sha) in expected.items():
        c = cards.get(name, {})
        check(f"{name}: available, adopted={adopted}, status={status}",
              c.get("available") is True and c.get("adopted") is adopted and c.get("status") == status,
              str({k: c.get(k) for k in ("available", "adopted", "status", "reason")}))
        check(f"{name}: sha256 is the hash pinned in code", c.get("sha256") == sha, str(c.get("sha256")))
        check(f"{name}: carries provenance, metrics, baseline and the ledger",
              all(c.get(k) for k in ("training_data_source", "consent_basis", "generator", "generator_sha256",
                                      "created_at", "metrics", "baseline_metrics", "eval_ledger", "caveat")),
              str(sorted(k for k in c if c.get(k)))[:300])
        check(f"{name}: headline rows compare model and baseline on the same data",
              c.get("headline") and all(isinstance(h.get("model"), (int, float))
                                        and isinstance(h.get("baseline"), (int, float))
                                        for h in c.get("headline", [])), str(c.get("headline"))[:300])
        check(f"{name}: every headline row says whether it is a percentage or a score",
              c.get("headline") and all(h.get("unit") in ("percent", "score") for h in c.get("headline", [])),
              str([h.get("unit") for h in c.get("headline", [])]))
        if not adopted:
            check(f"{name}: the not-adopted reasons are shown", c.get("reasons"), str(c.get("reasons")))
    check("telemetry_anomaly's card carries its misspecification results",
          bool(cards.get("telemetry_anomaly", {}).get("misspecification")))
    code, one = call("GET", "/ai/models/copilot_intent", op)
    check("GET /ai/models/copilot_intent -> the same card", code == 200
          and one.get("sha256") == classifier.ARTIFACT_SHA256 and not keys_anywhere(one, "parameters"),
          f"{code} {str(one)[:160]}")
    for bad in ("..", "failure_risk_v1", "failure_risk_v1.json", "%2e%2e", "__init__", "registry", "MODELS",
                "..%2Fartifacts%2Ffailure_risk_v1.json", "FAILURE_RISK", "failure_risk "):
        code, body = call("GET", f"/ai/models/{bad}", op)
        check(f"GET /ai/models/{bad} -> 404", code == 404, f"{code} {str(body)[:120]}")
    check("registry.card() returns None for a path-like name, never reading a file",
          registry.card("../artifacts/failure_risk_v1") is None and registry.card("") is None
          and registry.card(None) is None)


def section_tampering(ids):
    print()
    print("=" * 74)
    print("6. A TAMPERED ARTIFACT IS REFUSED, EVERYWHERE IT IS USED")
    print("=" * 74)
    admin = token("ta-admin", "Admin", "TA")
    folders = []
    try:
        # failure risk: flip nothing visible, change one coefficient, re-hash.
        def fr_edit(d):
            d["parameters"]["logistic"]["intercept"] = d["parameters"]["logistic"]["intercept"] + 0.5
        folder, path = tampered_copy(predict.ARTIFACT_PATH, fr_edit)
        folders.append(folder)
        original = predict.ARTIFACT_PATH
        predict.ARTIFACT_PATH = path
        try:
            code, card = call("GET", "/ai/models/failure_risk", admin)
            check("failure_risk card: unavailable, with the reason", code == 200 and card.get("available") is False
                  and "pinned" in str(card.get("reason", "")), f"{code} {str(card)[:200]}")
            check("...status unavailable, and no metrics are shown from the untrusted file",
                  card.get("status") == "unavailable" and not card.get("metrics"), str(card)[:200])
            code, body = call("GET", "/ai/native/failure-risk", admin)
            check("failure-risk endpoint: model_unavailable", code == 200
                  and body.get("status") == "model_unavailable", f"{code} {str(body)[:200]}")
            machines = body.get("machines", [])
            check("...but the RULE is still shown for every machine",
                  sorted(m.get("name") for m in machines) == ["TA-PRESS-1", "TA-PRESS-2"]
                  and all(isinstance(m.get("rule_score"), (int, float)) and m.get("probability") is None
                          for m in machines), str(machines)[:300])
        finally:
            predict.ARTIFACT_PATH = original

        # telemetry anomaly: promote the verdict to adopted, re-hash.
        def ta_edit(d):
            d["adoption"]["adopted"] = True
            d["adoption"]["reasons"] = []
        folder, path = tampered_copy(service.EVAL_ARTIFACT_PATH, ta_edit)
        folders.append(folder)
        original = service.EVAL_ARTIFACT_PATH
        service.EVAL_ARTIFACT_PATH = path
        try:
            code, card = call("GET", "/ai/models/telemetry_anomaly", admin)
            check("telemetry_anomaly card: a verdict edited to 'adopted' is unavailable, not adopted",
                  card.get("available") is False and card.get("adopted") is not True, str(card)[:200])
            code, body = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", admin)
            check("anomaly endpoint: model_unavailable, score null", code == 200
                  and body.get("status") == "model_unavailable" and body.get("score") is None,
                  f"{code} {str(body)[:200]}")
        finally:
            service.EVAL_ARTIFACT_PATH = original

        # copilot intent: promote to adopted, re-hash.
        def ci_edit(d):
            d["adoption"]["adopted"] = True
            d["adoption"]["reasons"] = []
        folder, path = tampered_copy(classifier.ARTIFACT_PATH, ci_edit)
        folders.append(folder)
        original = classifier.ARTIFACT_PATH
        classifier.ARTIFACT_PATH = path
        try:
            code, card = call("GET", "/ai/models/copilot_intent", admin)
            check("copilot_intent card: edited to 'adopted' -> unavailable", card.get("available") is False
                  and card.get("adopted") is not True, str(card)[:200])
            import ai_copilot
            check("...and the native copilot does not switch on", ai_copilot.NATIVE.is_configured() is False
                  and ai_copilot._answer_engine() == "rules", ai_copilot._answer_engine())
        finally:
            classifier.ARTIFACT_PATH = original

        code, card = call("GET", "/ai/models/failure_risk", admin)
        check("CONTROL: with the committed file restored the card is available again",
              card.get("available") is True, str(card)[:160])
    finally:
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)


def section_throttle(ids):
    print()
    print("=" * 74)
    print("7. RATE LIMITS")
    print("=" * 74)
    check("/ai/native/failure-risk is throttled", http_security._limit_for("/ai/native/failure-risk") is not None)
    check("/ai/native/anomaly/machines/<id> is throttled",
          http_security._limit_for(f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}") is not None)
    limit = http_security.RATE_LIMITS["/ai/native/anomaly"][0]
    sup = token("ta-sup", "Supervisor", "TA")
    http_security._window.reset()
    codes = []
    for i in range(limit + 1):
        code, _ = call("GET", f"/ai/native/anomaly/machines/{900000 + i}", sup, reset=False, client="10.9.9.9")
        codes.append(code)
    check(f"{limit} requests to {limit} DIFFERENT machine ids pass, the next is 429 (one budget, not one per id)",
          all(c != 429 for c in codes[:limit]) and codes[limit] == 429, str(codes[-3:]))
    code, _ = call("GET", "/ai/native/failure-risk", sup, reset=False, client="10.9.9.9")
    check("...while the failure-risk budget is separate", code == 200, str(code))
    code, _ = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", token("ta-admin", "Admin", "TA"),
                   reset=False, client="10.9.9.9")
    check("...and another signed-in user, even from the same address, is not throttled", code != 429, str(code))

    # The bucket is the verified token's principal, not the client address: an
    # address is whatever the client puts in X-Forwarded-For.
    code, _ = call("GET", f"/ai/native/anomaly/machines/{ids['TA-PRESS-1']}", sup, reset=False, client="10.8.8.8",
                   headers={"X-Forwarded-For": "198.51.100.23"})
    check("...while the throttled user coming from another address is STILL throttled", code == 429, str(code))
    http_security._window.reset()
    rotating = token("ta-sup-2", "Supervisor", "TA")
    codes = [call("GET", f"/ai/native/anomaly/machines/{900000 + i}", rotating, reset=False,
                  headers={"X-Forwarded-For": f"203.0.113.{i + 1}"})[0] for i in range(limit + 1)]
    check(f"a new X-Forwarded-For on every request does not buy a fresh budget (request {limit + 1} -> 429)",
          all(c != 429 for c in codes[:limit]) and codes[limit] == 429, str(codes[-3:]))

    http_security._window.reset()
    victim_address = {"X-Forwarded-For": "192.0.2.77"}
    codes = [call("GET", "/ai/native/failure-risk", None, reset=False, headers=victim_address)[0]
             for _ in range(limit * 2)]
    forged = pyjwt.encode({"sub": "ta-sup", "role": "Supervisor", "tenant": "TA",
                           "exp": datetime.utcnow() + timedelta(hours=1)}, "not-the-server-key", algorithm=auth.ALGORITHM)
    for bad in ("not-a-valid-token", forged):
        codes += [call("GET", "/ai/native/failure-risk", bad, reset=False, headers=victim_address)[0]
                  for _ in range(limit * 2)]
    check("requests with no token or a forged token are refused by authentication, never counted (no 429)",
          all(c in (401, 403) for c in codes), str(sorted(set(codes))))
    code, _ = call("GET", "/ai/native/failure-risk", sup, reset=False, headers=victim_address)
    check("...so they cannot spend a factory user's budget by sharing the factory's address", code == 200, str(code))
    http_security._window.reset()
    login_limit = http_security.RATE_LIMITS["/login"][0]
    check("CONTROL: an exact-path entry keeps its own bucket", http_security._limit_for("/login") is not None
          and login_limit >= 1)


def main_():
    install()
    ids = seed_factories()
    section_rbac(ids)
    section_failure_risk(ids)
    section_anomaly(ids)
    section_founder_preview(ids)
    section_plan_gate(ids)
    section_cards()
    section_tampering(ids)
    section_throttle(ids)
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_routes():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
