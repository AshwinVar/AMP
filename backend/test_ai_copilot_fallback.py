"""LLM copilot graceful-degradation tests.

An LLM failure (no credits, rate limit, outage) must never surface a raw API
error in the copilot: /ai/ask falls back to the rule-based assistant and
/ai/report to the rule-composed weekly report, both honestly labelled
source="rules". Found live when the Anthropic account had no credits and the
copilot answered with the raw billing error.

Run:  python backend/test_ai_copilot_fallback.py     (exit 0 = pass)
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
import ai_copilot
import main


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _endpoint(path):
    return next(r for r in main.app.routes if getattr(r, "path", "") == path).endpoint


def _boom(system, user):
    raise RuntimeError("Anthropic API 400: credit balance is too low")


def test_ask_falls_back_to_rules():
    db = _fresh_session()
    db.add(models.Machine(name="CNC-01", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}

    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    original = ai_copilot._ask_claude
    ai_copilot._ask_claude = _boom
    try:
        out = _endpoint("/ai/ask")({"question": "status of CNC-01"}, db=db, current_user=founder)
    finally:
        ai_copilot._ask_claude = original
        del os.environ["ANTHROPIC_API_KEY"]

    assert out["source"] == "rules"
    assert "CNC-01" in out["answer"]
    assert "credit balance" not in out["answer"], "raw API error leaked to the user"
    assert out["note"]
    print("PASS /ai/ask falls back to the rule-based assistant")


def test_report_falls_back_to_rules():
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}

    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    original = ai_copilot._ask_claude
    ai_copilot._ask_claude = _boom
    try:
        out = _endpoint("/ai/report")(db=db, current_user=founder)
    finally:
        ai_copilot._ask_claude = original
        del os.environ["ANTHROPIC_API_KEY"]

    assert out["source"] == "rules"
    assert out["report"] and "credit balance" not in out["report"]
    print("PASS /ai/report falls back to the rule-composed report")


def test_llm_success_is_labelled():
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    original = ai_copilot._ask_claude
    ai_copilot._ask_claude = lambda system, user: "All machines look healthy."
    try:
        out = _endpoint("/ai/ask")({"question": "how are we doing?"}, db=db, current_user=founder)
    finally:
        ai_copilot._ask_claude = original
        del os.environ["ANTHROPIC_API_KEY"]
    assert out["source"] == "llm" and out["model"]
    assert out["answer"] == "All machines look healthy."
    print("PASS LLM success is labelled source=llm")


def _clean_env():
    for k in ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(k, None)


def test_provider_selection():
    """Explicit AI_PROVIDER wins; auto-detect prefers Anthropic; no key = off."""
    _clean_env()
    assert ai_copilot._provider() is None and not ai_copilot._ai_enabled()

    os.environ["GEMINI_API_KEY"] = "g"
    assert ai_copilot._provider() == "gemini" and ai_copilot._ai_enabled()
    assert ai_copilot._current_model().startswith("gemini")

    os.environ["ANTHROPIC_API_KEY"] = "a"
    assert ai_copilot._provider() == "anthropic"          # auto-detect prefers paid/commercial
    os.environ["AI_PROVIDER"] = "gemini"
    assert ai_copilot._provider() == "gemini"             # explicit choice wins
    # provider chosen but its key missing -> disabled, not silently the other one
    del os.environ["GEMINI_API_KEY"]
    assert not ai_copilot._ai_enabled()
    _clean_env()
    print("PASS provider selection")


def test_gemini_route_is_used_and_labelled():
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}
    _clean_env()
    os.environ["AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = "test-key"
    original = ai_copilot._ask_gemini
    ai_copilot._ask_gemini = lambda system, user: "Gemini says: all good."
    try:
        out = _endpoint("/ai/ask")({"question": "how are we doing?"}, db=db, current_user=founder)
    finally:
        ai_copilot._ask_gemini = original
        _clean_env()
    assert out["source"] == "llm"
    assert out["answer"] == "Gemini says: all good."
    assert out["model"].startswith("gemini")
    print("PASS gemini route is used and labelled")


def test_gemini_failure_falls_back_to_rules():
    db = _fresh_session()
    db.add(models.Machine(name="CNC-01", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}
    _clean_env()
    os.environ["AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = "test-key"
    original = ai_copilot._ask_gemini
    ai_copilot._ask_gemini = lambda s, u: (_ for _ in ()).throw(RuntimeError("Gemini API 429: quota exceeded"))
    try:
        out = _endpoint("/ai/ask")({"question": "status of CNC-01"}, db=db, current_user=founder)
    finally:
        ai_copilot._ask_gemini = original
        _clean_env()
    assert out["source"] == "rules" and "CNC-01" in out["answer"]
    assert "quota" not in out["answer"]
    print("PASS gemini failure falls back to rules")


def test_pick_flash_models():
    models = [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["embedContent"]},
    ]
    # newest stable first; preview/pro/image/non-text excluded
    assert ai_copilot._pick_flash_models(models) == ["gemini-3.0-flash", "gemini-2.5-flash"]
    assert ai_copilot._pick_flash_models([]) == []
    print("PASS flash-model picker")


def test_gemini_unusable_model_self_heals():
    """A 404 (retired) or 429 (zero-quota model) on the configured model walks
    the discovered candidates and caches the first that answers — the exact
    failures seen live with a fresh free-tier key."""
    _clean_env()
    os.environ["AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = "test-key"
    ai_copilot._GEMINI_DISCOVERED = None
    calls = []

    def fake_generate(model, system, user):
        calls.append(model)
        if model == "gemini-2.5-flash":
            raise RuntimeError("Gemini API 404: no longer available to new users")
        if model == "gemini-3.1-flash":
            raise RuntimeError("Gemini API 429: You exceeded your current quota")
        return "healed answer"

    orig_gen, orig_disc = ai_copilot._gemini_generate, ai_copilot._gemini_discover_models
    ai_copilot._gemini_generate = fake_generate
    ai_copilot._gemini_discover_models = lambda: ["gemini-3.1-flash", "gemini-3.0-flash"]
    try:
        out = ai_copilot._ask_gemini("sys", "hello")
        assert out == "healed answer"
        # walked: retired default -> quota-less candidate -> working candidate
        assert calls == ["gemini-2.5-flash", "gemini-3.1-flash", "gemini-3.0-flash"]
        # cached: the next call goes straight to the winner
        out2 = ai_copilot._ask_gemini("sys", "again")
        assert out2 == "healed answer" and calls[-1] == "gemini-3.0-flash" and len(calls) == 4
        assert ai_copilot._current_model() == "gemini-3.0-flash"
    finally:
        ai_copilot._gemini_generate = orig_gen
        ai_copilot._gemini_discover_models = orig_disc
        ai_copilot._GEMINI_DISCOVERED = None
        _clean_env()
    print("PASS unusable gemini model self-heals via candidate walk")


if __name__ == "__main__":
    test_ask_falls_back_to_rules()
    test_report_falls_back_to_rules()
    test_llm_success_is_labelled()
    test_provider_selection()
    test_gemini_route_is_used_and_labelled()
    test_gemini_failure_falls_back_to_rules()
    test_pick_flash_models()
    test_gemini_unusable_model_self_heals()
    print("ALL COPILOT FALLBACK TESTS PASSED")
