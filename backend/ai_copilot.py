"""
AI Factory Copilot — a natural-language assistant over live factory data.

It answers plant-floor questions ("why is my OEE low?", "what should I
reorder?"), does AI root-cause analysis, and generates management reports —
grounded in the company's real machines, downtime, OEE, shifts and inventory.

  OFF BY DEFAULT. Two providers, chosen by environment only:

    Anthropic (paid, commercial data terms — for real clients):
      ANTHROPIC_API_KEY = <key from the Claude Platform Console>
      AI_MODEL          = claude-haiku-4-5 (default; cheapest/fastest)

    Gemini (free tier via aistudio.google.com — DEMO USE ONLY; free-tier
    data may be used for training, so never route a paying customer's
    factory data through it):
      AI_PROVIDER    = gemini
      GEMINI_API_KEY = <key from Google AI Studio>
      GEMINI_MODEL   = gemini-2.5-flash (default)

  With both keys present, AI_PROVIDER decides; unset, Anthropic wins.
  Switching back for a real client is one variable: AI_PROVIDER=anthropic
  (or just delete AI_PROVIDER).

No code change is needed to connect; keys live only in the environment.
Both providers are called over plain REST via the standard library (no SDK
dependency), so the copilot never affects the deploy build.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from currency import money
from database import SessionLocal

import logging_config

log = logging_config.get_logger(__name__)

# Cheap + fast models by default; override with AI_MODEL / GEMINI_MODEL.
# Keys are never in code — only in the env.
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")


# ── The provider registry (AI roadmap, phase 1) ──────────────────────
#
# ONE TABLE, NOT FOUR IF/ELSE CHAINS. Provider choice, model name, "is it
# configured" and "make the call" used to branch on the same two strings in four
# separate places, so adding a third provider meant finding all four and getting
# each right. A local or AMP-native model is on the roadmap, and this is the seam
# it plugs into: one class, one registry entry.
#
# DELIBERATELY NOT AN ABC AND NOT A PLUGIN SYSTEM. Two providers do not justify
# machinery; what they justify is putting the per-provider facts in one place.
# Behaviour is unchanged — test_ai_copilot_fallback.test_provider_selection
# pinned the old precedence and passes untouched.


class AIProvider:
    """One way of asking a language model a question.

    `env_key`  the environment variable whose presence means "configured"
    `model()`  the model string to report and call
    `ask()`    system + user prompt in, text out; raises on failure
    """

    name = ""
    env_key = ""

    def is_configured(self) -> bool:
        return bool(os.environ.get(self.env_key))

    def model(self):
        raise NotImplementedError

    def ask(self, system: str, user: str) -> str:
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"

    def model(self):
        return AI_MODEL

    def ask(self, system, user):
        return _ask_claude(system, user)


class GeminiProvider(AIProvider):
    name = "gemini"
    env_key = "GEMINI_API_KEY"

    def model(self):
        return _GEMINI_DISCOVERED or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def ask(self, system, user):
        return _ask_gemini(system, user)


# Order is the AUTO-DETECT PRECEDENCE when AI_PROVIDER is unset: Anthropic first
# because it is the paid tier with commercial data terms, Gemini second because
# it is the free one. An explicit AI_PROVIDER always wins over this order.
PROVIDERS = (AnthropicProvider(), GeminiProvider())


def _resolve_provider():
    """The active provider OBJECT, or None when nothing is configured.

    ONE DELIBERATE BEHAVIOUR CHANGE, and the only one in the registry refactor:
    an AI_PROVIDER that names no registered provider now selects NOTHING. The
    old code tested `explicit in ("anthropic", "gemini")` and, on a typo, fell
    silently through to auto-detection — so `AI_PROVIDER=gemni`, set by someone
    deliberately moving OFF the paid tier, would quietly keep calling Anthropic
    and billing for it. An operator who names a provider has expressed an
    intention; the honest answers are "that one" or "none", never "a different
    one". Pinned in test_ai_provider_registry.
    """
    explicit = os.environ.get("AI_PROVIDER", "").strip().lower()
    if explicit:
        for p in PROVIDERS:
            if p.name == explicit:
                return p
        log.info("[AI COPILOT] AI_PROVIDER=%r matches no provider; copilot off "
                 "(known: %s)", explicit, ", ".join(p.name for p in PROVIDERS))
        return None
    for p in PROVIDERS:
        if p.is_configured():
            return p
    return None


def _provider():
    """Active LLM provider NAME, or None when no key is configured.
    Explicit AI_PROVIDER wins; otherwise auto-detect, Anthropic first."""
    p = _resolve_provider()
    return p.name if p else None


def _current_model():
    p = _resolve_provider()
    return p.model() if p else None


def _ai_enabled() -> bool:
    """The copilot is on only when the active provider's key is present."""
    p = _resolve_provider()
    return bool(p and p.is_configured())


def _build_factory_context(db: Session, tenant: str) -> str:
    """Compact, token-efficient snapshot of the factory for the model to reason over."""
    lines = []

    machines = db.query(models.Machine).all()
    if machines:
        lines.append("MACHINES:")
        for m in machines:
            lines.append(f"- {m.name}: {m.status}, utilization {m.utilization}%, downtime {m.downtime}")

    # Plant OEE from THE canonical contract (ADR-0014), over the same time window
    # the dashboard uses.
    #
    # This used to read `.order_by(id.desc()).limit(10)` — a window counted in
    # ROWS, not time — while the dashboard windowed by 7 days. The comment above
    # it claimed to use "the ONE definition every dashboard uses", and the
    # pooling function was indeed shared; the RECORD SET was not. Measured on one
    # factory at one moment: the dashboard said 100% and this said 10%. A
    # customer asking the assistant how the plant is doing got an answer ninety
    # points from the screen in front of them.
    import oee_contract
    plant = oee_contract.plant_oee(db, tenant)
    if plant["has_data"]:
        pct = oee_contract.as_percentages(plant)
        cov = plant["coverage"]
        line = (f"PLANT OEE ({plant['window']}, pooled): {pct['oee']}% "
                f"(availability {pct['availability']}%, performance "
                f"{pct['performance']}%, quality {pct['quality']}%)")
        # Coverage travels WITH the number, so the model cannot state a
        # whole-plant figure that was measured from part of the plant. A machine
        # whose gateway drops leaves the pool silently; measured, that made the
        # plant look 27 points better.
        if not cov["complete"]:
            line += (f" — measured from {cov['machines_reporting']} of "
                     f"{cov['machines_expected']} machines ({cov['coverage_pct']}% "
                     f"coverage); the rest reported nothing in this window")
        lines.append(line)
    elif plant["coverage"]["machines_expected"] > 0:
        # A factory exists but reported nothing in the window. Say so
        # explicitly — silence lets the model infer whatever it likes, and a 0%
        # would be a fabricated loss (ADR-0014).
        #
        # Gated on there BEING machines: a workspace with no factory at all must
        # fall through to the "No factory data available yet." placeholder, and
        # emitting a line here unconditionally made that message unreachable.
        lines.append(f"PLANT OEE ({plant['window']}): no production recorded — "
                     f"not zero, unmeasured")

    downs = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(8).all()
    if downs:
        lines.append("RECENT DOWNTIME:")
        for d in downs:
            lines.append(f"- {d.reason}: {d.duration}")

    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(5).all()
    if shifts:
        lines.append("RECENT SHIFTS (actual/target):")
        for s in shifts:
            lines.append(f"- {s.shift_name}: {s.actual_output}/{s.target_output}")

    # Low stock — tenant aware (GMATS uses its own 4-bucket inventory).
    if tenant == "GMATS":
        items = db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == "GMATS").all()
        # physical_stock / reserved_stock / reorder_level are Column(Integer,
        # default=0) WITHOUT nullable=False — any can be NULL, and `None - None` /
        # `None <= None` raised TypeError. As above, this runs outside the copilot's
        # try/except, so a NULL 500'd /ai/ask & /ai/report; exclude an item whose
        # availability or level can't be computed rather than crash the context.
        low = [i for i in items
               if i.physical_stock is not None and i.reserved_stock is not None
               and i.reorder_level is not None
               and (i.physical_stock - i.reserved_stock) <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low:
                lines.append(f"- {i.item_name}: available {i.physical_stock - i.reserved_stock} {i.unit} (reorder {i.reorder_level})")
    else:
        items = db.query(models.InventoryItem).all()
        # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
        # nullable=False, so either can be NULL (a raw-SQL / migration / cleared
        # write), and `None <= None` raised TypeError. _build_factory_context is
        # called OUTSIDE the /ai/ask & /ai/report try/except that answers from the
        # honest rules fallback, so that TypeError became an unhandled 500 — the
        # exact failure the fallback exists to prevent. A NULL level can't say
        # whether the item is low, so exclude it, matching generate_low_stock_
        # escalations (whose SQL `current_stock <= reorder_level` yields NULL, and
        # so excludes the row, when either side is NULL) and the recommendations
        # generator.
        low = [i for i in items
               if i.current_stock is not None and i.reorder_level is not None
               and i.current_stock <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low[:15]:
                lines.append(f"- {i.item_name}: {i.current_stock} {i.unit} (reorder {i.reorder_level})")

    # The raw tables above only cover machines / OEE / downtime / stock, so the
    # LLM answered "the data doesn't contain that" for cost, orders, quality,
    # maintenance, WIP and compliance questions. Compose the same read-models the
    # dashboard uses so the copilot can actually answer the domains it advertises.
    # Lazy imports avoid the import cycle (these pull in the pillar modules);
    # best-effort so a hiccup in one summary can't blank the whole context.
    try:
        from ai.production import build_production_summary
        from ai.cost import build_cost_summary
        from ai.delivery import build_delivery_summary
        from ai.quality import build_quality_summary
        from ai.maintenance import build_maintenance_summary
        from ai.flow import build_flow_summary
        from ai.compliance import build_compliance_summary

        prod = build_production_summary(db, tenant)
        if prod["runs"]:
            lines.append(f"PRODUCTION (7d): {prod['good']:,} good of {prod['total']:,} units "
                         f"({prod['good_rate']}% good) over {prod['runs']} runs.")
        cost = build_cost_summary(db, tenant)
        if cost["has_data"]:
            worst = cost["by_machine"][0]["name"] if cost["by_machine"] else "-"
            lines.append(f"COST OF LOSSES (7d): {money(cost['loss_cost'])} total "
                         f"(downtime {money(cost['downtime_cost'])}, scrap {money(cost['scrap_cost'])}); "
                         f"costliest machine {worst}.")
        deliv = build_delivery_summary(db, tenant)
        if deliv["total"]:
            lines.append(f"ORDERS/DELIVERY: {deliv['total']} orders, "
                         f"{deliv['fulfillment_rate']}% fulfilled by units, "
                         f"{deliv['late']} late, {deliv['at_risk']} at risk.")
        qual = build_quality_summary(db, tenant)
        if qual["inspections"]:
            defect = qual["top_defects"][0]["category"] if qual["top_defects"] else "-"
            lines.append(f"QUALITY (7d): first-pass yield {qual['first_pass_yield']}%, "
                         f"fail rate {qual['fail_rate']}%, top defect {defect}.")
        maint = build_maintenance_summary(db, tenant)
        if maint["open"]:
            lines.append(f"MAINTENANCE: {maint['open']} open task(s), {maint['overdue']} overdue, "
                         f"{maint['pending_approval']} awaiting approval.")
        flow = build_flow_summary(db, tenant)
        if flow["total"]:
            lines.append(f"WORK ORDERS: {flow['wip']} in progress, {flow['finished']} finished "
                         f"({flow['total']} total).")
        comp = build_compliance_summary(db, tenant)
        if comp["total"]:
            lines.append(f"COMPLIANCE: {comp['total']} controlled documents, "
                         f"{comp['overdue']} review(s) overdue.")
    except Exception as e:  # pragma: no cover - defensive; context must never 500 the copilot
        log.info(f"[AI COPILOT] context enrichment skipped: {e}")

    return "\n".join(lines) if lines else "No factory data available yet."


def _ask_claude(system: str, user: str) -> str:
    """Single call to the Anthropic Messages REST API using only the standard
    library — no SDK dependency, so deploys never break on it."""
    import json
    import urllib.error
    import urllib.request

    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 1500,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Anthropic API {e.code}: {detail[:300]}")
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


# Google retires model names over time (a fresh key 404'd on the shipped
# default with "no longer available to new users"). Instead of chasing names,
# discover what THIS key can use and remember it for the process lifetime.
_GEMINI_DISCOVERED = None


def _gemini_generate(model: str, system: str, user: str) -> str:
    """One generateContent call. Key goes in a header, never the URL, so it
    can't leak into request logs."""
    import json
    import urllib.error
    import urllib.request

    body = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": 1500},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={
            "x-goog-api-key": os.environ.get("GEMINI_API_KEY", ""),
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Gemini API {e.code}: {detail[:300]}")
    candidates = data.get("candidates") or [{}]
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _pick_flash_models(models: list) -> list:
    """From a ListModels payload, generateContent-capable flash-family TEXT
    models this key can use, best (newest stable) first. Specialised variants
    (image/tts/live/embedding) are skipped, and so are preview/experimental
    names — those often carry zero free-tier quota. Pure, for testability."""
    names = []
    for m in models or []:
        name = (m.get("name") or "").split("/")[-1]
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        if "flash" not in name:
            continue
        if any(x in name for x in ("image", "tts", "live", "embedding", "audio",
                                   "thinking", "preview", "exp")):
            continue
        names.append(name)
    return sorted(set(names), reverse=True)


def _gemini_discover_models() -> list:
    """Ask the Gemini ListModels API which models this key actually has."""
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _pick_flash_models(data.get("models"))


def _ask_gemini(system: str, user: str) -> str:
    """generateContent with self-healing model choice: when the configured
    model is retired (404) or out of free-tier quota (429 — quotas are per
    model, so a sibling flash model may still have allowance), walk the
    discovered candidates best-first and cache the first that answers."""
    global _GEMINI_DISCOVERED
    model = _GEMINI_DISCOVERED or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        return _gemini_generate(model, system, user)
    except RuntimeError as e:
        if not any(code in str(e) for code in ("404", "429")):
            raise
        last = e
        for candidate in _gemini_discover_models()[:4]:
            if candidate == model:
                continue
            try:
                result = _gemini_generate(candidate, system, user)
            except RuntimeError as retry_err:
                last = retry_err
                continue
            _GEMINI_DISCOVERED = candidate
            log.info(f"[AI COPILOT] Gemini model '{model}' unusable; discovered and using '{candidate}'")
            return result
        raise last


# Last LLM failure, surfaced (founder-only) in /ai/status so "why is the
# copilot answering from rules?" is answerable from the app, not Railway logs.
_LAST_LLM_ERROR = None


def _ask_llm(system: str, user: str) -> str:
    """Route one question to the active provider; remember the last failure."""
    global _LAST_LLM_ERROR
    try:
        provider = _resolve_provider()
        # No provider configured still calls Anthropic, exactly as before: the
        # caller has already checked _ai_enabled(), and the resulting auth error
        # is the honest failure rather than a silent None.
        result = (provider or PROVIDERS[0]).ask(system, user)
    except Exception as e:
        from datetime import datetime
        _LAST_LLM_ERROR = {"at": datetime.utcnow().isoformat(), "provider": _provider(),
                           "error": str(e)[:300]}
        raise
    _LAST_LLM_ERROR = None
    return result


router = APIRouter(prefix="/ai", tags=["AI Copilot"], dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/status")
def ai_status(current_user: dict = Depends(get_current_user)):
    """Lets the UI show 'connect to enable' vs the live copilot."""
    result = {"enabled": _ai_enabled(), "provider": _provider() if _ai_enabled() else None,
              "model": _current_model() if _ai_enabled() else None}
    # The last LLM failure is founder-only: error strings can carry
    # upstream details a client workspace shouldn't see.
    if current_user.get("tenant", "DEFAULT") == "DEFAULT":
        result["last_error"] = _LAST_LLM_ERROR
    return result


@router.post("/ask")
def ai_ask(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY to enable.")
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Ask a question.")
    tenant = current_user.get("tenant", "DEFAULT")
    context = _build_factory_context(db, tenant)
    system = (
        "You are AMP Copilot, a no-nonsense assistant for a factory manager at an Indian SME "
        "manufacturer. Answer using ONLY the factory data provided. If the data doesn't contain the "
        "answer, say so plainly. Be concise and practical — give shop-floor advice a supervisor can act on. "
        "When asked 'why', do a short root-cause analysis from the data."
    )
    try:
        answer = _ask_llm(system, f"Factory data:\n{context}\n\nQuestion: {question}")
    except Exception as e:
        # Graceful degradation: an LLM failure (no credits, rate limit,
        # outage) must never surface a raw API error in a customer's
        # copilot. Answer from the rule-based assistant instead, honestly
        # labelled — the factory data is all local, so this always works.
        log.info(f"[AI COPILOT] LLM failed, answering from rules: {e}")
        import ai
        fallback = ai.assistant.answer(db, tenant, question)
        return {
            "answer": fallback.get("answer", "I couldn't reach the AI model just now — try again shortly."),
            "view": fallback.get("view"),
            "model": None,
            "source": "rules",
            "note": "AI model temporarily unavailable — answered from live factory data.",
        }
    # The drill-in view is AMP's, not the model's. The rules fallback above has
    # always returned one and the UI renders "Open <view> ->" from it
    # (AICopilot.tsx), so without this the button disappeared exactly when a key
    # was configured: turning AI ON took a feature away. `route_view` reads the
    # same routing table with NO queries — measured, running the pillar instead
    # would cost up to 116% of this endpoint's context build (ai/assistant.py).
    import ai
    return {"answer": answer, "view": ai.assistant.route_view(question),
            "model": _current_model(), "source": "llm"}


@router.post("/report")
def ai_report(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY to enable.")
    tenant = current_user.get("tenant", "DEFAULT")
    context = _build_factory_context(db, tenant)
    system = (
        "You are AMP Copilot. Write a brief daily management report for a factory manager from the data. "
        "Use short sections with these headings: Summary, Machine status, Key issues, Recommended actions. "
        "Be specific and concise — no fluff."
    )
    try:
        report = _ask_llm(system, f"Factory data:\n{context}\n\nWrite today's report.")
    except Exception as e:
        # Same graceful degradation as /ai/ask: fall back to the
        # rule-composed weekly report rather than erroring.
        log.info(f"[AI COPILOT] LLM failed, reporting from rules: {e}")
        import ai
        built = ai.report.build_weekly_report(db, tenant)
        return {
            "report": built.get("markdown") or built.get("report") or "Report unavailable right now.",
            "model": None,
            "source": "rules",
            "note": "AI model temporarily unavailable — composed from live factory data.",
        }
    return {"report": report, "model": _current_model(), "source": "llm"}
