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


if __name__ == "__main__":
    test_ask_falls_back_to_rules()
    test_report_falls_back_to_rules()
    test_llm_success_is_labelled()
    print("ALL COPILOT FALLBACK TESTS PASSED")
