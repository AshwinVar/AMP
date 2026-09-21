"""A company's data goes to a hosted AI model only with that company's consent (ADR-0037).

THE DEFECT THIS CLOSES
----------------------
Setting ANTHROPIC_API_KEY or GEMINI_API_KEY on the platform made every
company's Copilot questions - and the evidence behind their answers: machine,
order and item names, figures, windows - go to the provider, for every tenant,
with no company ever asked. ADR-0023 recorded the question as open; the
learning consent of ADR-0020 already had the right shape (explicit, stored,
audited, revocable, the company's own Admin, never a preview). This adds the
second capability, `external_model`, to that gate, and checks it at the ONE
place the Copilot builds a request's model.

WHAT IS PINNED
--------------
  1. THE CONTRACT   external_model is a consent capability and not a learning
                    one; the consent card's wording says what leaves, to whom,
                    what is kept and what happens without it.
  2. THE PAGE       GET /ai-consent lists it beside the learning capability; an
                    Admin's PUT grants it with an audit row; a Supervisor and a
                    preview cannot.
  3. /ai/ask        with a hosted key configured: no consent -> the provider is
                    NEVER called, the answer is AMP's own and the note says why;
                    a LEARNING consent does not count; granted -> called (any
                    role of the company); revoked -> not called on the very next
                    question; granted again -> called.
  4. ANOTHER COMPANY's consent never counts.
  5. A FOUNDER PREVIEW never sends the company's data, whatever the company or
                    the founder's own workspace decided.
  6. /ai/report     the same gate.
  7. /ai/status     says whether THIS company's questions will reach the
                    provider, and why not; nothing to consent to when no hosted
                    provider is configured or the model is self-hosted.
  8. SELF-HOSTED    the local provider never consults the gate (nothing leaves
                    AMP); the hosted one asks for exactly this tenant and this
                    capability.
  9. STRUCTURE      ai_copilot builds a ProviderLLM in exactly one place, behind
                    the gate, and both routes reach it through that place.

Driven at the ASGI layer through the real middleware stack, like
test_amp_ai_integration_consent.py. The provider is a recorder in place of
_ask_claude: what matters is whether it was CALLED, never what it said.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_external_model_consent.py
"""
import ast
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_copilot
import auth
import database
import http_security
import main
import models
import native_ai_routes
import plan_gate
import platform_routes
import saas_routes
import tenancy
from amp_ai import consent as C
from amp_ai.core import contracts
from amp_ai.core.contracts import CAPABILITY_EXTERNAL_MODEL, CAPABILITY_TELEMETRY_BASELINE
from database import Base

EXT = CAPABILITY_EXTERNAL_MODEL
LEARN = CAPABILITY_TELEMETRY_BASELINE
# The fixed half of every refusal note; the other half is the gate's own reason
# (never turned on / revoked by whom / a preview).
NOT_ASKED = "the hosted AI model was not asked"
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)
failures = []
CALLS = []          # every (system, user) prompt the "hosted provider" received


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ai_copilot is patched too: /ai/ask, /ai/report and /ai/status open their session
# through ITS `get_db`, which the ADR-0020 suites never needed.
PATCHED = (database, main, native_ai_routes, plan_gate, platform_routes, saas_routes, ai_copilot)
_real_session_factories = {}
HOSTED_ENV = ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AMP_LLM_BASE_URL", "AMP_LLM_MODEL")


def install():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    for mod in PATCHED:
        _real_session_factories.setdefault(mod, mod.SessionLocal)
        mod.SessionLocal = SessionLocal
    plan_gate._licence_cache.clear()


def uninstall():
    for mod, real in _real_session_factories.items():
        mod.SessionLocal = real
    _real_session_factories.clear()
    plan_gate._licence_cache.clear()


def clean_env():
    for k in HOSTED_ENV:
        os.environ.pop(k, None)


def hosted_env():
    clean_env()
    os.environ["AI_PROVIDER"] = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"


def local_env():
    clean_env()
    os.environ["AI_PROVIDER"] = "local"
    os.environ["AMP_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    os.environ["AMP_LLM_MODEL"] = "stub-model"


def recorder(system, user):
    CALLS.append({"system": system, "user": user})
    # No figure and no name: the grounding gate lets it through, so a consented
    # question is labelled source="llm" end to end.
    return "All machines look healthy."


def token(sub, role, tenant):
    claims = {"sub": sub, "role": role, "tenant": tenant, "exp": datetime.utcnow() + timedelta(hours=1)}
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
    # question about consent, not load, so each starts with a clean window.
    http_security._window.reset()
    return asyncio.run(_call(method, path, tok, headers, body))


def ask(tok, question, headers=None):
    """(status, body, provider calls made by THIS request)."""
    before = len(CALLS)
    code, body = call("POST", "/ai/ask", tok, headers=headers, body={"question": question})
    return code, body, len(CALLS) - before


def report(tok, headers=None):
    before = len(CALLS)
    code, body = call("POST", "/ai/report", tok, headers=headers, body={})
    return code, body, len(CALLS) - before


def grant(tok, granted, headers=None):
    return call("PUT", f"/ai-consent/{EXT}", tok, headers=headers, body={"granted": granted})


def seed():
    db = SessionLocal()
    tok0 = tenancy.set_current_tenant(None)
    db.add(models.Machine(tenant_code="TA", site="P1", name="PRESS-01", status="Running", utilization=70))
    db.add(models.Machine(tenant_code="TB", site="P1", name="LATHE-02", status="Running", utilization=60))
    db.add(models.Machine(tenant_code="DEFAULT", site="P1", name="CNC-01", status="Running", utilization=80))
    db.commit()
    tenancy.reset_current_tenant(tok0)
    db.close()


ADMIN_A = token("ta-admin", "Admin", "TA")
SUP_A = token("ta-sup", "Supervisor", "TA")
ADMIN_B = token("tb-admin", "Admin", "TB")
FOUNDER = token("founder", "Admin", "DEFAULT")
PREVIEW_TA = {"X-Tenant": "TA"}


# ----------------------------------------------------------------------------
def section_contract():
    print("=" * 74)
    print("1. THE CONTRACT: A CONSENT CAPABILITY, NOT A LEARNING ONE, AND ITS WORDING")
    print("=" * 74)
    check("external_model is a consent capability", EXT == "external_model" and EXT in contracts.CONSENT_CAPABILITIES)
    check("...and not a learning one: AMP fits nothing to what it sends", EXT not in contracts.LEARNING_CAPABILITIES)
    check("the learning capabilities are unchanged", contracts.LEARNING_CAPABILITIES == (LEARN,))
    check("the consent capabilities are the learning ones plus this one",
          contracts.CONSENT_CAPABILITIES == (LEARN, EXT), str(contracts.CONSENT_CAPABILITIES))
    check("it is the one SCOPED capability: a consent to it names the provider it is for (ADR-0038)",
          contracts.SCOPED_CAPABILITIES == (EXT,), str(contracts.SCOPED_CAPABILITIES))
    info = C.CAPABILITY_INFO.get(EXT, {})
    fields = ("title", "reads", "stored", "used_by", "without")
    check("the consent card wording has every field, none blank",
          all(isinstance(info.get(f), str) and info[f].strip() for f in fields), str(info)[:200])
    reads = info.get("reads", "").lower()
    check("...it names where the data goes", "anthropic" in reads and "gemini" in reads, reads[:200])
    check("...what leaves: the question, the draft and the evidence",
          "question" in reads and "draft" in reads and "evidence" in reads, reads[:200])
    check("...that a preview never sends it and a self-hosted model needs no consent",
          "preview" in reads and "self-hosted" in reads, reads[:300])
    stored = info.get("stored", "").lower()
    check("...that AMP keeps nothing and trains nothing on it", "not saved" in stored and "no model is trained" in stored,
          stored)
    check("...which endpoints use it", "/ai/ask" in info.get("used_by", "") and "/ai/report" in info.get("used_by", ""),
          info.get("used_by"))
    check("...and what happens without it: AMP's own engine",
          "own engine" in info.get("without", "").lower(), info.get("without"))

    install()
    db = SessionLocal()
    d = C.DbConsentGate().check(db, "TA", EXT)
    check("no row -> refused, naming the decision and where to make it",
          d.granted is False and info["title"] in d.reason and "AI consent" in d.reason, d.reason)
    d = C.DbConsentGate().check(db, "TA", "send_everything")
    check("an unknown capability is still refused", d.granted is False, d.reason)
    db.close()


# ----------------------------------------------------------------------------
def section_page():
    print()
    print("=" * 74)
    print("2. THE CONSENT PAGE: LISTED BESIDE LEARNING, GRANTED BY AN ADMIN, AUDITED")
    print("=" * 74)
    install()
    seed()
    clean_env()
    code, body = grant(ADMIN_A, True)
    check("with no hosted provider configured a grant is refused: nothing to consent to (ADR-0038)",
          code == 400 and "nothing to consent to" in str(body), f"{code} {body}")
    # A hosted provider is configured for the rest of the section: a scoped
    # consent is given FOR the provider configured at that moment.
    hosted_env()
    code, body = call("GET", "/ai-consent", ADMIN_A)
    caps = [c.get("capability") for c in body.get("capabilities", [])]
    check("Admin GET /ai-consent lists learning first, then the hosted model", code == 200 and caps == [LEARN, EXT],
          f"{code} {caps}")
    ext = {c.get("capability"): c for c in body.get("capabilities", [])}.get(EXT, {})
    check("...off, with title, what it reads, what is kept, who uses it and what happens without it",
          ext.get("granted") is False and all(ext.get(f) for f in ("title", "reads", "stored", "used_by", "without")),
          str(ext)[:300])

    code, body = grant(SUP_A, True)
    check("a Supervisor cannot grant it", code == 403, f"{code} {body}")
    code, body = grant(FOUNDER, True, headers=PREVIEW_TA)
    check("a founder previewing TA cannot grant it for TA", code == 403 and "preview" in str(body).lower(),
          f"{code} {body}")
    code, body = call("PUT", "/ai-consent/send_everything", ADMIN_A, body={"granted": True})
    check("an unknown capability is a 400", code == 400, f"{code} {body}")

    code, body = grant(ADMIN_A, True)
    ext = {c.get("capability"): c for c in body.get("capabilities", [])}.get(EXT, {})
    check("TA's Admin grants it -> on, naming the Admin", code == 200 and ext.get("granted") is True
          and ext.get("granted_by") == "ta-admin", f"{code} {str(ext)[:200]}")
    check("...given for the provider configured now, and active for it (ADR-0038)",
          ext.get("scoped") is True and ext.get("scope") == "anthropic" and ext.get("configured") == "anthropic"
          and ext.get("active") is True, str({k: ext.get(k) for k in ("scoped", "scope", "configured", "active")}))
    db = SessionLocal()
    audits = (db.query(models.AuditLog).filter(models.AuditLog.tenant_code == "TA",
                                               models.AuditLog.entity_type == C.AUDIT_ENTITY).all())
    check("...with one audit row in TA's trail, for this capability",
          len(audits) == 1 and audits[0].action == C.AUDIT_GRANTED and EXT in (audits[0].details or ""),
          str([(a.action, a.details) for a in audits]))
    check("...and the learning consent is untouched",
          C.DbConsentGate().check(db, "TA", LEARN).granted is False)
    db.close()
    code, body = grant(ADMIN_A, False)
    ext = {c.get("capability"): c for c in body.get("capabilities", [])}.get(EXT, {})
    check("...and can withdraw it", code == 200 and ext.get("granted") is False and ext.get("revoked_by") == "ta-admin",
          f"{code} {str(ext)[:200]}")
    clean_env()


# ----------------------------------------------------------------------------
def section_ask():
    print()
    print("=" * 74)
    print("3. /ai/ask: THE PROVIDER IS CALLED ONLY WITH THIS COMPANY'S CONSENT")
    print("=" * 74)
    install()
    seed()
    hosted_env()
    code, body, calls = ask(ADMIN_A, "status of PRESS-01")
    check("a hosted key is configured and TA has not consented: 200, answered", code == 200 and body.get("answer"),
          f"{code} {str(body)[:200]}")
    check("...the provider was NEVER called", calls == 0, str(calls))
    check("...the answer is AMP's own, about the machine asked for",
          body.get("source") == "rules" and "PRESS-01" in body.get("answer", "") and body.get("model") is None,
          f"{body.get('source')} {body.get('model')} {body.get('answer', '')[:120]}")
    note = body.get("note") or ""
    check("...and the note says why: the company's consent, and where to give it",
          "consent" in note.lower() and "AI consent" in note and "own engine" in note.lower(), note)

    code, body = call("PUT", f"/ai-consent/{LEARN}", ADMIN_A, body={"granted": True})
    code, body, calls = ask(ADMIN_A, "status of PRESS-01")
    check("a LEARNING consent is not consent to send data outside AMP: still not called",
          calls == 0 and body.get("source") == "rules", f"{calls} {body.get('source')}")

    grant(ADMIN_A, True)
    code, body, calls = ask(ADMIN_A, "status of PRESS-01")
    check("TA's Admin consented: the provider is called once", code == 200 and calls == 1, f"{code} {calls}")
    check("...and the answer is labelled as the model's, with no consent note",
          body.get("source") == "llm" and body.get("model") and "consent" not in (body.get("note") or "").lower(),
          f"{body.get('source')} {body.get('model')} {body.get('note')}")
    prompt = (CALLS[-1]["user"] if CALLS else "")
    check("...the prompt carried the question and AMP's evidence, never a tenant code",
          "status of PRESS-01" in prompt and "PRESS-01" in prompt and '"TA"' not in prompt, prompt[:200])
    code, body, calls = ask(SUP_A, "status of PRESS-01")
    check("the consent covers the company's own users of every role: a Supervisor's question is sent",
          calls == 1 and body.get("source") == "llm", f"{calls} {body.get('source')}")

    grant(ADMIN_A, False)
    code, body, calls = ask(ADMIN_A, "status of PRESS-01")
    check("withdrawn: the very next question is not sent (no cache)", calls == 0 and body.get("source") == "rules",
          f"{calls} {body.get('source')}")
    check("...and the note says it was withdrawn", "revoked" in (body.get("note") or "").lower(), body.get("note"))
    grant(ADMIN_A, True)
    code, body, calls = ask(ADMIN_A, "status of PRESS-01")
    check("granted again: sent again", calls == 1, str(calls))
    clean_env()


# ----------------------------------------------------------------------------
def section_other_company():
    print()
    print("=" * 74)
    print("4. ANOTHER COMPANY'S CONSENT NEVER COUNTS")
    print("=" * 74)
    hosted_env()
    # TA consented in section 3; TB has no row.
    code, body, calls = ask(ADMIN_B, "status of LATHE-02")
    check("TA's consent does not send TB's questions", code == 200 and calls == 0 and body.get("source") == "rules",
          f"{code} {calls} {body.get('source')}")
    check("...TB's answer is TB's own machine, from AMP's engine", "LATHE-02" in body.get("answer", ""),
          body.get("answer", "")[:120])
    grant(ADMIN_B, True)
    code, body, calls = ask(ADMIN_B, "status of LATHE-02")
    check("TB's own Admin consents -> TB's question is sent", calls == 1 and body.get("source") == "llm",
          f"{calls} {body.get('source')}")
    grant(ADMIN_B, False)
    clean_env()


# ----------------------------------------------------------------------------
def section_preview():
    print()
    print("=" * 74)
    print("5. A FOUNDER PREVIEW NEVER SENDS THE COMPANY'S DATA")
    print("=" * 74)
    hosted_env()
    code, body, calls = ask(FOUNDER, "status of PRESS-01", headers=PREVIEW_TA)
    check("TA consented; the founder previewing TA is answered", code == 200 and body.get("answer"), f"{code}")
    check("...without calling the provider", calls == 0 and body.get("source") == "rules",
          f"{calls} {body.get('source')}")
    check("...and the note says the preview is why", "previewing" in (body.get("note") or "").lower(), body.get("note"))

    code, body, calls = ask(FOUNDER, "status of CNC-01")
    check("the founder's own workspace has not consented: not sent either", calls == 0 and body.get("source") == "rules",
          f"{calls} {body.get('source')}")
    code, body = grant(FOUNDER, True)
    check("the founder's own workspace is its own company: its Admin may consent for it", code == 200, f"{code} {body}")
    code, body, calls = ask(FOUNDER, "status of CNC-01")
    check("...and then its own questions are sent", calls == 1 and body.get("source") == "llm",
          f"{calls} {body.get('source')}")
    code, body, calls = ask(FOUNDER, "status of PRESS-01", headers=PREVIEW_TA)
    check("with BOTH workspaces consented, previewing TA still sends nothing: neither consent covers AMP staff",
          calls == 0 and "previewing" in (body.get("note") or "").lower(), f"{calls} {body.get('note')}")
    grant(FOUNDER, False)
    clean_env()


# ----------------------------------------------------------------------------
def section_report():
    print()
    print("=" * 74)
    print("6. /ai/report: THE SAME GATE")
    print("=" * 74)
    hosted_env()
    grant(ADMIN_A, False)
    code, body, calls = report(ADMIN_A)
    check("TA withdrew: the report is composed by AMP, the provider not called",
          code == 200 and calls == 0 and body.get("source") == "rules" and body.get("report"),
          f"{code} {calls} {body.get('source')}")
    check("...and its note says why", NOT_ASKED in (body.get("note") or "") and "revoked" in (body.get("note") or ""),
          body.get("note"))
    grant(ADMIN_A, True)
    code, body, calls = report(ADMIN_A)
    check("TA consented: the report goes through the provider", code == 200 and calls >= 1, f"{code} {calls}")
    check("...and is labelled as the model's", body.get("source") == "llm" and body.get("model"),
          f"{body.get('source')} {body.get('model')} {body.get('note')}")
    code, body, calls = report(FOUNDER, headers=PREVIEW_TA)
    check("a founder previewing TA gets AMP's own report", calls == 0 and body.get("source") == "rules",
          f"{calls} {body.get('source')}")
    clean_env()


# ----------------------------------------------------------------------------
def section_status():
    print()
    print("=" * 74)
    print("7. /ai/status SAYS WHETHER THIS COMPANY'S QUESTIONS WILL REACH THE PROVIDER")
    print("=" * 74)
    hosted_env()
    grant(ADMIN_A, False)
    code, body = call("GET", "/ai/status", ADMIN_A)
    ext = body.get("external") or {}
    check("hosted key, TA withdrawn: enabled (a provider is configured) but consent false, with the reason",
          code == 200 and body.get("enabled") is True and body.get("provider") == "anthropic"
          and ext.get("provider") == "anthropic" and ext.get("consent") is False
          and NOT_ASKED in str(ext.get("reason")) and "revoked" in str(ext.get("reason")), f"{code} {body}")
    grant(ADMIN_A, True)
    code, body = call("GET", "/ai/status", ADMIN_A)
    ext = body.get("external") or {}
    check("TA consented: consent true, no reason", ext.get("consent") is True and ext.get("reason") is None, str(ext))
    code, body = call("GET", "/ai/status", FOUNDER, headers=PREVIEW_TA)
    ext = body.get("external") or {}
    check("the founder previewing TA: consent false, the preview named as the reason",
          ext.get("consent") is False and "previewing" in str(ext.get("reason")).lower(), str(ext))

    # Called directly, as the ADR-0020 and ADR-0023 suites call it: no request
    # session, and the decision is still read, through the module's own factory.
    direct = ai_copilot.ai_status(current_user={"tenant": "TB", "role": "Admin", "sub": "tb-admin"})
    ext = direct.get("external") or {}
    check("called directly without a session, the decision is still read (TB: not consented)",
          ext.get("provider") == "anthropic" and ext.get("consent") is False, str(ext))

    clean_env()
    code, body = call("GET", "/ai/status", ADMIN_A)
    ext = body.get("external") or {}
    check("no provider configured: nothing to consent to (consent null)",
          body.get("enabled") is False and ext == {"provider": None, "consent": None, "reason": None}, str(body))
    local_env()
    code, body = call("GET", "/ai/status", ADMIN_A)
    ext = body.get("external") or {}
    check("a self-hosted model configured: enabled, and nothing to consent to (nothing leaves AMP)",
          body.get("enabled") is True and body.get("provider") == "local"
          and ext == {"provider": None, "consent": None, "reason": None}, str(body))
    clean_env()


# ----------------------------------------------------------------------------
def section_self_hosted():
    print()
    print("=" * 74)
    print("8. THE SELF-HOSTED MODEL NEVER CONSULTS THE GATE; THE HOSTED ONE ASKS FOR THIS TENANT")
    print("=" * 74)
    from ai import llm_adoption
    asked = []
    original_check, original_adopted = C.DbConsentGate.check, llm_adoption.is_adopted

    def spy(self, db, tenant, capability, scope=None):
        asked.append((tenant, capability, scope))
        return original_check(self, db, tenant, capability, scope=scope)

    C.DbConsentGate.check = spy
    llm_adoption.is_adopted = lambda provider, model, path=None: (True, None)
    db = SessionLocal()
    user = {"tenant": "TC", "role": "Admin", "sub": "tc-admin"}
    try:
        local_env()
        llm, why = ai_copilot._copilot_llm(db, user)
        check("self-hosted and adopted, no consent row: the model is used", llm is not None and why is None, str(why))
        check("...and the consent gate was never consulted", asked == [], str(asked))
        hosted_env()
        llm, why = ai_copilot._copilot_llm(db, user)
        check("hosted, no consent row: not used, with the reason", llm is None and "consent" in str(why).lower(), str(why))
        check("...and the gate was asked for exactly this tenant, this capability and this provider (ADR-0038)",
              asked == [("TC", EXT, "anthropic")], str(asked))
    finally:
        C.DbConsentGate.check, llm_adoption.is_adopted = original_check, original_adopted
        db.close()
        clean_env()


# ----------------------------------------------------------------------------
def section_structure():
    print()
    print("=" * 74)
    print("9. ONE PLACE BUILDS THE MODEL, BEHIND THE GATE, AND BOTH ROUTES GO THROUGH IT")
    print("=" * 74)
    path = os.path.join(os.path.dirname(os.path.abspath(ai_copilot.__file__)), "ai_copilot.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls_in(fn, name):
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                target = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else None)
                if target == name:
                    out.append(n)
        return out

    builders = [name for name, fn in funcs.items() if calls_in(fn, "ProviderLLM")]
    check("ProviderLLM is built in exactly one function, _copilot_llm", builders == ["_copilot_llm"], str(builders))
    gate_calls = calls_in(funcs["_copilot_llm"], "external_model_allowed")
    check("...which asks external_model_allowed for the request's session, user and the provider about to be used",
          len(gate_calls) == 1
          and [getattr(a, "id", None) for a in gate_calls[0].args] == ["db", "current_user", "provider"],
          str([[getattr(a, "id", None) for a in c.args] for c in gate_calls]))
    for route in ("ai_ask", "ai_report"):
        c = calls_in(funcs[route], "_copilot_llm")
        check(f"{route} reaches the model only through _copilot_llm(db, current_user)",
              len(c) == 1 and [getattr(a, "id", None) for a in c[0].args] == ["db", "current_user"],
              str([[getattr(a, "id", None) for a in x.args] for x in c]))
    allowed = funcs["external_model_allowed"]
    check("external_model_allowed refuses a preview and reads the decision through DbConsentGate",
          bool(calls_in(allowed, "is_preview")) and bool(calls_in(allowed, "DbConsentGate")))


# ----------------------------------------------------------------------------
def section_scope():
    print()
    print("=" * 74)
    print("10. A CONSENT NAMES THE PROVIDER IT WAS GIVEN FOR, AND HOLDS ONLY FOR THAT ONE (ADR-0038)")
    print("=" * 74)
    install()
    seed()
    hosted_env()                                         # Anthropic
    original_gemini = ai_copilot._ask_gemini
    ai_copilot._ask_gemini = recorder                    # the same recorder, whichever provider is asked
    try:
        grant(ADMIN_A, True)
        code, body, calls = ask(ADMIN_A, "status of PRESS-01")
        check("granted while Anthropic is configured: the question is sent", calls == 1 and body.get("source") == "llm",
              f"{calls} {body.get('source')}")
        db = SessionLocal()
        d = C.DbConsentGate().check(db, "TA", EXT, scope="anthropic")
        check("the gate's decision says what it was given for", d.granted is True and d.scope == "anthropic", repr(d))
        d = C.DbConsentGate().check(db, "TA", EXT)
        check("asked with no provider, the gate refuses: nothing to apply it to", d.granted is False
              and "No hosted AI provider is configured" in d.reason, d.reason)
        db.close()

        # The operator switches providers -- or removes the Anthropic key with a
        # Gemini key present, and auto-detection falls through to Gemini.
        clean_env()
        os.environ["AI_PROVIDER"] = "gemini"
        os.environ["GEMINI_API_KEY"] = "g"
        code, body, calls = ask(ADMIN_A, "status of PRESS-01")
        check("Gemini configured: the consent given for Anthropic does not carry over -- not sent",
              code == 200 and calls == 0 and body.get("source") == "rules", f"{code} {calls} {body.get('source')}")
        note = body.get("note") or ""
        check("...and the note says what it was given for and what is configured now",
              "for anthropic" in note and "gemini is configured now" in note and "decides again" in note, note)
        code, st = call("GET", "/ai/status", ADMIN_A)
        ext = st.get("external") or {}
        check("/ai/status says the same", ext.get("provider") == "gemini" and ext.get("consent") is False
              and "for anthropic" in str(ext.get("reason")), str(ext))
        code, page = call("GET", "/ai-consent", ADMIN_A)
        ext = {c.get("capability"): c for c in page.get("capabilities", [])}.get(EXT, {})
        check("the page shows the grant as given for Anthropic, Gemini configured, NOT active",
              ext.get("granted") is True and ext.get("scope") == "anthropic" and ext.get("configured") == "gemini"
              and ext.get("active") is False, str({k: ext.get(k) for k in ("granted", "scope", "configured", "active")}))

        code, page = grant(ADMIN_A, True)
        ext = {c.get("capability"): c for c in page.get("capabilities", [])}.get(EXT, {})
        check("the Admin decides again, for Gemini: the grant now names Gemini and is active",
              code == 200 and ext.get("scope") == "gemini" and ext.get("active") is True, str(ext)[:200])
        code, body, calls = ask(ADMIN_A, "status of PRESS-01")
        check("...and the question is sent to Gemini", calls == 1 and body.get("source") == "llm",
              f"{calls} {body.get('source')}")
        db = SessionLocal()
        audits = (db.query(models.AuditLog).filter(models.AuditLog.tenant_code == "TA",
                                                   models.AuditLog.entity_type == C.AUDIT_ENTITY)
                  .order_by(models.AuditLog.id).all())
        last = json.loads(audits[-1].details or "{}") if audits else {}
        check("the audit record of that grant names the provider", last.get("scope") == "gemini" and last.get("new") is True,
              str(last))
        db.close()

        code, page = call("PUT", f"/ai-consent/{LEARN}", ADMIN_A, body={"granted": True})
        lrn = {c.get("capability"): c for c in page.get("capabilities", [])}.get(LEARN, {})
        check("a learning consent names no third party: not scoped, no scope, active when granted",
              code == 200 and lrn.get("scoped") is False and lrn.get("scope") is None and lrn.get("configured") is None
              and lrn.get("active") is True, str({k: lrn.get(k) for k in ("scoped", "scope", "configured", "active")}))
        db = SessionLocal()
        learn_audit = [json.loads(a.details or "{}") for a in
                       db.query(models.AuditLog).filter(models.AuditLog.tenant_code == "TA",
                                                        models.AuditLog.entity_type == C.AUDIT_ENTITY).all()
                       if LEARN in (a.details or "")]
        check("...and its audit record carries no scope key", learn_audit and all("scope" not in a for a in learn_audit),
              str(learn_audit))
        db.close()

        # No hosted provider at all: nothing to consent to, in advance or otherwise.
        clean_env()
        code, body = grant(ADMIN_A, True)
        check("no hosted provider configured: a grant is refused, and says so",
              code == 400 and "nothing to consent to" in str(body), f"{code} {body}")
        code, page = call("GET", "/ai-consent", ADMIN_A)
        ext = {c.get("capability"): c for c in page.get("capabilities", [])}.get(EXT, {})
        check("...the page shows the Gemini grant with nothing configured, not active",
              ext.get("granted") is True and ext.get("scope") == "gemini" and ext.get("configured") is None
              and ext.get("active") is False, str({k: ext.get(k) for k in ("granted", "scope", "configured", "active")}))
        db = SessionLocal()
        try:
            C.set_consent(db, "TA", EXT, True, "ta-admin")
            check("the writer itself refuses a scoped grant with no provider", False, "no ValueError")
        except ValueError as exc:
            check("the writer itself refuses a scoped grant with no provider", "nothing to consent to" in str(exc), str(exc))
        C.set_consent(db, "TA", LEARN, True, "ta-admin")
        check("...while a learning grant needs none", C.DbConsentGate().check(db, "TA", LEARN).granted is True)
        C.set_consent(db, "TA", EXT, False, "ta-admin")
        check("a revocation needs no provider either", C.DbConsentGate().check(db, "TA", EXT, scope="gemini").granted is False)

        # A row from before AMP recorded providers (scope NULL, granted): asked again.
        row = (db.query(models.AiLearningConsent)
               .filter(models.AiLearningConsent.tenant_code == "TA", models.AiLearningConsent.capability == EXT).first())
        row.granted, row.scope = True, None
        db.commit()
        db.close()
        hosted_env()
        code, body, calls = ask(ADMIN_A, "status of PRESS-01")
        check("a grant made before AMP recorded providers does not apply: not sent, asked again",
              calls == 0 and "before AMP recorded which provider" in (body.get("note") or ""), body.get("note"))
        grant(ADMIN_A, False)
    finally:
        ai_copilot._ask_gemini = original_gemini
        clean_env()


# ----------------------------------------------------------------------------
def main_():
    original_ask = ai_copilot._ask_claude
    ai_copilot._ask_claude = recorder
    saved_env = {k: os.environ.get(k) for k in HOSTED_ENV}
    try:
        section_contract()
        section_page()
        section_ask()
        section_other_company()
        section_preview()
        section_report()
        section_status()
        section_self_hosted()
        section_structure()
        section_scope()
    finally:
        ai_copilot._ask_claude = original_ask
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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


def test_external_model_consent():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
