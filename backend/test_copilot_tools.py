"""The AMP tool registry decides; the caller only asks (ADR-0022).

What is pinned here, one property per section:

  1. REFUSALS ARE RESULTS. An unknown tool, a principal with no tenant or an OEM
     sentinel tenant, a role the tool does not admit, a pack the plan does not
     license, and bad arguments each come back as a refusal with a state, never
     as an exception and never as data.
  2. ARGUMENTS ARE STRICT. Unknown keys (a "tenant" above all), wrong types,
     out-of-range numbers, over-long text and non-object arguments are refused;
     a numeric string for an int is the one coercion.
  3. NO TOOL CAN BE GIVEN A SCOPE. Registering a parameter named like a scope
     (tenant, oem, company, ...) raises; no registered tool has one.
  4. THE TENANT IS BOUND BY run_tool. With NO ambient tenant (a background job,
     a test, a future caller that forgets), a tool still reads only the
     principal's rows, because run_tool binds it; and the binding is released.
  5. A FAILING HANDLER IS A GENERIC "FAILED". Its exception text, which can
     carry data, is not returned.
  6. EVERY TOOL, EVERY FACTORY: each tool runs for each of the three factories,
     returns only that factory's data (no other factory's markers anywhere in
     its result), labels every fact with a known provenance, and says the same
     sentence the rule copilot says for the same read-model.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_tools.py
"""
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tenancy
from ai import assistant
from ai import evidence as ev
from ai.tools import REGISTRY, Param, Principal, Tool, registry, run_tool, validate_args
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


def call(Session, principal, name, args=None, ambient="principal"):
    """Run a tool the way a request would (ambient = the principal's tenant) or
    with NO ambient tenant (ambient=None), which only run_tool's own binding covers."""
    db = Session()
    tok = tenancy.set_current_tenant(principal.tenant if ambient == "principal" else ambient)
    try:
        return run_tool(db, principal, name, args)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def leaks(result, tenant):
    text = json.dumps(result.to_dict(), default=str).lower()
    return [f"{o}:{m}" for o, ms in F.MARKERS.items() if o != tenant for m in ms if m.lower() in text]


def main():
    Session = session()
    admin_a = Principal(tenant=F.A, role="Admin", username="a-admin")
    operator_a = Principal(tenant=F.A, role="Operator", username="a-op")

    print("=" * 74)
    print("1. REFUSALS ARE RESULTS")
    print("=" * 74)
    r = call(Session, admin_a, "get_everything")
    check("an unknown tool is NOT FOUND", r.state == ev.NOT_FOUND and not r.facts, r.state)
    r = call(Session, admin_a, {"name": "get_oee"})
    check("a non-string tool name is NOT FOUND, not an exception", r.state == ev.NOT_FOUND, r.state)
    r = call(Session, Principal(tenant="", role="Admin"), "get_oee")
    check("a principal with no tenant is NOT PERMITTED", r.state == ev.NOT_PERMITTED, r.state)
    r = call(Session, Principal(tenant="OEM:ACME", role="Admin"), "get_machine_status", ambient="OEM:ACME")
    check("an OEM sentinel tenant is NOT PERMITTED", r.state == ev.NOT_PERMITTED and not r.facts, r.state)
    r = call(Session, {"tenant": F.A, "role": "Admin"}, "get_oee", ambient=F.A)
    check("a dict posing as a principal is NOT PERMITTED", r.state == ev.NOT_PERMITTED, r.state)

    # A role-restricted tool, registered for this test only and removed after.
    probe = Tool(name="probe_admin_only", description="test", mirrors="/machines", roles=("Admin",),
                 handler=lambda db, tenant: ev.ToolResult("probe_admin_only", ev.OK, f"ran for {tenant}"))
    registry.register(probe)
    try:
        r = call(Session, operator_a, "probe_admin_only")
        check("an Operator is NOT PERMITTED a tool that admits Admin only", r.state == ev.NOT_PERMITTED, r.state)
        r = call(Session, admin_a, "probe_admin_only")
        check("...and the Admin it admits runs it", r.state == ev.OK and r.summary == f"ran for {F.A}", r.summary)
        names = {t["name"] for t in registry.catalog(operator_a)}
        check("the catalogue an Operator is shown does not list it", "probe_admin_only" not in names)
        check("...and the Admin's does", "probe_admin_only" in {t["name"] for t in registry.catalog(admin_a)})
    finally:
        REGISTRY.pop("probe_admin_only", None)

    # Licence: a tool that mirrors a Factory-pack route, for a tenant licensed without it.
    original = registry._licensed_packs
    try:
        registry._licensed_packs = lambda tenant: frozenset({"operations"})
        r = call(Session, admin_a, "get_machine_history", {"machine": "CNC-01"})
        check("a tool whose route is in an unlicensed pack is NOT LICENSED", r.state == ev.NOT_LICENSED, r.state)
        check("...and says which pack", "Factory" in r.summary or "factory" in r.summary, r.summary)
        r = call(Session, admin_a, "get_downtime")
        check("an ungated tool runs whatever the licence", r.state == ev.OK, r.state)
        registry._licensed_packs = lambda tenant: None
        r = call(Session, admin_a, "get_machine_history", {"machine": "CNC-01"})
        check("an unreadable licence fails OPEN, as the plan gate does", r.state != ev.NOT_LICENSED, r.state)
        registry._licensed_packs = lambda tenant: frozenset({"factory"})
        r = call(Session, admin_a, "get_machine_history", {"machine": "CNC-01"})
        check("the licensed pack runs", r.state in (ev.OK, ev.PARTIAL_DATA), r.state)
    finally:
        registry._licensed_packs = original

    print()
    print("=" * 74)
    print("2. ARGUMENTS ARE STRICT")
    print("=" * 74)
    t = REGISTRY["get_top_downtime_causes"]
    for args, label in [({"tenant": F.B}, "a tenant argument"), ({"tenant_code": F.B}, "a tenant_code argument"),
                        ({"limit": 3, "oem": "ACME"}, "an oem argument beside a valid one"),
                        ({"limit": "three"}, "a word for a number"), ({"limit": 99}, "an out-of-range number"),
                        ({"limit": True}, "a boolean for a number"), ({"limit": 2.5}, "a fraction for a whole number"),
                        ("limit=3", "a string instead of an object"), ([3], "a list instead of an object")]:
        clean, errors = validate_args(t, args)
        check(f"refused: {label}", bool(errors), f"clean={clean}")
    clean, errors = validate_args(t, {"limit": "4"})
    check("a numeric string for an int is accepted as that int", clean == {"limit": 4} and not errors, f"{clean}")
    clean, errors = validate_args(t, {})
    check("an omitted optional argument takes its default", clean == {"limit": 3} and not errors, f"{clean}")
    h = REGISTRY["get_machine_history"]
    check("a missing required argument is refused", bool(validate_args(h, {})[1]))
    check("text over its length limit is refused", bool(validate_args(h, {"machine": "X" * 500})[1]))
    check("blank text is refused", bool(validate_args(h, {"machine": "   "})[1]))
    r = call(Session, admin_a, "get_downtime", {"tenant": F.B})
    check("run_tool refuses a scope argument as INVALID ARGUMENTS, and reads nothing",
          r.state == ev.INVALID_ARGUMENTS and not r.facts, r.state)

    print()
    print("=" * 74)
    print("3. NO TOOL CAN BE GIVEN A SCOPE")
    print("=" * 74)
    for bad in ("tenant", "tenant_code", "oem_code", "company", "workspace", "organisation", "factory"):
        try:
            registry.register(Tool(name=f"probe_{bad}", description="x", mirrors="/machines",
                                   handler=lambda db, tenant, **k: None,
                                   params={bad: Param("str", "x")}))
            REGISTRY.pop(f"probe_{bad}", None)
            check(f"a parameter named {bad!r} cannot be registered", False)
        except ValueError:
            check(f"a parameter named {bad!r} cannot be registered", True)
    scoped = [f"{n}.{p}" for n, tl in REGISTRY.items() for p in tl.params if registry._SCOPE_WORDS.search(p)]
    check("no registered tool has a scope parameter", not scoped, str(scoped))
    check("every tool's schema forbids extra properties",
          all(tl.schema()["parameters"]["additionalProperties"] is False for tl in REGISTRY.values()))
    try:
        registry.register(Tool(name="get_oee", description="dup", mirrors="/x", handler=lambda db, t: None))
        check("a tool name cannot be registered twice", False)
    except ValueError:
        check("a tool name cannot be registered twice", True)

    print()
    print("=" * 74)
    print("4. THE TENANT IS BOUND BY run_tool, AND RELEASED")
    print("=" * 74)
    for name, args in (("get_downtime", {}), ("get_machine_status", {}), ("get_production", {}),
                       ("get_inventory_status", {}), ("find_record", {"query": "WO-001"})):
        for tenant in F.TENANTS:
            p = Principal(tenant=tenant, role="Admin")
            r = call(Session, p, name, args, ambient=None)
            check(f"{name} with NO ambient tenant reads only {tenant}'s rows", not leaks(r, tenant),
                  str(leaks(r, tenant)))
    check("the binding is released after the call", tenancy.current_tenant() is None, str(tenancy.current_tenant()))
    b_as_a = call(Session, Principal(tenant=F.B, role="Admin"), "get_machine_status", ambient=F.A)
    down = [f.value for f in b_as_a.facts if f.key.startswith("machines.down_")]
    check("the PRINCIPAL's tenant wins over a different ambient one (B's CNC-01 is the one down)",
          down == ["CNC-01"], str(down))

    print()
    print("=" * 74)
    print("5. A FAILING HANDLER IS A GENERIC FAILED")
    print("=" * 74)

    def boom(db, tenant):
        raise RuntimeError("secret row: FACTORY_B Hydraulic seal leak")
    registry.register(Tool(name="probe_boom", description="x", mirrors="/machines", handler=boom))
    try:
        r = call(Session, admin_a, "probe_boom")
        check("a handler exception is FAILED", r.state == ev.FAILED, r.state)
        check("...and its message is not returned", "Hydraulic" not in r.summary and "secret" not in r.summary,
              r.summary)
    finally:
        REGISTRY.pop("probe_boom", None)

    print()
    print("=" * 74)
    print("6. EVERY TOOL, EVERY FACTORY")
    print("=" * 74)
    args_for = {"get_machine_history": {"machine": "CNC-01"}, "find_record": {"query": "WO-001"}}
    for name in sorted(REGISTRY):
        for tenant in F.TENANTS:
            p = Principal(tenant=tenant, role="Admin")
            r = call(Session, p, name, args_for.get(name))
            bad_prov = [f.key for f in r.facts if f.provenance not in ev.PROVENANCE]
            check(f"{name} / {tenant}: runs, only its own data, known provenance",
                  not r.refused and not leaks(r, tenant) and not bad_prov,
                  f"state={r.state} leaks={leaks(r, tenant)} prov={bad_prov}")
    # The same sentence as the rule copilot, for the same read-model.
    for pillar, tool in (("downtime", "get_downtime"), ("oee", "get_oee"), ("machines", "get_machine_status"),
                         ("cost", "get_financial_losses"), ("quality", "get_quality_summary"),
                         ("inventory", "get_inventory_status"), ("briefing", "get_factory_summary")):
        for tenant in F.TENANTS:
            db = Session()
            tok = tenancy.set_current_tenant(tenant)
            try:
                said, _view = assistant._pillar(pillar)(db, tenant)
                r = run_tool(db, Principal(tenant=tenant, role="Admin"), tool)
            finally:
                tenancy.reset_current_tenant(tok)
                db.close()
            check(f"{tool} / {tenant} says what the rule copilot says", r.summary == said,
                  f"{r.summary!r} vs {said!r}")
    # Nothing set up is not "healthy" and not "all running" (the evaluation found
    # both: C has no stock records, and an empty plant read "All 0 machines").
    empty_stock = call(Session, Principal(tenant=F.C, role="Admin"), "get_inventory_status")
    check("C has no stock records: it says so, and does not call stock healthy",
          empty_stock.state == ev.NO_DATA and "No stock items" in empty_stock.summary
          and "healthy" not in empty_stock.summary, empty_stock.summary)
    text, _view = assistant.say_machines([])
    check("an empty plant is not 'All 0 machines are running'", "No machines" in text and "running" not in text, text)
    no_money = call(Session, Principal(tenant=F.B, role="Admin"), "get_financial_losses")
    check("B has no unit value: its losses are NOT CONFIGURED, with no money figure",
          no_money.state == ev.NOT_CONFIGURED
          and not any(f.unit == "£" and isinstance(f.value, (int, float)) for f in no_money.facts),
          no_money.state)
    money = next((f for f in no_money.facts if f.key == "losses.cost"), None)
    check("...and the money figure is stated as UNKNOWN, not left out silently",
          money is not None and money.provenance == ev.UNKNOWN and money.value is None)
    partial = call(Session, Principal(tenant=F.C, role="Admin"), "get_oee")
    check("C: one machine never reported, so plant OEE is PARTIAL DATA", partial.state == ev.PARTIAL_DATA,
          partial.state)
    plan = call(Session, Principal(tenant=F.C, role="Admin"), "get_production_vs_target")
    check("C has no plan: production vs target is NOT CONFIGURED, not 0%", plan.state == ev.NOT_CONFIGURED
          and not any(f.key == "plan.attainment" for f in plan.facts), plan.state)
    ghost = call(Session, admin_a, "get_machine_history", {"machine": "WELD-07"})
    # The summary quotes the name A typed; nothing ELSE of B's may appear.
    others = [x for x in leaks(ghost, F.A) if x != f"{F.B}:WELD-07"]
    check("A asking for B's WELD-07 is NOT FOUND in A's workspace, with nothing of B's",
          ghost.state == ev.NOT_FOUND and not ghost.facts and not others and "this workspace" in ghost.summary,
          f"{ghost.summary} {others}")

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
