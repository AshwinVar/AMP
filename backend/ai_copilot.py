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

  A KEY CONNECTS THE PLATFORM; IT DOES NOT SPEAK FOR A COMPANY (ADR-0037).
  Either hosted provider runs outside infrastructure AMP controls, so a
  company's question, AMP's draft answer and the evidence behind it go there
  only after an Admin OF THAT COMPANY has turned on "external_model" under
  AI consent (amp_ai.consent, the same stored, audited, revocable decision as
  learning consent). The decision is read from the database on every request
  at the ONE place a request's model is built, _copilot_llm(); without it, or
  for a founder previewing the company, /ai/ask and /ai/report answer from
  AMP's own engine and say why. The self-hosted model (ADR-0023) needs no
  consent: nothing leaves AMP.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import tenancy
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
    # Does factory data leave AMP's own infrastructure when this provider is
    # asked? True for the hosted APIs; False only for a model AMP runs itself.
    external = True

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


class LocalOpenAIProvider(AIProvider):
    """A self-hosted, open-weight model behind an OpenAI-compatible endpoint (ADR-0023).

    Ollama, the llama.cpp server and vLLM all serve `POST {base}/chat/completions`
    with tool calling, so one small class covers them. Configured by environment:

      AMP_LLM_BASE_URL   e.g. http://127.0.0.1:11434/v1. It must be infrastructure
                         AMP runs: pointing it at a third party's service makes
                         the "local" model an external one, and nothing here can
                         tell the difference.
      AMP_LLM_MODEL      e.g. qwen3:8b
      AMP_LLM_API_KEY    optional bearer token (vLLM --api-key)
      AMP_LLM_TIMEOUT    seconds per call, default 30

    Configured is not trusted. /ai/ask uses this model's wording only once the
    model has passed the Copilot evaluation and its record is committed
    (copilot_eval/adopted_models.json): a model is switched on when it has
    EARNED it (ADR-0020's rule, applied to language models).
    """

    name = "local"
    env_key = "AMP_LLM_BASE_URL"
    external = False

    @staticmethod
    def _base():
        return os.environ.get("AMP_LLM_BASE_URL", "").strip().rstrip("/")

    def is_configured(self) -> bool:
        base = self._base()
        # http(s) only: urllib would also open file:// and ftp://, and a mistyped
        # setting must not turn the copilot into a file reader.
        return base.lower().startswith(("http://", "https://")) and bool(self.model())

    def model(self):
        return os.environ.get("AMP_LLM_MODEL", "").strip() or None

    def chat(self, messages, tools=None, max_tokens=500):
        """One chat completion. Returns {"content", "tool_calls", "finish_reason"}.
        Raises RuntimeError on any transport, HTTP or shape failure; the message
        names the failure, never the response body (it can echo the prompt).

        `finish_reason` is carried out because "length" is a different fact from
        "the model had nothing to say", and AMP could not previously tell them
        apart. A reasoning model that thinks past the token budget returns no
        tool call and no content, which looked exactly like a refusal -- so a
        model that routes perfectly well was scored as one that cannot route.
        Measured on qwen3:8b: 0/4 questions routed at 300 tokens, 4/4 at 1200.
        """
        import json
        import urllib.error
        import urllib.request

        if not self.is_configured():
            raise RuntimeError("local model not configured")
        body = {"model": self.model(), "messages": messages, "temperature": 0,
                "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"content-type": "application/json"}
        key = os.environ.get("AMP_LLM_API_KEY", "").strip()
        if key:
            headers["authorization"] = f"Bearer {key}"
        try:
            timeout = float(os.environ.get("AMP_LLM_TIMEOUT", "30"))
        except ValueError:
            timeout = 30.0
        req = urllib.request.Request(self._base() + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"local model HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"local model unreachable ({type(e).__name__})")
        except ValueError:
            raise RuntimeError("local model returned something that is not JSON")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("local model returned no message")
        if not isinstance(message, dict):
            raise RuntimeError("local model returned no message")
        calls = message.get("tool_calls")
        try:
            finish = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            finish = None
        # Token counts, when the runtime reports them (OpenAI-compatible `usage`).
        # Kept on the provider so the orchestrator can log them per request
        # without the prompt ever being logged: counts are observability, the
        # prompt is customer data.
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self.last_usage = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                           if isinstance(usage.get(k), int)}
        return {"content": message.get("content") if isinstance(message.get("content"), str) else "",
                "tool_calls": calls if isinstance(calls, list) else [],
                "finish_reason": finish if isinstance(finish, str) else None,
                "usage": dict(self.last_usage)}

    def ask(self, system, user):
        out = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                        max_tokens=1500)
        return out["content"].strip()


# Order is the AUTO-DETECT PRECEDENCE when AI_PROVIDER is unset: Anthropic first
# because it is the paid tier with commercial data terms, Gemini second because
# it is the free one. An explicit AI_PROVIDER always wins over this order.
#
# The self-hosted model is LAST on purpose. Configuring it is not the same as
# trusting it: an operator switches to it with AI_PROVIDER=local, and even then
# its wording is used only after it has passed the Copilot evaluation (ADR-0023).
# Appending it leaves the precedence of the two hosted providers exactly as it was.
PROVIDERS = (AnthropicProvider(), GeminiProvider(), LocalOpenAIProvider())
LOCAL = PROVIDERS[2]


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


class AmpNativeProvider(AIProvider):
    """The AMP-native copilot intent model (ADR-0020). NOT an LLM, and NOT in PROVIDERS.

    It writes no prose: `ask()` refuses. What it does is PROPOSE which of the
    copilot's fixed pillars answers a question (`route()` -> RouteDecision), and
    `ai.assistant.answer(..., proposer=)` decides whether to use the proposal.
    It is kept out of PROVIDERS on purpose: that tuple is the LLM precedence
    list, and `_provider()` / `_ai_enabled()` mean "an LLM is configured" to
    every caller and to two pinned suites (test_ai_provider_registry,
    test_ai_copilot_fallback).

    SWITCHED ON ONLY WHEN EARNED. `is_configured()` is true only when ALL hold:
      * the artifact verifies against the hash pinned in
        amp_ai.copilot_intent.classifier (tampering -> off, never an exception);
      * its evaluated adoption gate says `adopted: true` (the committed v1 says
        false: it did not beat the keyword router on held-out questions);
      * AMP_NATIVE_COPILOT is not "off" (an operator's kill switch).
    The model sees a question string only: no session, tenant, user or role.
    """

    name = "amp-native"
    env_key = ""
    SWITCH = "AMP_NATIVE_COPILOT"

    @staticmethod
    def _load():
        from amp_ai.copilot_intent import classifier   # stdlib-only; cached after the first verified load
        return classifier.load()

    def switched_off(self) -> bool:
        return os.environ.get(self.SWITCH, "").strip().lower() == "off"

    def status(self) -> dict:
        """{available, adopted, version, reason, switched_off}. Never raises."""
        try:
            clf = self._load()
        except Exception as e:   # noqa: BLE001 - an unusable artifact is "unavailable", fail closed
            return {"available": False, "adopted": None, "version": None,
                    "reason": getattr(e, "reason", None) or str(e)[:300], "switched_off": self.switched_off()}
        return {"available": True, "adopted": clf.adopted is True, "version": clf.model_version,
                "reason": None, "switched_off": self.switched_off()}

    def is_configured(self) -> bool:
        if self.switched_off():
            return False
        st = self.status()
        return st["available"] is True and st["adopted"] is True

    def model(self):
        st = self.status()
        return f"amp-native-intent@{st['version']}" if st["available"] else None

    def ask(self, system, user):
        raise NotImplementedError("amp-native proposes a route; it does not write answers")

    def route(self, question):
        return self._load().route(question)


NATIVE = AmpNativeProvider()


def _answer_engine() -> str:
    """Who answers a copilot question: "llm", "amp-native" (an ADOPTED intent model) or "rules"."""
    if _ai_enabled():
        return "llm"
    if NATIVE.is_configured():
        return "amp-native"
    return "rules"


def native_proposer():
    """The proposer /copilot/ask passes to ai.assistant.answer, or None.

    Only when the engine IS amp-native: with an LLM configured the copilot's
    questions go to /ai/ask, and the model is used there (below) only for the
    drill-in view and the rules fallback.
    """
    return NATIVE.route if _answer_engine() == "amp-native" else None


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
            if cost["priced"] and cost["loss_cost"] is not None:
                lines.append(f"COST OF LOSSES (7d): {money(cost['loss_cost'])} total "
                             f"(downtime {money(cost['downtime_cost'])}, scrap {money(cost['scrap_cost'])}); "
                             f"biggest loss {worst}.")
            elif cost["lost_units"] is not None:
                # No unit value set: the losses exist in units only; never hand the
                # model a £ the tenant did not give us (ADR-0010).
                lines.append(f"LOSSES (7d): {cost['lost_units']:,} good units not made "
                             f"(downtime {cost['downtime_minutes']:,} min, scrap {cost['rejected_units']:,} units; "
                             f"no unit value set, so no money figure); biggest loss {worst}.")
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


def _record_llm_error(e):
    global _LAST_LLM_ERROR
    from datetime import datetime
    _LAST_LLM_ERROR = {"at": datetime.utcnow().isoformat(), "provider": _provider(), "error": str(e)[:300]}


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
        _record_llm_error(e)
        raise
    _LAST_LLM_ERROR = None
    return result


def hosted_provider():
    """The HOSTED provider a request would use (configured and external), or None.

    None when nothing is configured or the configured provider is the
    self-hosted one: then there is nothing a company could consent to."""
    p = _resolve_provider()
    return p if p is not None and p.is_configured() and p.external else None


def external_model_allowed(db, current_user, provider=None):
    """(may THIS request's company's data go to a hosted model, and if not, why).

    The company's own decision, read from ai_learning_consents on every call
    through amp_ai.consent.DbConsentGate (ADR-0037): no row, or a revoked one,
    is no, so a revocation bites on the very next question. The company is the
    request's effective tenant, as on every read-model route. A founder
    PREVIEWING the company from the platform workspace is refused before the
    row is read: the consent an Admin gave covers the company's own users'
    questions, not AMP staff's (ADR-0020's rule for the anomaly check).

    The consent is honoured only for the provider it was given for (ADR-0038):
    the gate is asked with the name of the provider about to be used, and a
    row given for another one -- or before AMP recorded which -- is a refusal
    that says so, until an Admin decides again."""
    from amp_ai import consent
    from amp_ai.core.contracts import CAPABILITY_EXTERNAL_MODEL
    provider = provider if provider is not None else hosted_provider()
    if provider is None:
        return False, ("Answered from live factory data by AMP's own engine; no hosted AI provider is "
                       "configured.")
    tenant = tenancy.request_tenant(current_user)
    if tenancy.is_preview(current_user):
        return False, (f"Answered from live factory data by AMP's own engine; the hosted AI model was not "
                       f"asked: you are previewing {tenant} from the platform workspace, and {tenant}'s "
                       "consent to send its data to a hosted model covers only its own users.")
    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)
    if not decision.granted:
        return False, ("Answered from live factory data by AMP's own engine; the hosted AI model was not "
                       f"asked: {decision.reason}")
    return True, None


def _copilot_llm(db, current_user):
    """(the model the Copilot may use for THIS request, or None, and why not).

    Three switches, and a model is used only when every one it meets is on:
      * configured: a provider is named or auto-detected (_resolve_provider);
      * a HOSTED provider (external = True) is used only with the company's
        consent to send its data outside AMP (external_model_allowed, ADR-0037);
      * the SELF-HOSTED one (external = False) needs no consent, because nothing
        leaves infrastructure AMP runs, and is used only once its exact model has
        passed the Copilot evaluation (ai/adopted_models.json, ADR-0023).
    Otherwise /ai/ask and /ai/report answer from AMP's own engine and say why."""
    from ai import llm_adoption
    from ai.llm import ProviderLLM
    provider = _resolve_provider()
    if provider is None or not provider.is_configured():
        return None, None
    if provider.external:
        allowed, why = external_model_allowed(db, current_user, provider)
        if not allowed:
            return None, why
    else:
        adopted, reason = llm_adoption.is_adopted(provider.name, provider.model())
        if not adopted:
            return None, reason
    return ProviderLLM(provider, ask=_ask_llm, on_error=_record_llm_error), None


def _external_status(db, current_user):
    """For /ai/status: whether the configured provider is hosted, and whether THIS
    request's company may use it. `consent` is None when no hosted provider is
    configured, so a client can tell "nothing to consent to" from "not consented"."""
    provider = _resolve_provider()
    if provider is None or not provider.is_configured() or not provider.external:
        return {"provider": None, "consent": None, "reason": None}
    own = None
    if not isinstance(db, Session):
        # Called directly, without a request session (the suites do): read the
        # decision through the module's own factory rather than skip it.
        own = db = SessionLocal()
    try:
        allowed, why = external_model_allowed(db, current_user, provider)
    finally:
        if own is not None:
            own.close()
    return {"provider": provider.name, "consent": allowed, "reason": why}


router = APIRouter(prefix="/ai", tags=["AI Copilot"], dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/status")
def ai_status(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets the UI show 'connect to enable' vs the live copilot."""
    result = {"enabled": _ai_enabled(), "provider": _provider() if _ai_enabled() else None,
              "model": _current_model() if _ai_enabled() else None}
    # ADR-0037: a hosted provider is used for THIS company only with its consent.
    # `enabled` keeps meaning "a provider is configured"; this says whether the
    # company's questions will reach it, and if not, why.
    result["external"] = _external_status(db, current_user)
    # ADR-0020: which engine answers, and the native model's verdict. The model
    # card (/ai/models/copilot_intent) carries the numbers; this is the switch.
    native = NATIVE.status()
    result["engine"] = _answer_engine()
    result["native"] = {"available": native["available"], "adopted": native["adopted"],
                        "version": native["version"]}
    # ADR-0023: the self-hosted model, whether it is configured and whether it has
    # passed the evaluation. The model name only: the endpoint address is
    # infrastructure, not something a workspace needs to see.
    from ai import llm_adoption
    adopted, why = llm_adoption.is_adopted(LOCAL.name, LOCAL.model()) if LOCAL.is_configured() else (False, None)
    result["local"] = {"configured": LOCAL.is_configured(), "model": LOCAL.model() if LOCAL.is_configured() else None,
                       "adopted": adopted, "reason": why}
    # The last LLM failure is founder-only: error strings can carry upstream
    # details a client workspace shouldn't see, and it is process-wide, so it may
    # come from any tenant's request. The founder, not the founder's workspace:
    # this used to show it to every login there (tenancy.is_founder).
    if tenancy.is_founder(current_user):
        result["last_error"] = _LAST_LLM_ERROR
    return result


@router.post("/ask")
def ai_ask(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """A plant question, answered through the typed-tool orchestrator (ADR-0022/0023).

    AMP plans (or, with a tool-calling model, the model may plan), AMP's tools
    read the data for THIS request's principal, and the model may word the answer
    only if its text passes the grounding gate. So the model never sees a tenant,
    never reads the database, and never puts a figure in front of the user that
    the evidence does not hold.

    What changed from the snapshot prompt this replaces:
      * the tenant is `request_tenant` (via Principal.from_user), as on every
        read-model route. It used to be the token's own claim, so a founder
        previewing a company got the company's rows priced at the founder's own
        unit value, and OEE filtered twice to nothing;
      * the answer carries its evidence, tools and data state, like /copilot/ask;
      * the drill-in view is still AMP's: it comes from the tools AMP ran.
    """
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY, "
                                                    "GEMINI_API_KEY or a self-hosted model (AMP_LLM_BASE_URL "
                                                    "and AMP_LLM_MODEL) to enable.")
    question = payload.get("question") if isinstance(payload, dict) else None
    question = question.strip() if isinstance(question, str) else ""
    if not question:
        raise HTTPException(status_code=400, detail="Ask a question.")
    from ai import orchestrator
    from ai.tools import Principal
    llm, not_used = _copilot_llm(db, current_user)
    # ADR-0035: the caller's own prior turns, reduced by the orchestrator to
    # questions and the calls AMP ran, so a follow-up can refer to the machine
    # the conversation named. Nothing in them can choose the principal.
    thread = payload.get("thread") if isinstance(payload, dict) else None
    # An ADOPTED native intent model may still propose the pillar AMP's plan uses
    # (ADR-0020); a model that plans for itself replaces that plan when it can.
    out = orchestrator.ask(db, Principal.from_user(current_user), question,
                           proposer=NATIVE.route if NATIVE.is_configured() else None, llm=llm,
                           thread=thread)
    worded = out["engine"] == "llm"
    out["source"] = "llm" if worded else "rules"
    out["model"] = out.get("model") if worded else None
    if not_used:
        out["note"] = not_used
    elif not worded:
        # The model failed, timed out, or its wording did not pass the gate: the
        # answer is AMP's own, from the same tools, and says so.
        out["note"] = "AI model's wording not used this time; answered from live factory data."
    return out


@router.post("/report")
def ai_report(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    if not _ai_enabled():
        raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY, "
                                                    "GEMINI_API_KEY or a self-hosted model (AMP_LLM_BASE_URL "
                                                    "and AMP_LLM_MODEL) to enable.")
    # THE REPORT GOES THROUGH THE SAME GATE AS EVERY OTHER ANSWER (ADR-0034).
    # This endpoint used to hand a raw text snapshot of the factory to whatever
    # model was configured and return the model's prose as the report, unchecked
    # -- the one Copilot path where a model could state a figure nobody measured.
    # It now asks the orchestrator for the Daily Brief (ADR-0028): AMP's tools
    # produce the evidence, the model may word it, and the grounding gate decides
    # whether that wording is shown. A wording the gate refuses is replaced by
    # AMP's own sentence and the response says so.
    # The request's effective tenant, as on every read-model route and as the
    # orchestrator path below already does through Principal.from_user. This
    # read the token's own claim, so when the orchestrator raised and the
    # report fell back to the rules, a founder previewing a company got that
    # company's rows priced at the founder's own unit value and the explicitly
    # filtered sections (escalations, maintenance, outcomes) came back empty --
    # the defect /ai/ask's docstring records as fixed, missed on this branch.
    tenant = tenancy.request_tenant(current_user)
    from ai import orchestrator
    from ai.tools import Principal
    llm, not_used = _copilot_llm(db, current_user)
    try:
        out = orchestrator.ask(db, Principal.from_user(current_user), "Give me my morning briefing.", llm=llm)
    except Exception as e:   # noqa: BLE001 - the report must exist even when the copilot path does not
        log.info("[AI COPILOT] report path failed, composing from rules: %s", type(e).__name__)
        import ai
        built = ai.report.build_weekly_report(db, tenant)
        return {
            "report": built.get("markdown") or built.get("report") or "Report unavailable right now.",
            "model": None,
            "source": "rules",
            "note": "AI model temporarily unavailable — composed from live factory data.",
        }
    result = {"report": out["answer"], "model": out.get("model"), "source": out.get("engine") or "rules"}
    if not_used:
        result["note"] = not_used
    elif result["source"] != "llm":
        result["note"] = "AI model's wording not used this time; composed from live factory data."
    return result
