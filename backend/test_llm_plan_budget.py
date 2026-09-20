"""A reasoning model must have room to name a tool, and a truncated plan must say so (ADR-0034).

AMP allowed a model 300 tokens to choose its tools. That was decided when the
only models here were scripted stubs, which emit their tool call as the first
token they produce. The first REAL model measured (qwen3:8b) spends 321-870
tokens thinking before it names anything, so at 300 it returned no tool call
AND no content -- Ollama strips thinking from `content` -- which is exactly the
shape of a model that had nothing to say.

The result: a model that routes 8/8 was being scored as one that routes 0/8, and
nothing in the suite could tell the difference. Both halves are pinned here:

  1. the budget is generous by default and configurable;
  2. a plan cut off at the ceiling is REPORTED as a budget exhaustion, not
     mistaken for a refusal.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_llm_plan_budget.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai import llm as L  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class FakeProvider:
    """A model that spends `thinks` tokens before answering, like a real one."""

    name = "fake"

    def __init__(self, budget_needed, calls=None, finish=None):
        self.budget_needed = budget_needed
        # `is None`, not `or`: an explicitly EMPTY call list is the case in
        # section 4 -- a model that answered and chose nothing -- and `or` would
        # quietly replace it with the default, testing the opposite thing.
        self.calls = [{"function": {"name": "get_oee", "arguments": {}}}] if calls is None else calls
        self.seen_max_tokens = None
        self._finish = finish

    def model(self):
        return "fake-reasoner"

    def ask(self, system, user):
        return "..."

    def chat(self, messages, tools=None, max_tokens=500):
        self.seen_max_tokens = max_tokens
        if max_tokens < self.budget_needed:
            # What Ollama actually returns: thinking consumed the budget, so no
            # call and no content, and only finish_reason tells you why.
            return {"content": "", "tool_calls": [], "finish_reason": "length"}
        return {"content": "", "tool_calls": self.calls,
                "finish_reason": self._finish or "tool_calls"}


TOOLS = [{"name": "get_oee", "description": "Plant OEE.", "parameters": {"type": "object", "properties": {}}}]


def main():
    print("=" * 74)
    print("A REASONING MODEL NEEDS ROOM TO NAME A TOOL (ADR-0034)")
    print("=" * 74)

    print("\n1. The default budget fits a small reasoning model")
    # Measured on qwen3:8b: 321-870 completion tokens to name a tool. A default
    # below that scores a working model as a broken one.
    check("the default planning budget is at least the 870 tokens measured",
          L.PLAN_TOKENS_DEFAULT >= 870, str(L.PLAN_TOKENS_DEFAULT))
    os.environ.pop("AMP_LLM_PLAN_TOKENS", None)
    check("...and plan_tokens() returns it when nothing is configured",
          L.plan_tokens() == L.PLAN_TOKENS_DEFAULT, str(L.plan_tokens()))

    print("\n2. A model that needs 870 tokens is not reported as unable to route")
    p = FakeProvider(budget_needed=870)
    errors = []
    plan = L.ProviderLLM(p, on_error=errors.append).plan("Why are we behind?", TOOLS)
    check("the model names its tool", [c["name"] for c in plan] == ["get_oee"], str(plan))
    check("...and AMP asked for the whole budget", p.seen_max_tokens == L.plan_tokens(),
          str(p.seen_max_tokens))
    check("...and nothing was reported as an error", not errors, str(errors))

    print("\n3. THE OLD CEILING: 300 tokens turns a working model into a silent one")
    os.environ["AMP_LLM_PLAN_TOKENS"] = "300"
    p2 = FakeProvider(budget_needed=870)
    errors2 = []
    plan2 = L.ProviderLLM(p2, on_error=errors2.append).plan("Why are we behind?", TOOLS)
    check("the plan comes back empty, exactly as it did before the fix",
          plan2 == [], str(plan2))
    # This is the half that matters. Empty-because-truncated and
    # empty-because-refused are different facts, and AMP could not tell them
    # apart -- so the reason on /ai/status blamed the model.
    check("...but AMP REPORTS it as a budget exhaustion, not a refusal",
          len(errors2) == 1 and "planning budget" in str(errors2[0]), str(errors2))
    check("...and the message says which knob to turn",
          "AMP_LLM_PLAN_TOKENS" in str(errors2[0]), str(errors2))

    print("\n4. An empty plan that is NOT truncated stays silent")
    # A model that genuinely declines must not be reported as out of room, or
    # the diagnosis is wrong in the other direction.
    os.environ.pop("AMP_LLM_PLAN_TOKENS", None)
    p3 = FakeProvider(budget_needed=0, calls=[], finish="stop")
    errors3 = []
    plan3 = L.ProviderLLM(p3, on_error=errors3.append).plan("Hello?", TOOLS)
    check("an empty plan with finish_reason=stop is not blamed on the budget",
          plan3 == [] and not errors3, f"plan={plan3} errors={errors3}")

    print("\n4b. A plan that named its tool and THEN hit the ceiling is a plan")
    # A reasoning model can emit its call and keep thinking until the cap:
    # finish_reason=length with a usable tool call. That is not a budget
    # failure -- AMP has what it needs -- and reporting it as one would make
    # /ai/status blame the budget on every successful call. The mutation
    # that dropped `not calls` from the condition survived until this case.
    p4 = FakeProvider(budget_needed=0, finish="length")
    errors4 = []
    plan4 = L.ProviderLLM(p4, on_error=errors4.append).plan("Why are we behind?", TOOLS)
    check("the tool call is kept", [c["name"] for c in plan4] == ["get_oee"], str(plan4))
    check("...and the cut-off after it is NOT reported as a budget exhaustion",
          not errors4, str(errors4))

    print("\n5. The budget is configurable, and nonsense falls back to the default")
    for value, expected in (("2000", 2000), ("0", L.PLAN_TOKENS_DEFAULT),
                            ("-5", L.PLAN_TOKENS_DEFAULT), ("banana", L.PLAN_TOKENS_DEFAULT),
                            ("", L.PLAN_TOKENS_DEFAULT)):
        os.environ["AMP_LLM_PLAN_TOKENS"] = value
        check(f"AMP_LLM_PLAN_TOKENS={value!r} -> {expected}", L.plan_tokens() == expected,
              str(L.plan_tokens()))
    os.environ.pop("AMP_LLM_PLAN_TOKENS", None)

    print("\n6. A prompt that cannot fit the window is declined at once, by name")
    # Measured on the first real runtime (ADR-0034 §9): handed evidence far
    # past the 16k window, Ollama silently truncated the prompt to 8,194
    # tokens, the model spent its whole wording budget thinking about the
    # fragment, and AMP fell back to its own sentence after 36 SECONDS. The
    # fallback was right; the wait and the silence were not. AMP now estimates
    # the prompt and declines before sending, and says why.
    os.environ.pop("AMP_LLM_CONTEXT_TOKENS", None)

    class Recording(FakeProvider):
        asked = 0

        def ask(self, system, user):
            self.asked += 1
            return "worded"

    p6 = Recording(budget_needed=0)
    errors6 = []
    llm6 = L.ProviderLLM(p6, on_error=errors6.append)
    huge = [{"id": f"F{i}", "label": f"Reading {i}", "value": i, "unit": "units",
             "provenance": "MEASURED FACT", "window": "last 7 days"} for i in range(2500)]
    try:
        llm6.phrase("How is the plant doing?", huge, "Plant OEE is 61.2%.")
        check("oversized evidence is declined", False, "no exception was raised")
    except RuntimeError as e:
        check("oversized evidence is declined with a RuntimeError",
              "context window" in str(e) and "16384" in str(e), str(e)[:160])
        check("...naming the knob that would change it", "AMP_LLM_CONTEXT_TOKENS" in str(e), str(e)[:160])
    check("...and the model was never asked", p6.asked == 0, str(p6.asked))
    check("...and /ai/status was told the sentence, not just the type",
          len(errors6) == 1 and "context window" in str(errors6[0]), str(errors6)[:160])

    # The same for planning: a catalogue that cannot fit is not sent.
    p6b = Recording(budget_needed=0)
    errors6b = []
    fat_tools = [{"name": f"tool_{i}", "description": "x" * 400, "parameters": {"type": "object", "properties": {}}}
                 for i in range(200)]
    try:
        L.ProviderLLM(p6b, on_error=errors6b.append).plan("Why are we behind?", fat_tools)
        check("an oversized catalogue is declined", False, "no exception was raised")
    except RuntimeError as e:
        check("an oversized catalogue is declined, naming the catalogue",
              "tool catalogue" in str(e) and "context window" in str(e), str(e)[:160])
    check("...and chat() was never called", p6b.seen_max_tokens is None, str(p6b.seen_max_tokens))

    # CONTROL: a prompt that fits goes through untouched.
    p6c = Recording(budget_needed=0)
    small = huge[:3]
    check("CONTROL: evidence that fits is sent, and worded",
          L.ProviderLLM(p6c).phrase("How is the plant doing?", small, "Plant OEE is 61.2%.") == "worded"
          and p6c.asked == 1, str(p6c.asked))

    # The window is configurable, and nonsense falls back to the measured default.
    for value, expected in (("40960", 40960), ("0", L.CONTEXT_TOKENS_DEFAULT), ("x", L.CONTEXT_TOKENS_DEFAULT)):
        os.environ["AMP_LLM_CONTEXT_TOKENS"] = value
        check(f"AMP_LLM_CONTEXT_TOKENS={value!r} -> {expected}", L.context_tokens() == expected,
              str(L.context_tokens()))
    os.environ.pop("AMP_LLM_CONTEXT_TOKENS", None)
    check("the estimate is conservative: never below chars/4",
          L.estimate_tokens("a" * 4000) >= 1000, str(L.estimate_tokens("a" * 4000)))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
