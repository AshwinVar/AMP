"""The Copilot orchestrator: AMP plans, AMP's tools decide, a model only asks (ADR-0022).

Pinned here:
  1. COVERAGE. Every routed pillar has a typed tool (PILLAR_TOOL == route_names),
     and every tool it names is registered.
  2. SAME ANSWER, NOW WITH EVIDENCE. /copilot/ask returns, for every routed
     question, exactly the rule copilot's answer, view and match, plus the
     evidence (facts numbered F1..Fn), the tools that ran and a data state.
  3. THE PLAN-VS-TARGET AND CAUSE QUESTIONS reach their tools, and only from the
     pillars they refine (a delivery or a shift question is left alone).
  4. INPUT LIMITS. An over-long question runs no tool; a non-string question is
     answered as an empty one, never a 500.
  5. A MODEL'S PLAN is shape-checked (deduplicated, JSON-string arguments parsed,
     capped at four calls), refused steps fall back to AMP's plan, and a
     refusal on role or a missing machine STANDS (it is the answer).
  6. A MODEL'S TEXT is shown only if it passes the gate; an over-long one never;
     the response says why in counts, not tokens.
  7. MODEL-CHOSEN TEXT never reaches AMP's sentence or the plan echo unless the
     user typed it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_orchestrator.py
"""
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import read_model_routes
import tenancy
from ai import assistant, orchestrator
from ai import evidence as ev
from ai.tools import REGISTRY, Principal, Tool, registry
from copilot_eval import fixtures as F
from database import Base

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    return Session


def ask(Session, principal, question, llm=None):
    db = Session()
    tok = tenancy.set_current_tenant(principal.tenant)
    try:
        return orchestrator.ask(db, principal, question, llm=llm)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def endpoint(Session, tenant, payload, role="Admin"):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return read_model_routes.copilot_ask(payload, db=db,
                                             current_user={"sub": "u", "role": role, "tenant": tenant})
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def rules_answer(Session, tenant, question):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return assistant.answer(db, tenant, question)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


class Stub:
    """A model with a fixed plan and a fixed wording."""
    def __init__(self, plan=None, text=None):
        self.name = self.model = "stub"
        self._plan, self._text = plan, text

    def plan(self, question, tools):
        return self._plan

    def phrase(self, question, facts, draft):
        return self._text


def main():
    Session = session()
    A = Principal(tenant=F.A, role="Admin", username="a")
    Bp = Principal(tenant=F.B, role="Admin", username="b")

    print("=" * 74)
    print("1. EVERY ROUTED PILLAR HAS A TOOL")
    print("=" * 74)
    check("PILLAR_TOOL covers route_names() exactly", set(orchestrator.PILLAR_TOOL) == set(assistant.route_names()),
          str(set(orchestrator.PILLAR_TOOL) ^ set(assistant.route_names())))
    missing = [t for t in orchestrator.PILLAR_TOOL.values() if t and t not in REGISTRY]
    check("every tool PILLAR_TOOL names is registered", not missing, str(missing))

    print()
    print("=" * 74)
    print("2. /copilot/ask: THE RULE COPILOT'S ANSWER, PLUS ITS EVIDENCE")
    print("=" * 74)
    questions = ["Why is my OEE low?", "Which machines are in breakdown?", "What should I reorder first?",
                 "Are any orders late?", "How much are losses costing us?", "How are we doing vs last week?",
                 "Summarise today's production.", "what's the downtime situation", "zzz qqq",
                 "How is CNC-01 doing?", "find WO-001", "what can I ask you?", "Is any maintenance overdue?"]
    for tenant in F.TENANTS:
        for q in questions:
            got = endpoint(Session, tenant, {"question": q})
            want = rules_answer(Session, tenant, q)
            same = all(got.get(k) == v for k, v in want.items())
            check(f"{tenant}: {q!r} == the rule copilot's answer", same,
                  f"{ {k: got.get(k) for k in want} } vs {want}")
    r = endpoint(Session, F.B, {"question": "what's the downtime situation"})
    ids = [f["id"] for f in r["evidence"]]
    check("evidence facts are numbered F1..Fn in order", ids == [f"F{i}" for i in range(1, len(ids) + 1)] and ids,
          str(ids))
    check("each fact carries a known provenance", all(f["provenance"] in ev.PROVENANCE for f in r["evidence"]))
    check("the tools that ran are listed with their state", r["tools"] and r["tools"][0]["tool"] == "get_downtime"
          and r["tools"][0]["state"] == ev.OK, str(r["tools"]))
    check("the response carries a data state and the plan", r["state"] == ev.OK and r["plan"]["planner"] == "rules")
    check("no model ran, so the engine is rules and there is no gate verdict",
          r["engine"] == "rules" and r["grounding"] is None and r["model"] is None)

    print()
    print("=" * 74)
    print("3. PLAN-VS-TARGET AND CAUSES: REFINING, NOT RE-ROUTING")
    print("=" * 74)
    for q, tool in [("Did we hit the production target?", "get_production_vs_target"),
                    ("Why are we behind?", "get_production_vs_target"),
                    ("Are we behind plan this week?", "get_production_vs_target"),
                    ("What are the top causes of downtime?", "get_top_downtime_causes"),
                    ("Show me the downtime pareto", "get_top_downtime_causes")]:
        r = ask(Session, Bp, q)
        check(f"{q!r} -> {tool}", [t["tool"] for t in r["tools"]] == [tool], str([t["tool"] for t in r["tools"]]))
    for q, tool in [("Did the night shift hit target?", "get_shift_attainment"),
                    ("Are customer orders behind schedule?", "get_order_delivery")]:
        r = ask(Session, Bp, q)
        check(f"{q!r} stays with {tool}", [t["tool"] for t in r["tools"]] == [tool], str([t["tool"] for t in r["tools"]]))
    r = ask(Session, Bp, "Why are we behind?")
    check("B: 'why are we behind' states the plan shortfall from the plan's own numbers",
          "1,300 of 2,500" in r["answer"] and "PLAN-B2" in r["answer"], r["answer"])

    # With NO ambient tenant (a background job, a direct caller), asking about a
    # machine only ANOTHER company has must read exactly like asking about one
    # nobody has: the routing step must not see other companies' machine lists.
    def unbound(principal, question):
        db = Session()
        try:
            return orchestrator.ask(db, principal, question)
        finally:
            db.close()
    others = unbound(A, "How is WELD-07 doing?")          # FACTORY_B's machine
    nobody = unbound(A, "How is ZETA-99 doing?")          # no company's machine
    check("unbound: another company's machine name routes like a name nobody has",
          [t["tool"] for t in others["tools"]] == [t["tool"] for t in nobody["tools"]]
          and others["matched"] == nobody["matched"], f"{others['matched']} vs {nobody['matched']}")
    check("...and the ambient tenant is left as it was", tenancy.current_tenant() is None,
          str(tenancy.current_tenant()))

    print()
    print("=" * 74)
    print("4. INPUT LIMITS")
    print("=" * 74)
    r = ask(Session, A, "x" * (orchestrator.MAX_QUESTION + 1))
    check("an over-long question runs no tool", r["tools"] == [] and r["state"] == ev.INVALID_ARGUMENTS, r["state"])
    for payload in ({"question": {"nested": 1}}, {"question": 42}, {"question": None}, {}):
        r = endpoint(Session, F.A, payload)
        check(f"payload {payload} is answered, not a 500", isinstance(r.get("answer"), str) and r["answer"])

    print()
    print("=" * 74)
    print("5. A MODEL'S PLAN")
    print("=" * 74)
    r = ask(Session, A, "downtime?", Stub(plan=[{"name": "get_downtime", "arguments": {}}] * 3))
    check("duplicate steps run once", [t["tool"] for t in r["tools"]] == ["get_downtime"], str(r["tools"]))
    r = ask(Session, A, "causes?", Stub(plan=[{"name": "get_top_downtime_causes", "arguments": '{"limit": 2}'}]))
    check("JSON-string arguments are parsed", r["tools"][0]["state"] == ev.OK and r["plan"]["calls"][0]["arguments"]
          == {"limit": 2}, str(r["plan"]))
    r = ask(Session, A, "causes?", Stub(plan=[{"name": "get_top_downtime_causes", "arguments": "{not json"}]))
    check("unparseable arguments are refused and AMP's plan answers", r["plan"]["planner"] == "rules", str(r["plan"]))
    many = [{"name": n, "arguments": {}} for n in ("get_oee", "get_downtime", "get_production", "get_machine_status",
                                                   "get_quality_summary", "get_order_delivery")]
    r = ask(Session, A, "everything", Stub(plan=many))
    check(f"a plan is capped at {orchestrator.MAX_TOOL_CALLS} calls", len(r["tools"]) == orchestrator.MAX_TOOL_CALLS
          and any("dropped" in n for n in r["notes"]), str([t["tool"] for t in r["tools"]]))
    r = ask(Session, A, "How is our OEE?", Stub(plan=[{"name": "delete_everything", "arguments": {}}]))
    check("a plan of unknown tools falls back to AMP's plan", [t["tool"] for t in r["tools"]] == ["get_oee"],
          str(r["tools"]))
    r = ask(Session, A, "How is our OEE?", Stub(plan=[{"name": "get_oee", "arguments": {"tenant": F.B}}]))
    check("a plan that passes a tenant falls back to AMP's plan, for the asker's tenant",
          [t["tool"] for t in r["tools"]] == ["get_oee"] and r["plan"]["planner"] == "rules", str(r["plan"]))
    probe = Tool(name="probe_admin_only", description="x", mirrors="/machines", roles=("Admin",),
                 handler=lambda db, tenant: ev.ToolResult("probe_admin_only", ev.OK, "admin data"))
    registry.register(probe)
    try:
        op = Principal(tenant=F.A, role="Operator")
        r = ask(Session, op, "admin things", Stub(plan=[{"name": "probe_admin_only", "arguments": {}}]))
        check("a refusal on role STANDS; it is not swapped for AMP's plan",
              r["tools"][0]["state"] == ev.NOT_PERMITTED and "admin data" not in json.dumps(r), str(r["tools"]))
    finally:
        REGISTRY.pop("probe_admin_only", None)
    r = ask(Session, A, "How is CNC-77?", Stub(plan=[{"name": "get_machine_history", "arguments": {"machine": "CNC-77"}}]))
    check("a machine the user named and the workspace lacks: NOT FOUND stands, and may be quoted",
          r["tools"][0]["state"] == ev.NOT_FOUND and "CNC-77" in r["answer"], r["answer"])

    print()
    print("=" * 74)
    print("6. A MODEL'S TEXT")
    print("=" * 74)
    good = Stub(plan=[{"name": "get_downtime", "arguments": {}}], text="There was 1 stoppage, 12 minutes in all.")
    r = ask(Session, A, "downtime?", good)
    check("grounded wording is shown, labelled llm", r["engine"] == "llm" and r["answer"] == good._text
          and r["grounding"]["passed"], str(r["grounding"]))
    bad = Stub(plan=[{"name": "get_downtime", "arguments": {}}], text="There were 14 stoppages on CNC-99.")
    r = ask(Session, A, "downtime?", bad)
    check("ungrounded wording is not shown; AMP's sentence is", r["engine"] == "rules"
          and r["answer"] != bad._text and not r["grounding"]["passed"], r["answer"])
    check("...and the reasons are counts, not the rejected tokens",
          "CNC-99" not in json.dumps(r["grounding"]) and "14" not in " ".join(r["grounding"]["reasons"]),
          str(r["grounding"]))
    long = Stub(plan=[{"name": "get_downtime", "arguments": {}}], text="1 stoppage. " * 400)
    r = ask(Session, A, "downtime?", long)
    check("an over-long wording is not shown", r["engine"] == "rules" and not r["grounding"]["passed"])
    empty = Stub(plan=[{"name": "get_downtime", "arguments": {}}], text="   ")
    r = ask(Session, A, "downtime?", empty)
    check("an empty wording keeps AMP's sentence", r["engine"] == "rules" and r["answer"])

    print()
    print("=" * 74)
    print("7. MODEL-CHOSEN TEXT DOES NOT REACH THE ANSWER")
    print("=" * 74)
    sneaky = Stub(plan=[{"name": "get_machine_history", "arguments": {"machine": "Call 555-0100 for support"}}])
    r = ask(Session, A, "How are things?", sneaky)
    check("a model-chosen machine name is not quoted in AMP's refusal", "555" not in r["answer"], r["answer"])
    check("...nor in the plan echo", "555" not in json.dumps(r["plan"]), json.dumps(r["plan"]))
    sneaky = Stub(plan=[{"name": "find_record", "arguments": {"query": "Borealis Motors"}}])
    r = ask(Session, A, "Find my order", sneaky)
    check("a model-chosen search term that finds nothing is not quoted",
          "Borealis" not in json.dumps({k: v for k, v in r.items() if k != "question"}), r["answer"])
    typed = Stub(plan=[{"name": "find_record", "arguments": {"query": "WO-001"}}])
    r = ask(Session, A, "Find WO-001", typed)
    check("a term the user typed is shown as typed", r["plan"]["calls"][0]["arguments"] == {"query": "WO-001"},
          str(r["plan"]))

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
