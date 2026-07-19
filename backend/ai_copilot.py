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

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import SessionLocal

# Cheap + fast models by default; override with AI_MODEL / GEMINI_MODEL.
# Keys are never in code — only in the env.
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")


def _provider():
    """Active LLM provider name, or None when no key is configured.
    Explicit AI_PROVIDER wins; otherwise auto-detect, Anthropic first."""
    explicit = os.environ.get("AI_PROVIDER", "").strip().lower()
    if explicit in ("anthropic", "gemini"):
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


def _current_model():
    p = _provider()
    if p == "anthropic":
        return AI_MODEL
    if p == "gemini":
        return _GEMINI_DISCOVERED or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    return None


def _ai_enabled() -> bool:
    """The copilot is on only when the active provider's key is present."""
    p = _provider()
    if p == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if p == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    return False


def _build_factory_context(db: Session, tenant: str) -> str:
    """Compact, token-efficient snapshot of the factory for the model to reason over."""
    lines = []

    machines = db.query(models.Machine).all()
    if machines:
        lines.append("MACHINES:")
        for m in machines:
            lines.append(f"- {m.name}: {m.status}, utilization {m.utilization}%, downtime {m.downtime}")

    recs = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(10).all()
    if recs:
        from analytics_engine import calculate_oee_from_record
        oees = [calculate_oee_from_record(r)["oee"] for r in recs]
        lines.append(f"AVERAGE OEE (last {len(recs)} runs): {round(sum(oees) / len(oees))}%")

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
        low = [i for i in items if (i.physical_stock - i.reserved_stock) <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low:
                lines.append(f"- {i.item_name}: available {i.physical_stock - i.reserved_stock} {i.unit} (reorder {i.reorder_level})")
    else:
        items = db.query(models.InventoryItem).all()
        low = [i for i in items if i.current_stock <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low[:15]:
                lines.append(f"- {i.item_name}: {i.current_stock} {i.unit} (reorder {i.reorder_level})")

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
            print(f"[AI COPILOT] Gemini model '{model}' unusable; discovered and using '{candidate}'")
            return result
        raise last


# Last LLM failure, surfaced (founder-only) in /ai/status so "why is the
# copilot answering from rules?" is answerable from the app, not Railway logs.
_LAST_LLM_ERROR = None


def _ask_llm(system: str, user: str) -> str:
    """Route one question to the active provider; remember the last failure."""
    global _LAST_LLM_ERROR
    try:
        if _provider() == "gemini":
            result = _ask_gemini(system, user)
        else:
            result = _ask_claude(system, user)
    except Exception as e:
        from datetime import datetime
        _LAST_LLM_ERROR = {"at": datetime.utcnow().isoformat(), "provider": _provider(),
                           "error": str(e)[:300]}
        raise
    _LAST_LLM_ERROR = None
    return result


def register(app):
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    @app.get("/ai/status")
    def ai_status(current_user: dict = Depends(get_current_user)):
        """Lets the UI show 'connect to enable' vs the live copilot."""
        result = {"enabled": _ai_enabled(), "provider": _provider() if _ai_enabled() else None,
                  "model": _current_model() if _ai_enabled() else None}
        # The last LLM failure is founder-only: error strings can carry
        # upstream details a client workspace shouldn't see.
        if current_user.get("tenant", "DEFAULT") == "DEFAULT":
            result["last_error"] = _LAST_LLM_ERROR
        return result

    @app.post("/ai/ask")
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
            print(f"[AI COPILOT] LLM failed, answering from rules: {e}")
            import ai
            fallback = ai.assistant.answer(db, tenant, question)
            return {
                "answer": fallback.get("answer", "I couldn't reach the AI model just now — try again shortly."),
                "view": fallback.get("view"),
                "model": None,
                "source": "rules",
                "note": "AI model temporarily unavailable — answered from live factory data.",
            }
        return {"answer": answer, "model": _current_model(), "source": "llm"}

    @app.post("/ai/report")
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
            print(f"[AI COPILOT] LLM failed, reporting from rules: {e}")
            import ai
            built = ai.report.build_weekly_report(db, tenant)
            return {
                "report": built.get("markdown") or built.get("report") or "Report unavailable right now.",
                "model": None,
                "source": "rules",
                "note": "AI model temporarily unavailable — composed from live factory data.",
            }
        return {"report": report, "model": _current_model(), "source": "llm"}
