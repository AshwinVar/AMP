"""Adding an AI provider is one class and one registry entry (AI roadmap, phase 1).

WHY THIS EXISTS
---------------
Provider choice, model name, "is it configured?" and "make the call" each used to
branch on the same two strings — `_provider`, `_current_model`, `_ai_enabled` and
`_ask_llm` all had their own if/elif. Four places to find and four chances to get
one wrong. AMP's roadmap is to make the external model replaceable
infrastructure (a local model, then an AMP-native one), so the seam matters.

The refactor is behaviour-preserving: `test_ai_copilot_fallback.test_provider_selection`
pinned the old precedence and passes unchanged. What THIS suite adds is the
property that refactor was FOR — that a third provider plugs in without editing
any dispatch logic. Without it, the registry could quietly rot back into
branching and every existing test would stay green.

WHAT IS DELIBERATELY NOT TESTED
-------------------------------
No network call. `ask()` is exercised through a stub provider; the real
Anthropic and Gemini calls are covered by test_ai_copilot_fallback.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_provider_registry.py
"""
import os

import ai_copilot

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def clear_env():
    for k in ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "STUB_AI_KEY"):
        os.environ.pop(k, None)


class StubProvider(ai_copilot.AIProvider):
    """A third provider, added the way a LocalProvider would be."""
    name = "stub"
    env_key = "STUB_AI_KEY"
    asked = []

    def model(self):
        return "stub-model-v1"

    def ask(self, system, user):
        self.asked.append((system, user))
        return "answered by stub"


def main():
    original = ai_copilot.PROVIDERS
    try:
        print("=" * 74)
        print("1. THE SHIPPED PROVIDERS ARE DESCRIBED BY THE TABLE, NOT BY BRANCHES")
        print("=" * 74)
        names = [p.name for p in ai_copilot.PROVIDERS]
        check("both providers are registered", names == ["anthropic", "gemini"], str(names))
        check("...in auto-detect precedence, paid tier first",
              names[0] == "anthropic", str(names))
        check("each declares the env var that configures it",
              all(p.env_key for p in ai_copilot.PROVIDERS),
              str([p.env_key for p in ai_copilot.PROVIDERS]))

        print()
        print("=" * 74)
        print("2. A THIRD PROVIDER NEEDS NO CHANGE TO ANY DISPATCH FUNCTION")
        print("=" * 74)
        stub = StubProvider()
        ai_copilot.PROVIDERS = (stub,) + original

        clear_env()
        os.environ["STUB_AI_KEY"] = "x"
        check("auto-detect finds it with no code change",
              ai_copilot._provider() == "stub", str(ai_copilot._provider()))
        check("...and reports ITS model", ai_copilot._current_model() == "stub-model-v1",
              str(ai_copilot._current_model()))
        check("...and counts as enabled", ai_copilot._ai_enabled() is True,
              str(ai_copilot._ai_enabled()))

        stub.asked.clear()
        answer = ai_copilot._ask_llm("sys", "why is machine 4 down?")
        check("...and _ask_llm routes to it", answer == "answered by stub", repr(answer))
        check("...passing the prompt through unchanged",
              stub.asked == [("sys", "why is machine 4 down?")], str(stub.asked))

        print()
        print("=" * 74)
        print("3. EXPLICIT CHOICE STILL BEATS AUTO-DETECT")
        print("=" * 74)
        clear_env()
        os.environ["STUB_AI_KEY"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "y"
        check("auto-detect prefers the earlier registry entry",
              ai_copilot._provider() == "stub", str(ai_copilot._provider()))
        os.environ["AI_PROVIDER"] = "anthropic"
        check("an explicit AI_PROVIDER overrides the order",
              ai_copilot._provider() == "anthropic", str(ai_copilot._provider()))

        # The failure mode that matters: a typo must not silently fall through to
        # a provider the operator did not choose.
        os.environ["AI_PROVIDER"] = "antropic"           # deliberate typo
        check("AN UNKNOWN AI_PROVIDER SELECTS NOTHING, rather than guessing",
              ai_copilot._provider() is None, str(ai_copilot._provider()))
        check("...and reports the copilot as off",
              ai_copilot._ai_enabled() is False, str(ai_copilot._ai_enabled()))

        print()
        print("=" * 74)
        print("4. NOTHING CONFIGURED IS AN HONEST 'OFF'")
        print("=" * 74)
        # The SHIPPED registry, not the stubbed one: this section is about what a
        # real deployment with no keys does.
        ai_copilot.PROVIDERS = original
        clear_env()
        check("no keys -> no provider", ai_copilot._provider() is None,
              str(ai_copilot._provider()))
        check("no keys -> no model", ai_copilot._current_model() is None,
              str(ai_copilot._current_model()))
        check("no keys -> disabled", ai_copilot._ai_enabled() is False,
              str(ai_copilot._ai_enabled()))

    finally:
        ai_copilot.PROVIDERS = original
        clear_env()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
