"""Learning consent for AMP-native AI: explicit, stored, audited, revocable, per tenant (ADR-0020).

THE RULE THIS SUITE ENFORCES
----------------------------
AMP never learns from a tenant's own operational data unless an ADMIN OF THAT
TENANT has said yes, and it stops the moment they say no. The first learning
capability is ``telemetry_baseline``: fitting a per-machine normal-behaviour
baseline from that machine's own recent telemetry, on request.

  1. THE GATE      no row -> refused with a reason; granted -> allowed; revoked ->
                   refused; another tenant's grant never counts (explicit filter,
                   so it holds with no ambient tenant and with the WRONG one).
  2. THE AUDIT     every change writes an AuditLog row for the right tenant, IN THE
                   SAME COMMIT as the consent row. If the audit write fails, the
                   consent change is rolled back: consent can never change silently.
  3. WHO MAY       GET /ai-consent: Admin and Supervisor. PUT: Admin only. A
                   founder PREVIEWING a customer may read but never consent on the
                   customer's behalf; the founder's own DEFAULT workspace may.
                   OEM principals are refused. /ai-consent is not plan-gated, so a
                   downgraded tenant can still see and withdraw it.
  4. REVOCATION    takes effect on the very next anomaly request (403 with the
                   reason), because nothing is cached.
  5. OFFBOARDING   purges the tenant's consent rows; the audit trail is kept.
  6. COMPANY GONE  deleting a company from the registry WITHOUT purge still
                   removes its consent (audited), so a new company given the same
                   code has not opted in.
  7. NO FORGERIES  POST /audit-logs refuses the consent audit namespace, so the
                   consent history holds only what set_consent (or the company
                   delete) wrote.

Driven at the ASGI layer through the real middleware stack (see
test_oem_routes.py for why not TestClient).

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_consent.py
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import http_security
import main
import models
import native_ai_routes
import offboard_tenant
import plan_gate
import platform_routes
import saas_routes
import tenancy
from amp_ai import consent as C
from amp_ai.core.contracts import CAPABILITY_TELEMETRY_BASELINE, ConsentDecision
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


PATCHED = (database, main, native_ai_routes, plan_gate, platform_routes, saas_routes)
_real_session_factories = {}


def install():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    for mod in PATCHED:
        _real_session_factories.setdefault(mod, mod.SessionLocal)
        mod.SessionLocal = SessionLocal
    plan_gate._licence_cache.clear()


def uninstall():
    """Give every module back the session factory install() replaced.

    CI's coverage job runs every suite in ONE pytest process, and this file sorts
    before test_boot_migrations.py. Left installed, this in-memory factory is what
    that suite's User query reached, so the query "succeeded" on a database whose
    column had never been dropped, and the job failed (PR #610's first CI run).
    """
    for mod, real in _real_session_factories.items():
        mod.SessionLocal = real
    _real_session_factories.clear()
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


async def _call(method, path, tok=None, headers=None, body=None):
    raw = [(b"host", b"testserver")]
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode()
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    path_only, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path_only,
             "raw_path": path_only.encode(), "query_string": query.encode(),
             "root_path": "", "headers": raw,
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
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


def call(method, path, tok=None, headers=None, body=None):
    # The AI routes are throttled per client; every request here is a different
    # question about authorization, not load, so each starts with a clean window.
    http_security._window.reset()
    return asyncio.run(_call(method, path, tok, headers, body))


def rows(db, tenant=None):
    q = db.query(models.AiLearningConsent)
    if tenant is not None:
        q = q.filter(models.AiLearningConsent.tenant_code == tenant)
    return q.all()


def consent_audits(db, tenant):
    return (db.query(models.AuditLog)
            .filter(models.AuditLog.tenant_code == tenant,
                    models.AuditLog.entity_type == "ai_learning_consent")
            .order_by(models.AuditLog.id).all())


def fresh():
    db = SessionLocal()
    return db


# ----------------------------------------------------------------------------
def section_gate():
    print("=" * 74)
    print("1. THE GATE: NO ROW MEANS NO, AND ONLY THIS TENANT'S ROW COUNTS")
    print("=" * 74)
    install()
    gate = C.DbConsentGate()
    db = fresh()
    d = gate.check(db, "TA", CAP)
    check("no consent row -> refused", isinstance(d, ConsentDecision) and d.granted is False, repr(d))
    check("...with a reason a person can read", bool(d.reason.strip()) and d.capability == CAP, repr(d))
    d = gate.check(db, "TA", "learn_everything")
    check("an unknown capability is refused even with nothing stored", d.granted is False, repr(d))

    C.set_consent(db, "TA", CAP, True, "ta-admin")
    db.close()
    db = fresh()
    d = gate.check(db, "TA", CAP)
    check("after an Admin grants it -> allowed", d.granted is True, repr(d))
    check("...naming who granted it and when", d.granted_by == "ta-admin"
          and isinstance(d.granted_at, datetime), repr(d))
    d = gate.check(db, "TA", "learn_everything")
    check("a grant for telemetry_baseline does not grant an unknown capability", d.granted is False, repr(d))

    d = gate.check(db, "TB", CAP)
    check("tenant A's grant does not count for tenant B (no ambient tenant)", d.granted is False, repr(d))
    tok = tenancy.set_current_tenant("TB")
    try:
        d = gate.check(db, "TB", CAP)
        check("...nor with B bound as the request tenant", d.granted is False, repr(d))
    finally:
        tenancy.reset_current_tenant(tok)
    tok = tenancy.set_current_tenant("TA")
    try:
        d = gate.check(db, "TB", CAP)
        check("...nor with A (the WRONG tenant) bound", d.granted is False, repr(d))
    finally:
        tenancy.reset_current_tenant(tok)

    C.set_consent(db, "TA", CAP, False, "ta-admin-2")
    db.close()
    db = fresh()
    d = gate.check(db, "TA", CAP)
    check("after revocation -> refused", d.granted is False, repr(d))
    check("...and the reason says it was withdrawn, and by whom",
          "revoked" in d.reason.lower() and "ta-admin-2" in d.reason, d.reason)
    C.set_consent(db, "TA", CAP, True, "ta-admin")
    check("a revoked consent can be granted again", gate.check(db, "TA", CAP).granted is True)
    db.close()


# ----------------------------------------------------------------------------
def section_audit_and_atomicity():
    print()
    print("=" * 74)
    print("2. EVERY CHANGE IS AUDITED, IN THE SAME COMMIT, OR IT DOES NOT HAPPEN")
    print("=" * 74)
    install()
    db = fresh()
    commits = {"n": 0}

    def _count(session):
        commits["n"] += 1

    event.listen(db, "after_commit", _count)
    row = C.set_consent(db, "TA", CAP, True, "ta-admin")
    check("a grant costs exactly one commit (row and audit together)", commits["n"] == 1, str(commits))
    event.remove(db, "after_commit", _count)
    row_id = row.id
    db.close()

    db = fresh()
    audits = consent_audits(db, "TA")
    check("the grant wrote exactly one audit row for TA", len(audits) == 1, str(len(audits)))
    if audits:
        a = audits[0]
        details = json.loads(a.details or "{}")
        check("...action ai.learning_consent.granted", a.action == "ai.learning_consent.granted", a.action)
        check("...by the actor", a.actor == "ta-admin", a.actor)
        check("...pointing at the consent row", a.entity_id == row_id, f"{a.entity_id} vs {row_id}")
        check("...with capability, previous and new state",
              details == {"capability": CAP, "previous": None, "new": True}, str(details))
    check("nothing was audited under another tenant", not consent_audits(db, "TB"))

    commits["n"] = 0
    event.listen(db, "after_commit", _count)
    C.set_consent(db, "TA", CAP, False, "ta-admin")
    event.remove(db, "after_commit", _count)
    check("a revocation also costs exactly one commit", commits["n"] == 1, str(commits))
    db.close()
    db = fresh()
    audits = consent_audits(db, "TA")
    check("the revocation added a second audit row", len(audits) == 2, str(len(audits)))
    if len(audits) == 2:
        details = json.loads(audits[1].details or "{}")
        check("...action ai.learning_consent.revoked with previous True, new False",
              audits[1].action == "ai.learning_consent.revoked"
              and details == {"capability": CAP, "previous": True, "new": False},
              f"{audits[1].action} {details}")

    for label, args, exc in (
            ("an unknown capability", ("TA", "learn_everything", True, "x"), ValueError),
            ("a non-boolean decision", ("TA", CAP, "yes", "x"), TypeError),
            ("a blank tenant", ("", CAP, True, "x"), ValueError)):
        before_rows, before_audits = len(rows(db)), len(consent_audits(db, "TA"))
        raised = None
        try:
            C.set_consent(db, *args)
        except Exception as e:  # noqa: BLE001 - asserted below
            raised = e
        check(f"{label} is refused with {exc.__name__}", isinstance(raised, exc), repr(raised))
        db.rollback()
        check(f"...and stores nothing", len(rows(db)) == before_rows
              and len(consent_audits(db, "TA")) == before_audits)

    # THE ATOMICITY GUARD. The audit row is made invalid (action is NOT NULL), so
    # the one commit fails. Neither the new consent nor its audit may survive.
    original = platform_routes.build_audit_row

    def broken(*a, **k):
        r = original(*a, **k)
        r.action = None
        return r

    platform_routes.build_audit_row = broken
    try:
        raised = None
        try:
            C.set_consent(db, "TC", CAP, True, "tc-admin")
        except Exception as e:  # noqa: BLE001
            raised = e
        check("a grant whose audit row cannot be written raises", raised is not None, "no exception")
        # The SAME session, not a fresh one: a failed commit that was not rolled
        # back leaves the caller's session unusable (PendingRollbackError).
        try:
            same = C.DbConsentGate().check(db, "TC", CAP)
            check("...the caller's own session is rolled back and still usable, and still refuses",
                  same.granted is False, repr(same))
        except Exception as e:  # noqa: BLE001 - asserted
            check("...the caller's own session is rolled back and still usable, and still refuses", False, repr(e))
        db.close()
        db = fresh()
        check("...and NO consent row exists for that tenant", not rows(db, "TC"), str(rows(db, "TC")))
        check("...and no audit row either", not consent_audits(db, "TC"))
        check("...so the gate still refuses", C.DbConsentGate().check(db, "TC", CAP).granted is False)

        raised = None
        try:
            C.set_consent(db, "TA", CAP, True, "ta-admin")    # TA is currently revoked
        except Exception as e:  # noqa: BLE001
            raised = e
        check("re-granting an existing row with a failing audit raises", raised is not None)
        db.close()
        db = fresh()
        check("...and the existing row stays REVOKED", C.DbConsentGate().check(db, "TA", CAP).granted is False)
        check("...with no extra audit row", len(consent_audits(db, "TA")) == 2, str(len(consent_audits(db, "TA"))))
    finally:
        platform_routes.build_audit_row = original

    # log_audit keeps its contract: it builds through the same row factory,
    # stamps the ambient tenant and never raises.
    tok = tenancy.set_current_tenant("TD")
    try:
        platform_routes.log_audit(db, "td-user", "login", "user", 7, "hello")
    finally:
        tenancy.reset_current_tenant(tok)
    got = db.query(models.AuditLog).filter(models.AuditLog.actor == "td-user").all()
    check("log_audit still writes and commits, stamped with the ambient tenant",
          len(got) == 1 and got[0].tenant_code == "TD", str([(g.tenant_code, g.action) for g in got]))
    built = platform_routes.build_audit_row("u", "a", "e", 1, "d", tenant_code="TE")
    check("build_audit_row neither adds nor commits", built not in db and built.tenant_code == "TE")
    db.close()


# ----------------------------------------------------------------------------
def section_http():
    print()
    print("=" * 74)
    print("3. WHO MAY READ AND WHO MAY CHANGE CONSENT, OVER HTTP")
    print("=" * 74)
    install()
    db = fresh()
    tok0 = tenancy.set_current_tenant(None)
    machine = models.Machine(tenant_code="TA", site="P1", name="PRESS-01", status="Running", utilization=70)
    db.add(machine)
    db.add(models.TenantConfig(tenant_code="TS", plan="starter", enabled_modules="core",
                               subscription_status="active"))
    db.commit()
    machine_id = machine.id
    tenancy.reset_current_tenant(tok0)
    db.close()

    admin = token("ta-admin", "Admin", "TA")
    sup = token("ta-sup", "Supervisor", "TA")
    op = token("ta-op", "Operator", "TA")

    code, body = call("GET", "/ai-consent", op)
    check("Operator GET /ai-consent -> 403", code == 403, f"{code} {body}")
    code, body = call("GET", "/ai-consent", sup)
    check("Supervisor GET /ai-consent -> 200", code == 200, f"{code} {body}")
    check("...read-only, and told why", body.get("can_edit") is False
          and "admin" in str(body.get("read_only_reason", "")).lower(), str(body)[:300])
    code, body = call("GET", "/ai-consent", admin)
    check("Admin GET /ai-consent -> 200 and may edit", code == 200 and body.get("can_edit") is True,
          f"{code} {str(body)[:300]}")
    caps = {c.get("capability"): c for c in body.get("capabilities", [])}
    tb = caps.get(CAP, {})
    check("the capability is listed, off, with what it reads and what it stores",
          tb.get("granted") is False and all(isinstance(tb.get(k), str) and tb.get(k) for k in
                                              ("title", "reads", "stored")), str(tb)[:300])
    check("...for this tenant", body.get("tenant") == "TA", str(body.get("tenant")))

    for who, tok in (("Operator", op), ("Supervisor", sup)):
        code, body = call("PUT", f"/ai-consent/{CAP}", tok, body={"granted": True})
        check(f"{who} PUT -> 403", code == 403, f"{code} {body}")
    db = fresh()
    check("...and neither created a consent row or an audit", not rows(db) and not consent_audits(db, "TA"))
    db.close()

    code, body = call("PUT", "/ai-consent/learn_everything", admin, body={"granted": True})
    check("Admin PUT of an unknown capability -> 400", code == 400, f"{code} {body}")
    for bad in ({"granted": "yes"}, {"granted": 1}, {}):
        code, body = call("PUT", f"/ai-consent/{CAP}", admin, body=bad)
        check(f"a malformed body {bad} is rejected (422), not coerced", code == 422, f"{code} {str(body)[:160]}")
    db = fresh()
    check("...and none of those stored anything", not rows(db))
    db.close()

    # Revocation must bite on the very next request.
    code, body = call("GET", f"/ai/native/anomaly/machines/{machine_id}", admin)
    check("before consent, the anomaly check -> 403 learning_consent_required",
          code == 403 and body.get("code") == "learning_consent_required"
          and body.get("capability") == CAP and bool(body.get("reason")), f"{code} {body}")
    code, body = call("PUT", f"/ai-consent/{CAP}", admin, body={"granted": True})
    check("Admin PUT granted=true -> 200", code == 200, f"{code} {body}")
    got = {c["capability"]: c for c in body.get("capabilities", [])}.get(CAP, {})
    check("...and the response shows it granted, by whom", got.get("granted") is True
          and got.get("granted_by") == "ta-admin", str(got))
    code, body = call("GET", f"/ai/native/anomaly/machines/{machine_id}", admin)
    check("with consent, the anomaly check answers (not enough history yet)",
          code == 200 and body.get("status") == "insufficient_history" and body.get("score") is None,
          f"{code} {str(body)[:200]}")
    code, body = call("PUT", f"/ai-consent/{CAP}", admin, body={"granted": False})
    check("Admin PUT granted=false -> 200", code == 200, f"{code} {body}")
    code, body = call("GET", f"/ai/native/anomaly/machines/{machine_id}", admin)
    check("the NEXT anomaly request after revoking -> 403, with the reason",
          code == 403 and body.get("code") == "learning_consent_required"
          and "revoked" in str(body.get("reason", "")).lower(), f"{code} {body}")
    db = fresh()
    audits = consent_audits(db, "TA")
    check("both HTTP changes were audited for TA, by the token's subject",
          [a.action for a in audits] == ["ai.learning_consent.granted", "ai.learning_consent.revoked"]
          and {a.actor for a in audits} == {"ta-admin"}, str([(a.action, a.actor) for a in audits]))
    db.close()

    # --- founder preview ----------------------------------------------------
    founder = token("founder", "Admin", "DEFAULT")
    preview = {"X-Tenant": "TA"}
    code, body = call("GET", "/ai-consent", founder, headers=preview)
    check("founder previewing TA may READ TA's consent", code == 200 and body.get("tenant") == "TA",
          f"{code} {str(body)[:200]}")
    check("...but the page says it cannot be changed from a preview",
          body.get("can_edit") is False and "preview" in str(body.get("read_only_reason", "")).lower(),
          str(body)[:300])
    code, body = call("PUT", f"/ai-consent/{CAP}", founder, headers=preview, body={"granted": True})
    check("founder previewing TA may NOT consent for TA -> 403", code == 403, f"{code} {body}")
    db = fresh()
    ta = rows(db, "TA")
    check("...TA's consent is still revoked", len(ta) == 1 and ta[0].granted is False,
          str([(r.tenant_code, r.granted) for r in ta]))
    check("...and nothing was written for DEFAULT either", not rows(db, "DEFAULT"))
    db.close()
    code, body = call("PUT", f"/ai-consent/{CAP}", founder, body={"granted": True})
    check("the founder Admin in their OWN DEFAULT workspace may consent -> 200",
          code == 200 and body.get("tenant") == "DEFAULT", f"{code} {str(body)[:200]}")
    db = fresh()
    check("...which writes a DEFAULT row", len(rows(db, "DEFAULT")) == 1)
    db.close()

    # A customer's header cannot aim the change at another tenant.
    code, body = call("PUT", f"/ai-consent/{CAP}", admin, headers={"X-Tenant": "TB"}, body={"granted": True})
    check("TA Admin sending X-Tenant: TB changes TA, not TB", code == 200 and body.get("tenant") == "TA",
          f"{code} {str(body)[:200]}")
    db = fresh()
    check("...TB has no consent row", not rows(db, "TB"))
    db.close()

    # --- principals that are not factory staff ----------------------------------
    oem = token("alpha-admin", "OEM_ADMIN", None, principal="oem", oem="OEM_ALPHA")
    for method, body in (("GET", None), ("PUT", {"granted": True})):
        code, _ = call(method, f"/ai-consent{'/' + CAP if method == 'PUT' else ''}", oem, body=body)
        check(f"an OEM token {method} /ai-consent -> 403", code == 403, str(code))
    code, _ = call("GET", "/ai-consent", None)
    check("no token -> refused", code in (401, 403), str(code))

    # --- not plan-gated ---------------------------------------------------------
    check("/ai-consent maps to no plan pack", plan_gate.pack_for_path("/ai-consent") is None
          and plan_gate.pack_for_path(f"/ai-consent/{CAP}") is None)
    starter = token("ts-admin", "Admin", "TS")
    code, body = call("GET", "/ai-consent", starter)
    check("a Starter tenant (no Intelligence Pack) can still SEE its consent", code == 200, f"{code} {body}")
    code, body = call("PUT", f"/ai-consent/{CAP}", starter, body={"granted": False})
    check("...and still WITHDRAW it", code == 200, f"{code} {body}")
    code, body = call("GET", "/ai/native/failure-risk", starter)
    check("CONTROL: the same tenant is plan-gated off the model endpoints", code == 403, f"{code} {body}")


# ----------------------------------------------------------------------------
def section_offboarding():
    print()
    print("=" * 74)
    print("4. OFFBOARDING PURGES CONSENT AND KEEPS THE AUDIT TRAIL")
    print("=" * 74)
    install()
    db = fresh()
    C.set_consent(db, "TA", CAP, True, "ta-admin")
    C.set_consent(db, "TB", CAP, True, "tb-admin")
    counts = offboard_tenant.purge_tenant_data(db, "TA")
    db.close()
    db = fresh()
    check("offboarding TA deletes TA's consent", not rows(db, "TA"), str(counts))
    check("...reports it in the per-table counts", counts.get("ai_learning_consents") == 1, str(counts))
    check("...leaves TB's consent alone", len(rows(db, "TB")) == 1)
    check("...and keeps TA's consent audit trail", len(consent_audits(db, "TA")) == 1)
    db.close()


# ----------------------------------------------------------------------------
def section_company_deleted_without_purge():
    """Consent is given by a company's Admin; it must not outlive that company.

    Deleting a company from the registry WITHOUT ?purge=true leaves its rows
    behind (pre-existing), and create_company_tenant accepts the same code again.
    The consent row must not be among the leftovers: a new company with that code
    never opted in, so the gate must refuse it rather than cite the old Admin.
    """
    print()
    print("=" * 74)
    print("5. A DELETED COMPANY'S CONSENT IS NOT INHERITED BY A NEW COMPANY WITH ITS CODE")
    print("=" * 74)
    install()
    founder = token("founder", "Admin", "DEFAULT")
    code, body = call("POST", "/saas/tenants", founder, body={"company_code": "TR", "company_name": "Original Co"})
    check("the founder registers TR", code == 200 and isinstance(body.get("id"), int), f"{code} {str(body)[:200]}")
    tenant_id = body.get("id")
    code, body = call("PUT", f"/ai-consent/{CAP}", token("tr-admin", "Admin", "TR"), body={"granted": True})
    check("TR's own Admin grants telemetry_baseline", code == 200, f"{code} {str(body)[:200]}")
    db = fresh()
    C.set_consent(db, "TB", CAP, True, "tb-admin")
    check("CONTROL: before the delete, TR's consent is granted", C.DbConsentGate().check(db, "TR", CAP).granted is True)
    db.close()

    code, body = call("DELETE", f"/saas/tenants/{tenant_id}", founder)
    check("the founder deletes TR WITHOUT purge", code == 200 and body.get("purged") is None,
          f"{code} {str(body)[:200]}")
    db = fresh()
    d = C.DbConsentGate().check(db, "TR", CAP)
    check("TR's consent is gone with the company: the gate refuses", d.granted is False, repr(d))
    check("...no consent row is left for TR", not rows(db, "TR"), str([(r.tenant_code, r.granted) for r in rows(db)]))
    check("...another company's consent is untouched", C.DbConsentGate().check(db, "TB", CAP).granted is True)
    removed = [a for a in consent_audits(db, "TR") if a.action == C.AUDIT_REMOVED_WITH_COMPANY]
    check("...and the removal is audited in TR's trail, by the founder", len(removed) == 1
          and removed[0].actor == "founder", str([(a.action, a.actor) for a in consent_audits(db, "TR")]))
    if removed:
        details = json.loads(removed[0].details or "{}")
        check("...naming the capability and that it was granted", details.get("capability") == CAP
              and details.get("previous") is True and details.get("new") is False, str(details))
    check("...and TR's earlier grant record is kept", [a.action for a in consent_audits(db, "TR")][:1]
          == [C.AUDIT_GRANTED], str([a.action for a in consent_audits(db, "TR")]))
    db.close()

    code, body = call("POST", "/saas/tenants", founder,
                      body={"company_code": "TR", "company_name": "Totally Different Co"})
    check("a new company re-uses the code TR", code == 200, f"{code} {str(body)[:200]}")
    db = fresh()
    d = C.DbConsentGate().check(db, "TR", CAP)
    check("the new TR has NOT opted in: the gate refuses", d.granted is False, repr(d))
    check("...and does not cite the old company's Admin", "tr-admin" not in d.reason, d.reason)
    db.close()
    code, body = call("GET", "/ai-consent", token("tr-admin-2", "Admin", "TR"))
    got = {c["capability"]: c for c in body.get("capabilities", [])}.get(CAP, {})
    check("the new TR's consent page shows it off and never granted", code == 200 and got.get("granted") is False
          and got.get("granted_by") is None, f"{code} {str(got)[:200]}")


# ----------------------------------------------------------------------------
def section_consent_audit_not_forgeable():
    """Only set_consent (and the company delete) may write consent audit records.

    POST /audit-logs appends a caller-described row. A consent grant or revoke
    written there looks exactly like the real one, so the trail the consent card
    calls the full history could be filled with events that never happened.
    """
    print()
    print("=" * 74)
    print("6. THE CONSENT AUDIT TRAIL CANNOT BE WRITTEN THROUGH POST /audit-logs")
    print("=" * 74)
    install()
    admin = token("ta-admin", "Admin", "TA")
    forged = [
        {"action": "ai.learning_consent.revoked", "entity_type": "ai_learning_consent",
         "details": json.dumps({"capability": CAP, "previous": True, "new": False})},
        {"action": "ai.learning_consent.granted", "entity_type": "report"},
        {"action": "  AI.Learning_Consent.Granted ", "entity_type": None},
        {"action": "report_requested", "entity_type": "AI_Learning_Consent"},
        {"action": C.AUDIT_REMOVED_WITH_COMPANY},
    ]
    for payload in forged:
        code, body = call("POST", "/audit-logs", admin, body=payload)
        check(f"POST /audit-logs {payload.get('action')!r} / {payload.get('entity_type')!r} -> 400",
              code == 400, f"{code} {str(body)[:200]}")
    db = fresh()
    check("...and none of them was stored", db.query(models.AuditLog).count() == 0,
          str([(a.action, a.entity_type) for a in db.query(models.AuditLog).all()]))
    db.close()
    code, body = call("POST", "/audit-logs", admin, body={"action": "report_requested", "entity_type": "report",
                                                           "details": "monthly OEE"})
    check("CONTROL: an ordinary audit record is still accepted", code == 200 and body.get("actor") == "ta-admin",
          f"{code} {str(body)[:200]}")
    db = fresh()
    C.set_consent(db, "TA", CAP, True, "ta-admin")
    check("CONTROL: set_consent still writes its own audit record", len(consent_audits(db, "TA")) == 1)
    db.close()


def main_():
    try:
        section_gate()
        section_audit_and_atomicity()
        section_http()
        section_offboarding()
        section_company_deleted_without_purge()
        section_consent_audit_not_forgeable()
    finally:
        uninstall()
    check("every module has its real session factory back (the coverage job shares one process)",
          all(mod.SessionLocal is not SessionLocal for mod in PATCHED))
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_consent():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
