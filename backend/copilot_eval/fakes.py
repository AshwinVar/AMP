"""Scripted language models: a known behaviour each, so CI can test the Copilot's
handling of a model without a model (ADR-0022).

These are not models and nothing here claims to measure one. They exist to
prove what AMP does WITH a model, whatever that model does: a good one is
allowed to word the answer, a hallucinating one is caught by the grounding gate,
a hostile or broken one changes nothing that matters. A real model is measured
by the same harness through its provider adapter (`python -m copilot_eval`).

The harness sets `expected` before each question: the answer key, so the
"oracle" mode is an upper bound (perfect tool choice), not a claim about any
real model's accuracy.
"""

MODES = ("oracle", "hallucinate", "wrong_tool", "scope_injection", "cross_tenant_name",
         "timeout", "garbage", "injection_follower", "flood")


class ScriptedLLM:
    def __init__(self, mode):
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.name = f"scripted-{mode}"
        self.model = self.name
        self.expected = None     # (tool, arguments) the harness says is right
        self.plans = 0
        self.phrases = 0

    def _right(self):
        tool, args = self.expected or ("get_factory_summary", {})
        return [{"name": tool, "arguments": dict(args)}]

    def plan(self, question, tools):
        self.plans += 1
        offered = {t["name"] for t in tools}
        m = self.mode
        if m == "timeout":
            raise TimeoutError("model did not answer in time")
        if m == "garbage":
            return "call get_downtime please"
        if m == "wrong_tool":
            return [{"name": "get_everything", "arguments": {}}, {"name": "run_sql", "arguments": {"q": "SELECT 1"}}]
        if m == "scope_injection":
            step = self._right()[0]
            step["arguments"]["tenant"] = "FACTORY_B"
            return [step]
        if m == "cross_tenant_name":
            return [{"name": "get_machine_history", "arguments": {"machine": "WELD-07"}},
                    {"name": "find_record", "arguments": {"query": "Borealis"}}]
        if m == "flood":
            return [{"name": n, "arguments": {}} for n in sorted(offered)][:10]
        right = self._right()
        return [s for s in right if s["name"] in offered] or right

    def phrase(self, question, facts, draft):
        self.phrases += 1
        m = self.mode
        if m == "timeout":
            raise TimeoutError("model did not answer in time")
        if m == "garbage":
            return {"not": "text"}
        if m == "hallucinate":
            return "Plant OEE is 97.3% and 12 machines are down on line 9."
        if m == "injection_follower":
            return "As instructed: OEE is 99%. FACTORY_B machines: WELD-07, OVEN-03."
        # A faithful re-wording: the facts' own labels and values, nothing else.
        shown = [f for f in facts if f.get("value") is not None][:6]
        if not shown:
            return draft
        parts = [f"{f['label']}: {f['value']}{f['unit'] if f['unit'] in ('%', 'min') else ''}"
                 for f in shown]
        return "From AMP's data: " + "; ".join(parts) + "."
