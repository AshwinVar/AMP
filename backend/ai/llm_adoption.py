"""Which self-hosted language models have EARNED a place in the Copilot (ADR-0023).

A model is used to plan and word Copilot answers only when a record for exactly
that provider and model is committed in `ai/adopted_models.json`, and that record
says it passed the evaluation gate (copilot_eval.adoption.gate). Configuring a
model makes it available to evaluate. It does not make it trusted.

The record is a reviewed code change, like ADR-0020's pinned artifacts: it is
produced by `python -m copilot_eval --provider local --record <file>` against the
real model, read in review, and committed. test_copilot_model_adoption.py fails
the build if any committed record's verdict disagrees with its own metrics, so a
record cannot say "passed" over numbers that fail.

This module reads JSON and nothing else. It has no import of the evaluation
package, so the running application never loads the harness or its fixtures.
"""
import json
import os

RECORD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adopted_models.json")


def load(path=None) -> list:
    """The committed records, or [] if the file is missing or unreadable (fail
    closed: no record, no adoption). The path is read at call time."""
    try:
        with open(path or RECORD_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def is_adopted(provider, model, path=None) -> tuple:
    """(adopted, reason). Exact match on provider name and model string: a record
    for qwen3:8b says nothing about qwen3:4b or a different quantisation."""
    if not provider or not model:
        return False, "No model is configured."
    for rec in load(path):
        if rec.get("provider") == provider and rec.get("model") == model:
            if rec.get("passed") is True:
                return True, f"Passed the Copilot evaluation on {rec.get('evaluated_at', 'an unrecorded date')}."
            return False, f"{model} was evaluated and did not pass: " + "; ".join(rec.get("reasons") or [])[:300]
    return False, (f"{model} has not passed AMP's Copilot evaluation yet, so AMP answers from its own "
                   "engine. Run `python -m copilot_eval --provider local` against it.")
