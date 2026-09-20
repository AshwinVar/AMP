"""The AMP Copilot orchestrator: question -> plan -> AMP tools -> evidence -> answer (ADR-0022).

    question
      -> PLAN      which tools, with which arguments. AMP's own router always
                   plans; a language model, when one is configured and passed
                   in, may plan instead, from the catalogue of tools this
                   principal's role may call, and an unusable plan falls back
                   to AMP's.
      -> TOOLS     ai.tools.run_tool, one call per step. Authentication, role,
                   tenant, plan pack and argument checks happen THERE, for every
                   call, whoever planned it.
      -> EVIDENCE  the facts each tool returned, numbered F1..Fn, each with its
                   provenance (ai/evidence.py).
      -> ANSWER    AMP's deterministic sentence from the tools. A language
                   model may re-word it; its text is shown only if it passes the
                   grounding gate (ai/grounding.py), otherwise AMP's sentence is
                   shown and the rejection is recorded.

The model is optional at every step, and with none the Copilot is exactly the
rule copilot plus its evidence: the same routing (ai.assistant.route) and the
same sentences (ai.assistant.say_*). A model that errors, times out or returns
nonsense costs fluency, never the answer.

`llm` is duck-typed: `.name`, `.model`, `.plan(question, tools, thread=None) ->
[{"name", "arguments"}]` and `.phrase(question, facts, draft) -> str`. `thread`
is a follow-up's context (ADR-0035): the caller's prior questions and the calls
AMP ran for them, cleaned here before any planner sees them. The evaluation
harness drives scripted ones (copilot_eval); ai_copilot provides real ones.
"""
import json
import logging
import re
import time

from ai import assistant
from ai import evidence as ev
from ai import grounding
from ai.tools import Principal, catalog, run_tool

# One structured line per answered question (ADR-0034 §11): which engine
# answered, which model, which tools ran and how they ended, whether the gate
# passed, how long it took, and the token counts when the runtime reports them.
# NEVER the question, the answer, the evidence or the tenant: those are the
# customer's, and the access log already ties this line to its request.
log = logging.getLogger("amp.copilot")

MAX_QUESTION = 1000
MAX_TOOL_CALLS = 4
MAX_ANSWER = 2000

# Follow-up questions (ADR-0035). The client may send the turns of ITS OWN
# conversation -- each prior question and the tool calls AMP itself ran for
# it -- so that "and its downtime?" can mean the machine the last question
# named. Nothing is stored server-side; the thread is text the caller typed or
# was shown, and it can NAME things, never AUTHORIZE them: every tool the
# follow-up runs goes through run_tool for THIS principal, and a prior turn's
# results are not accepted at all.
MAX_THREAD_TURNS = 6      # the turns considered, from the most recent
MAX_THREAD_CALLS = 4      # a turn ran at most MAX_TOOL_CALLS
# A follow-up refers to a machine when it says so, with a pronoun or a
# demonstrative. "and what about the plant OEE?" says neither and routes on its
# own words: a conversation about CNC-01 must not turn every later question
# into a question about CNC-01.
_REFERENT_WORDS = ("it", "its", "it's", "that machine", "this machine", "the machine",
                   "same machine", "that one", "the same one")
_REFERENT_PUNCT = re.compile(r"[^a-z0-9' ]+")

# Each routed pillar's typed tool. `help` answers from static text and reads nothing.
# test_copilot_orchestrator.py checks this covers assistant.route_names() exactly,
# so a pillar added to the router cannot be missing a tool.
PILLAR_TOOL = {
    "inventory": "get_inventory_status", "delivery": "get_order_delivery",
    "cost": "get_financial_losses", "quality": "get_quality_summary",
    "maintenance": "get_maintenance_status", "compliance": "get_compliance_status",
    "downtime": "get_downtime", "machines": "get_machine_status",
    "production": "get_production", "flow": "get_work_order_status",
    "shift": "get_shift_attainment", "oee": "get_oee", "trend": "get_week_on_week",
    "briefing": "get_factory_summary", "help": None,
}


class Plan:
    __slots__ = ("calls", "matched", "labels", "planner", "resolved")

    def __init__(self, calls, matched, labels=None, planner="rules", resolved=None):
        self.calls, self.matched = list(calls), matched
        self.labels, self.planner = dict(labels or {}), planner
        self.resolved = resolved     # {"machine": name} when a follow-up's referent was filled (ADR-0035)


# Two tools answer questions no pillar owns: output against the PLAN (the rule
# copilot could only report output, never whether it met the target) and the
# downtime Pareto. They refine a pillar's answer rather than take over routing:
# each applies only when the router chose one of the pillars listed with it, so
# a question the router sent to delivery or shifts still goes there, and
# route_names() -- the allowlist the native intent model is pinned to -- is
# unchanged. Written before the evaluation's unseen questions were run, and not
# tuned against them.
_PLAN_PHRASES = ("target", "behind plan", "on plan", "against plan", "vs plan", "versus plan",
                 "plan attainment", "production plan", "schedule adherence", "hit plan", "hit the plan",
                 "met plan", "meet plan", "meet the plan", "are we behind", "falling behind",
                 "behind on production")
_PLAN_PILLARS = ("briefing", "production")
# A plan question that asks WHY goes to the Root-Cause Explorer (ADR-0025), which
# answers with the measured losses and what it cannot explain; one that asks
# WHETHER goes to the plan figures. Deliberately narrow: "why is my OEE low?"
# still reaches the OEE pillar, so /copilot/ask keeps answering the pinned
# questions exactly as the rule copilot does.
_WHY_WORDS = ("why", "explain", "what went wrong", "root cause", "reason we", "how come")
_SHIFT_WORDS = ("shift", "crew", "night", "evening")
_CAUSE_PHRASES = ("top causes", "main causes", "biggest causes", "causes of downtime", "downtime causes",
                  "reasons for downtime", "downtime reasons", "pareto", "top reasons", "main reasons")
_CAUSE_PILLARS = ("downtime", "machines", "briefing")
# The radar answers "what is ABOUT to go wrong" (ADR-0026). Applied only from the
# fallback pillar, so every question the router already answers keeps its answer.
_RISK_PHRASES = ("likely to", "about to", "going to go wrong", "worry about", "should i worry",
                 "risk radar", "what risks", "any risks", "coming up")
_RISK_PILLARS = ("briefing",)
# The written brief (ADR-0028) answers "tell me everything", where the briefing
# pillar answers "what needs attention". Applied only from the fallback pillar,
# and only for phrases that ask to be BRIEFED rather than for a figure, so every
# question the router already answers keeps its answer.
_BRIEF_PHRASES = ("brief me", "the brief", "a brief", "daily brief", "factory brief",
                  "daily update", "need to know today", "morning update", "read me in",
                  "catch me up", "bring me up to speed")
_BRIEF_PILLARS = ("briefing",)
# "Should you be telling me anything?" (ADR-0031). The restraint is the feature,
# so a question ABOUT the notifications gets the plan and its held-back list,
# not the notifications themselves.
_RAISE_PHRASES = ("should you be telling me", "would you alert", "sitting on anything",
                  "why did you not tell me", "why didn't you tell me", "what would you raise",
                  "anything i should know", "holding back", "worth interrupting")
_RAISE_PILLARS = ("briefing", "help")
# "Did it help?" (ADR-0029). Applied from the fallback pillar AND from `help`,
# because these questions reached nothing at all before: "did it help?" routed to
# the help text, and "did that work?" to the plant summary. Narrow phrases only —
# a question about whether a MACHINE is working still goes to the machine.
_OUTCOME_PHRASES = ("did it help", "did that help", "did it work", "did that work",
                    "did approving", "after we approved", "after the actions",
                    "recommendations working", "are the agents helping", "what happened after")
_OUTCOME_PILLARS = ("briefing", "help")
# What a shortage will STOP (ADR-0030), as against what is short. Applied from
# the inventory pillar and the fallback: "what should I reorder" keeps its
# answer, "what will running out cost us" now has one.
_SHORTAGE_PHRASES = ("what will the shortage", "shortage stop", "stock-out stopping",
                     "stockout stopping", "running out cost", "at risk from stock",
                     "stop production", "hold up production", "which orders are at risk from",
                     "what does the shortage")
_SHORTAGE_PILLARS = ("inventory", "briefing")


def clean_thread(thread) -> list:
    """The client's conversation, reduced to what a follow-up may use: the last
    MAX_THREAD_TURNS turns, each a question (a string, cut to MAX_QUESTION) and
    the calls AMP ran for it (a tool name and scalar arguments only). Anything
    else a turn carries -- an answer, evidence, a result, a role, a tenant, a
    claim -- is dropped here, so nothing downstream can be tempted by it.
    Never raises for the caller's input."""
    if not isinstance(thread, list):
        return []
    out = []
    for turn in thread:
        if not isinstance(turn, dict):
            continue
        question = turn.get("question")
        question = question.strip()[:MAX_QUESTION] if isinstance(question, str) else ""
        raw_calls = turn.get("calls")
        calls = []
        for c in (raw_calls if isinstance(raw_calls, list) else [])[:MAX_THREAD_CALLS]:
            if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
                continue
            args = c.get("arguments")
            args = ({str(k)[:40]: v for k, v in args.items()
                     if isinstance(k, str) and isinstance(v, (str, int, float, bool))}
                    if isinstance(args, dict) else {})
            calls.append({"tool": c["tool"][:80], "arguments": args})
        if question or calls:
            out.append({"question": question, "calls": calls})
    return out[-MAX_THREAD_TURNS:]


def _refers_to_a_machine(question) -> bool:
    """Whether the question points at a machine with a pronoun or a demonstrative
    rather than a name: "is it running?", "and its downtime?", "that machine"."""
    q = " " + _REFERENT_PUNCT.sub(" ", (question or "").lower()) + " "
    return any(f" {w} " in q for w in _REFERENT_WORDS)


def _referent(db, thread):
    """The machine a follow-up refers to: from the most recent prior turn that
    named one, either in a call AMP ran (get_machine_history's `machine`) or in
    the question itself. Looked up by name through the SCOPED machine list --
    the tenant is bound for the whole question (ask) -- so a name from another
    company's plant resolves to nothing, whoever typed it."""
    for turn in reversed(thread or []):
        for c in reversed(turn["calls"]):
            name = c["arguments"].get("machine") if c["tool"] == "get_machine_history" else None
            if isinstance(name, str) and name:
                m = assistant._machine_named(db, name)
                if m is not None:
                    return m
        m = assistant._machine_named(db, turn["question"]) if turn["question"] else None
        if m is not None:
            return m
    return None


def plan_rules(db, question, proposer=None, thread=None) -> Plan:
    """AMP's own plan, with a follow-up's referent filled in (ADR-0035): a
    question that points at a machine with a pronoun, names none itself, and
    follows a turn that named one is routed as if it named that machine. The
    name can only be one from this tenant's own machine list, and the plan
    says what it resolved."""
    resolved = None
    if thread and _refers_to_a_machine(question) and assistant._machine_named(db, question) is None:
        machine = _referent(db, thread)
        if machine is not None:
            question = f"{question} ({machine.name})"
            resolved = {"machine": machine.name}
    plan = _plan_rules(db, question, proposer)
    plan.resolved = resolved
    return plan


def _plan_rules(db, question, proposer=None) -> Plan:
    """AMP's own plan: the rule copilot's routing, mapped onto typed tools."""
    r = assistant.route(db, question, proposer=proposer)
    if r.kind == "machine":
        return Plan([("get_machine_history", {"machine": r.machine.name})], r.matched, r.labels)
    if r.kind == "find":
        return Plan([("find_record", {"query": r.term or ""})], r.matched, r.labels)
    q = f" {(question or '').lower()} "
    if (r.matched in _PLAN_PILLARS and any(p in q for p in _PLAN_PHRASES)
            and not any(w in q for w in _SHIFT_WORDS)):
        if any(w in q for w in _WHY_WORDS):
            return Plan([("explain_production_gap", {})], "production_gap", r.labels)
        return Plan([("get_production_vs_target", {})], "production_vs_target", r.labels)
    if r.matched in _CAUSE_PILLARS and any(p in q for p in _CAUSE_PHRASES):
        return Plan([("get_top_downtime_causes", {})], "downtime_causes", r.labels)
    if r.matched in _RISK_PILLARS and any(p in q for p in _RISK_PHRASES):
        return Plan([("get_production_risks", {})], "production_risks", r.labels)
    if r.matched in _BRIEF_PILLARS and any(p in q for p in _BRIEF_PHRASES):
        return Plan([("get_daily_brief", {})], "daily_brief", r.labels)
    if r.matched in _RAISE_PILLARS and any(p in q for p in _RAISE_PHRASES):
        return Plan([("get_what_to_raise", {})], "proactive", r.labels)
    if r.matched in _OUTCOME_PILLARS and any(p in q for p in _OUTCOME_PHRASES):
        return Plan([("get_action_outcomes", {})], "action_outcomes", r.labels)
    if r.matched in _SHORTAGE_PILLARS and any(p in q for p in _SHORTAGE_PHRASES):
        return Plan([("get_shortage_risk", {})], "shortage_impact", r.labels)
    name = PILLAR_TOOL.get(r.matched)
    return Plan([(name, {})] if name else [], r.matched, r.labels)


def _plan_with_llm(llm, question, principal, thread=None):
    """(Plan, None) from the model, or (None, why not). Only the SHAPE is
    checked here; whether each step may run is run_tool's decision. The thread
    (already cleaned: prior questions and the calls AMP ran, nothing else) is
    what the model may use to see what a follow-up refers to (ADR-0035)."""
    try:
        raw = llm.plan(question, catalog(principal), thread=thread or None)
    except Exception as e:   # noqa: BLE001 - a failing model falls back to AMP's plan
        return None, f"the language model could not plan ({type(e).__name__})"
    if not isinstance(raw, list):
        return None, "the language model returned no usable plan"
    calls, seen = [], set()
    for step in raw:
        if not isinstance(step, dict):
            continue
        name, args = step.get("name"), step.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {"__unparseable__": args[:40]}
        key = (name, json.dumps(args, sort_keys=True, default=str)) if isinstance(name, str) else None
        if key is None or key in seen:
            continue
        seen.add(key)
        calls.append((name, args))
    if not calls:
        return None, "the language model chose no tool"
    dropped = len(calls) - MAX_TOOL_CALLS
    plan = Plan(calls[:MAX_TOOL_CALLS], "llm_plan", planner="llm")
    return plan, (f"{dropped} further tool call(s) dropped" if dropped > 0 else None)


def _compose(results, plan) -> tuple:
    """AMP's deterministic answer from the tool results: their sentences, in order."""
    if not plan.calls:
        return assistant._help(None, None)
    texts = [r.summary for r in results if r.summary]
    view = next((r.view for r in results if not r.refused and r.view), None)
    return (" ".join(texts) or "I couldn't answer that from AMP's data."), view


def _overall_state(results) -> str:
    if not results:
        return ev.OK
    usable = [r for r in results if not r.refused]
    if not usable:
        return results[0].state
    if all(r.state == ev.OK for r in usable) and len(usable) == len(results):
        return ev.OK
    if len(usable) == 1 and len(results) == 1:
        return usable[0].state
    return ev.PARTIAL_DATA


def ask(db, principal: Principal, question, proposer=None, llm=None, thread=None) -> dict:
    """Answer one question for one authenticated principal. Never raises for the
    question's content or for a model's behaviour.

    The principal's tenant is bound for the WHOLE question, routing included, not
    only inside each tool. Routing reads the machine list to spot a named
    machine, and unbound that read saw every company's machines: tenant A asking
    "how is WELD-07?" got a different KIND of answer when some other company had
    a WELD-07 than when nobody did, which says the other company's machine
    exists. A request is bound by the middleware anyway; a background brief, a
    test or any future caller is bound here."""
    import tenancy
    if isinstance(principal, Principal) and principal.tenant and not tenancy.is_reserved_tenant_code(principal.tenant):
        token = tenancy.set_current_tenant(principal.tenant)
        try:
            return _ask(db, principal, question, proposer, llm, thread)
        finally:
            tenancy.reset_current_tenant(token)
    return _ask(db, principal, question, proposer, llm, thread)


def _ask(db, principal, question, proposer, llm, thread=None) -> dict:
    started = time.perf_counter()
    q = question.strip() if isinstance(question, str) else ""
    if len(q) > MAX_QUESTION:
        return {"question": q[:200], "answer": f"That question is too long for the Copilot; keep it under "
                f"{MAX_QUESTION:,} characters.", "view": None, "matched": "too_long", "engine": "rules",
                "state": ev.INVALID_ARGUMENTS, "plan": {"planner": "rules", "calls": []},
                "tools": [], "evidence": [], "grounding": None, "notes": [],
                "thread": {"turns": 0, "resolved": None},
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}

    notes = []
    # The caller's own prior turns, reduced to questions and the calls AMP ran
    # (ADR-0035). Nothing in them is trusted: they may help a planner see what a
    # follow-up refers to, and every tool still runs through run_tool below.
    thread = clean_thread(thread)
    rules = plan_rules(db, q, proposer, thread)
    plan = rules
    # A model without native tool calling words answers but does not plan
    # (`can_plan = False`, ai/llm.py): AMP's router plans, as it always did.
    if llm is not None and getattr(llm, "can_plan", True):
        llm_plan, why = _plan_with_llm(llm, q, principal, thread)
        if llm_plan is not None:
            plan = llm_plan
        if why:
            notes.append(why)

    results = [run_tool(db, principal, name, args) for name, args in plan.calls]
    # A model plan in which NOTHING could run (every step an unknown tool or bad
    # arguments) is replaced by AMP's plan: the user asked a question, and the
    # model misnaming a tool is not their problem. A refusal on role, licence or
    # a missing machine stands: that is the answer, whoever planned it.
    if plan.planner == "llm" and results and all(
            r.state in (ev.NOT_FOUND, ev.INVALID_ARGUMENTS) and r.tool not in _MACHINE_TOOLS for r in results):
        notes.append("the language model's tool choice could not run; AMP's own plan answered")
        plan = rules
        results = [run_tool(db, principal, name, args) for name, args in plan.calls]

    if plan.planner == "llm":
        results = [_unecho(r, args, q) for r, (_name, args) in zip(results, plan.calls)]

    evidence, tools, next_id = [], [], 1
    for r in results:
        d = r.to_dict(next_id)
        next_id += len(r.facts)
        evidence.extend(d["facts"])
        tools.append({"tool": d["tool"], "state": d["state"], "summary": d["summary"],
                      "notes": d["notes"], "elapsed_ms": d["elapsed_ms"]})
        notes.extend(n for n in d["notes"] if n not in notes)

    answer, view = _compose(results, plan)
    engine, gate, model = "rules", None, None
    if llm is not None and evidence:
        try:
            text = llm.phrase(q, evidence, answer)
        except Exception as e:   # noqa: BLE001 - a failing model keeps AMP's sentence
            text, gate = None, {"passed": False, "numbers_checked": 0,
                                "reasons": [f"the language model could not phrase ({type(e).__name__})"]}
        if isinstance(text, str) and text.strip():
            text = text.strip()
            if len(text) > MAX_ANSWER:
                gate = {"passed": False, "numbers_checked": 0, "reasons": ["the model's answer was too long"]}
            else:
                checked = grounding.check(text, evidence, q)
                gate = checked.to_dict()
                if checked.passed:
                    answer, engine, model = text, "llm", getattr(llm, "model", None)
        elif gate is None:
            gate = {"passed": False, "numbers_checked": 0, "reasons": ["the model returned no text"]}

    out = {"question": q, "answer": answer, "view": view, "matched": plan.matched, **plan.labels,
           "engine": engine, "model": model, "state": _overall_state(results),
           "plan": {"planner": plan.planner,
                    "calls": [{"tool": str(n)[:80], "arguments": _shown_args(plan, a, q)} for n, a in plan.calls]},
           "tools": tools, "evidence": evidence, "grounding": gate, "notes": notes,
           # What the follow-up machinery did with the caller's thread (ADR-0035):
           # how many prior turns were considered and, when AMP's planner filled
           # a pronoun with a machine, which one -- so the screen can say so.
           "thread": {"turns": len(thread), "resolved": plan.resolved if plan.planner == "rules" else None},
           "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    log.info("copilot answered", extra={"copilot": {
        "engine": engine, "planner": plan.planner, "thread_turns": len(thread),
        "provider": getattr(llm, "name", None) if llm is not None else None,
        "model": getattr(llm, "model", None) if llm is not None else None,
        "tools": [{"tool": t["tool"], "state": t["state"], "elapsed_ms": t["elapsed_ms"]} for t in tools],
        "gate_passed": None if gate is None else bool(gate.get("passed")),
        "gate_reasons": (gate or {}).get("reasons") or [],
        "state": out["state"], "elapsed_ms": out["elapsed_ms"],
        "usage": _usage_for_log(getattr(llm, "last_usage", None) if llm is not None else None)}})
    return out


def _usage_for_log(usage):
    """Token counts renamed for the log. The log redactor blanks any key with
    "token" in it (logging_config._SENSITIVE_KEY_PARTS), and rightly: a key
    called anything_token is a credential until proven otherwise. A COUNT of
    tokens is not, but the redactor cannot know that, and loosening it for one
    field is the wrong trade. So the counts are logged under names that carry no
    trigger word -- "prompt", "completion", "total" -- and the runtime's own
    OpenAI-standard names stay on the provider, where nothing logs them."""
    if not isinstance(usage, dict) or not usage:
        return None
    return {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens"),
            "total": usage.get("total_tokens")}


# A NOT FOUND from these is an answer about the user's own data ("no machine
# called X"), not a model misnaming a tool, so it is never swapped for AMP's plan.
_MACHINE_TOOLS = {"get_machine_history", "find_record"}


def _shown_args(plan, args, question):
    """The arguments a response may show. AMP's own plan: as planned. A model's:
    only values the user typed, since anything else is model-chosen text that no
    gate has checked (the same reason as _unecho)."""
    if not isinstance(args, dict):
        return {}
    if plan.planner != "llm":
        return dict(args)
    typed = (question or "").lower()
    return {str(k)[:40]: (v if isinstance(v, (int, float)) and not isinstance(v, bool)
                          or (isinstance(v, str) and v.strip().lower() in typed) else "(withheld: not in your question)")
            for k, v in list(args.items())[:6]}


def _unecho(result, args, question):
    """A refusal or an empty search QUOTES its argument ("no machine called X",
    "nothing matching X"). When a model chose that argument, the quote would put
    model-chosen text into AMP's own sentence, past the grounding gate: an
    argument like "WELD-07" (another factory's machine) or "call 555-0100 for
    support" would be read out as AMP's words. So the sentence is replaced unless
    every text argument is something the user typed."""
    texts = [v for v in (args or {}).values() if isinstance(v, str)] if isinstance(args, dict) else []
    typed = (question or "").lower()
    if not texts or all(t.strip().lower() in typed for t in texts):
        return result
    if result.refused or (result.state == ev.NO_DATA and result.tool == "find_record"):
        result.summary = "The Copilot's plan asked for a record this workspace doesn't have."
        result.facts = [f for f in result.facts if not isinstance(f.value, str)]
    return result
