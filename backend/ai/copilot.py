"""Copilot — natural-language assistant over live factory data (ADR-0003).

Wraps the existing copilot engine (``ai_copilot``) behind the AI platform, so it
sits alongside Prediction and Recommendations under ``ai.*`` — the same
"platform wraps engine" pattern as ``ai.prediction`` over ``predictive_engine``.
Behaviour is unchanged: off until ``ANTHROPIC_API_KEY`` is set, then it answers
plant-floor questions and writes reports grounded in the tenant's real data.
"""
import ai_copilot

name = "copilot"

# The model the copilot uses when enabled (see ai_copilot for the env switches).
MODEL = ai_copilot.AI_MODEL


def is_enabled() -> bool:
    """True when the copilot is connected (an API key is present)."""
    return ai_copilot._ai_enabled()


def register(app) -> None:
    """Expose the copilot's HTTP routes (/ai/status, /ai/ask, /ai/report).

    Delegates to the engine's registrar so the endpoints and behaviour are
    identical; the platform now owns Copilot's entry point, consistent with the
    other services.
    """
    ai_copilot.register(app)
