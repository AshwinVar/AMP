"""Turning the AI copilot ON must not take the drill-in button away.

THE DEFECT
----------
Every copilot answer carries a `view` — the screen that owns the detail behind
it — and the UI renders it as "Open Machines →" under the answer
(`AICopilot.tsx`). Two independent holes meant that button was missing far more
often than anyone had noticed:

1. `/ai/ask` returned NO `view` on the LLM success path (`ai_copilot.py`), while
   the rules fallback three lines above it always had. So configuring an API key
   — switching the product's headline AI feature ON — silently REMOVED the
   drill-in from every answer. The one path where the answer is prose rather
   than numbers is the path where a link to the numbers matters most.

2. `AICopilot.tsx` kept its own ten-entry `VIEW_LABEL` table, and the assistant
   had since grown thirteen views. `shifts`, `workorders` and `documents` are
   real screens in the nav that the assistant genuinely returns, and the button
   was suppressed for all three because the component's private copy had never
   heard of them. That one is fixed by deriving the label from the nav
   (`lib/modules.ts: viewLabel`), covered by `modules.test.ts`, and guarded from
   the backend side in section 5 below.

WHY THE VIEW IS NOT COMPUTED BY RUNNING THE PILLAR
--------------------------------------------------
The obvious fix — call `assistant.answer()` alongside the LLM and keep its view
— was measured and rejected. On a 60-machine factory with seven populated
tables, running a pillar costs 2-6 queries for most routes and **22 for
`_briefing`**, which is 116% of the queries `/ai/ask` already spends building
its model context. `_briefing` is also the fallback route, so that fix would
have roughly doubled the database work on exactly the free-form questions people
type once an LLM is connected. `route_view()` reads the same table with zero
queries, and section 3 asserts the endpoint pays nothing for the view.

WHAT SECTION 2 IS FOR
---------------------
The view is now written in two places: the `@_drills_into` declaration and the
pillar's own `return`. Second places drift. Section 2 executes every routable
pillar against a seeded factory and asserts the declaration equals what the
function actually returned — so the two cannot disagree without CI failing.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_drill_in.py
"""
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
import ai_copilot
from ai import assistant
from database import Base

# UTC, not the local date: the pillars compare against datetime.utcnow(), so
# seeding with date.today() makes this fail for part of every day on any machine
# ahead of UTC while UTC CI stays green (test_date_basis_guard.py).
TODAY = datetime.utcnow().date()

T = "DRILLIN"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class StubProvider(ai_copilot.AIProvider):
    """Stands in for a configured LLM. Its answer is deliberately CONTENTLESS —
    it names no machine, no view and no screen — so nothing in section 1 can
    pass because the model happened to say something useful."""
    name = "stub"
    env_key = "STUB_AI_KEY"

    def model(self):
        return "stub-model-v1"

    def ask(self, system, user):
        return "Some prose from a language model."


def seed():
    """A factory with rows in every table a pillar reads, so each pillar returns
    its DATA view rather than its no-data view. `_trend` is the only route where
    those differ ('executive' with data, 'overview' without), and it is the one
    the oracle in section 2 would otherwise not really test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    now = datetime.utcnow()
    mids = []
    for i in range(6):
        m = models.Machine(tenant_code=T, name=f"CNC-{i:02d}", site="P1",
                           status="Breakdown" if i == 0 else "Running",
                           utilization=40 + i * 8, downtime=f"{i * 7} min")
        db.add(m)
        db.flush()
        mids.append(m.id)
    # Two weeks of production so the scorecard has a PRIOR week to compare
    # against -- without it `_trend` has no data and returns 'overview'.
    for i in range(28):
        mid = mids[i % len(mids)]
        when = now - timedelta(days=i % 13)
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=mid, planned_minutes=480,
            runtime_minutes=380 + (i % 60), ideal_cycle_time_seconds=30,
            total_count=600 + i, good_count=540 + i, rejected_count=40 - (i % 20),
            created_at=when))
        db.add(models.DowntimeLog(tenant_code=T, machine_id=mid,
                                  reason=["Feeder jam", "Tool change", "Coolant"][i % 3],
                                  duration=f"{5 + (i % 40)} min", created_at=when))
        db.add(models.QualityInspection(
            tenant_code=T, inspection_no=f"QC{i}", machine_id=mid, inspector="QA",
            inspected_quantity=100, passed_quantity=90 + (i % 8),
            failed_quantity=10 - (i % 8), defect_category="Solder bridge",
            created_at=when))
        db.add(models.MaintenanceTask(
            tenant_code=T, task_no=f"MT{i}", machine_id=mid, task_type="Preventive",
            assigned_to="Tech", planned_date=TODAY - timedelta(days=i % 9),
            status="Open" if i % 3 else "Completed"))
        db.add(models.WorkOrder(
            tenant_code=T, work_order_no=f"WO{i}", part_number="PCB-1",
            batch_number=f"B{i}", machine_id=mid, target_quantity=100,
            actual_quantity=40 + i, status="In Progress" if i % 2 else "Completed"))
        db.add(models.CustomerOrder(
            tenant_code=T, order_no=f"SO{i}", customer_name="Acme Motors",
            product_name="Widget", order_quantity=100, dispatched_quantity=60 + i,
            due_date=TODAY + timedelta(days=(i % 14) - 7)))
        db.add(models.CostRecord(
            tenant_code=T, cost_no=f"C{i}", cost_type="Downtime Loss",
            description="loss", amount=1000 + i, created_at=when))
        db.add(models.ShiftData(
            tenant_code=T, shift_name=["Day", "Night"][i % 2],
            target_output=500, actual_output=430 + i, created_at=when))
    for i in range(12):
        db.add(models.InventoryItem(
            tenant_code=T, item_code=f"I{i}", item_name=f"Resistor {i}",
            category="Raw", unit="pcs", current_stock=i * 3, reorder_level=15))
        db.add(models.ComplianceDocument(
            tenant_code=T, document_no=f"D{i}", title=f"SOP {i}", document_type="SOP",
            department="Operations", owner="QA Lead",
            review_due_date=TODAY + timedelta(days=(i % 20) - 10)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    original = ai_copilot.PROVIDERS
    saved_env = {k: os.environ.get(k) for k in
                 ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "STUB_AI_KEY")}
    try:
        print("=" * 74)
        print("1. /ai/ask RETURNS A DRILL-IN VIEW WHEN THE LLM ANSWERS")
        print("=" * 74)
        for k in saved_env:
            os.environ.pop(k, None)
        ai_copilot.PROVIDERS = (StubProvider(),) + original
        os.environ["STUB_AI_KEY"] = "x"
        check("the stub counts as a configured LLM", ai_copilot._ai_enabled() is True)

        tok = tenancy.set_current_tenant(T)
        out = ai_copilot.ai_ask({"question": "which machines are down?"}, db, {"tenant": T})
        check("the LLM path answered", out["source"] == "llm", str(out.get("source")))
        check("...and the answer is the model's prose",
              out["answer"] == "Some prose from a language model.", out["answer"])
        # THE DEFECT: this key did not exist at all before.
        check("...and it carries a view", out.get("view") == "machines",
              f"view={out.get('view')!r}")

        # The rules fallback has always had one. Same question, both paths, same
        # view -- switching AI on must not move the user to a different screen.
        rules = assistant.answer(db, T, "which machines are down?")
        check("...the SAME view the rules path returns", out.get("view") == rules["view"],
              f"llm={out.get('view')!r} rules={rules['view']!r}")

        # The model's text cannot influence where the button goes: the view is a
        # function of the QUESTION only.
        cost = ai_copilot.ai_ask({"question": "how much are losses costing us?"},
                                 db, {"tenant": T})
        check("a different question moves the view, the same prose does not",
              cost.get("view") == "costing" and cost["answer"] == out["answer"],
              f"view={cost.get('view')!r}")
        tenancy.reset_current_tenant(tok)

        print()
        print("=" * 74)
        print("2. EVERY DECLARED VIEW IS WHAT THE PILLAR ACTUALLY RETURNS")
        print("=" * 74)
        # The reference oracle. `@_drills_into` is a second place the view is
        # written; this is what stops the two drifting apart.
        tok = tenancy.set_current_tenant(T)
        for route in assistant.route_names():
            fn = assistant._pillar(route)
            _text, actual = fn(db, T)
            check(f"{route}: declared {getattr(fn, 'view', None)!r} == returned {actual!r}",
                  getattr(fn, "view", None) == actual,
                  f"declared={getattr(fn, 'view', None)!r} returned={actual!r}")
        tenancy.reset_current_tenant(tok)

        print()
        print("=" * 74)
        print("3. THE VIEW COSTS /ai/ask NOTHING — THE REASON route_view EXISTS")
        print("=" * 74)
        # Counted AT THE ENDPOINT, deliberately.
        #
        # An earlier version of this section counted queries around
        # `route_view()` itself and asserted zero. That assertion could not fail:
        # `route_view(question)` takes no session, so there is no way for it to
        # touch this engine, and the check was guaranteed by the signature rather
        # than by the body. It looked like evidence and was not.
        #
        # The risk worth guarding is one level up and entirely plausible: someone
        # reaches for `assistant.answer(db, tenant, question)["view"]` here
        # because it is the obvious way to get a view. Measured, that costs 2-6
        # queries for most routes and 22 for `_briefing` — the FALLBACK route,
        # so it lands on precisely the free-form questions an LLM invites. This
        # asserts /ai/ask issues not one query more than the context build it
        # already had to do.
        engine = db.get_bind()
        counted = []

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cur, statement, params, context, many):
            counted.append(statement)

        tok = tenancy.set_current_tenant(T)
        ai_copilot._build_factory_context(db, T)          # warm any lazy imports
        counted.clear()
        ai_copilot._build_factory_context(db, T)
        context_only = len(counted)
        counted.clear()
        ai_copilot.ai_ask({"question": "what needs my attention?"}, db, {"tenant": T})
        with_view = len(counted)
        tenancy.reset_current_tenant(tok)
        event.remove(engine, "before_cursor_execute", _count)
        check(f"the context build costs {context_only} queries", context_only > 0,
              str(context_only))
        check(f"...and /ai/ask on the fallback route costs the same {context_only}, "
              f"not more", with_view == context_only,
              f"context={context_only} endpoint={with_view} "
              f"(+{with_view - context_only} for the view)")

        print()
        print("=" * 74)
        print("4. route_view() AGREES WITH THE FULL ROUTER")
        print("=" * 74)
        # Narrower on purpose (it skips the machine-name and `find` branches),
        # so agreement is asserted on the keyword-routed questions it claims to
        # cover -- one per pillar, phrased as a user would.
        tok = tenancy.set_current_tenant(T)
        QUESTIONS = [
            "what should I reorder first?", "are any orders late?",
            "how much are losses costing us?", "how is quality looking?",
            "any maintenance overdue?", "how are our controlled documents?",
            "how much downtime did we have?", "which machines are down?",
            "what was our output this week?", "how much WIP is in progress?",
            "did the night shift hit target?", "what is our OEE?",
            "how are we doing vs last week?", "what needs my attention?",
            "what can you do?", "tell me something unroutable about penguins",
        ]
        mismatches = []
        for q in QUESTIONS:
            cheap = assistant.route_view(q)
            full = assistant.answer(db, T, q)["view"]
            if cheap != full:
                mismatches.append((q, cheap, full))
        check(f"all {len(QUESTIONS)} keyword-routed questions agree", not mismatches,
              str(mismatches))
        check("an unroutable question falls back to the briefing's view",
              assistant.route_view("penguins") == "overview",
              assistant.route_view("penguins"))
        check("...and so does an empty question",
              assistant.route_view("") == "overview", assistant.route_view(""))
        check("...and None, which is what a missing JSON field arrives as",
              assistant.route_view(None) == "overview", str(assistant.route_view(None)))
        tenancy.reset_current_tenant(tok)

        print()
        print("=" * 74)
        print("5. EVERY VIEW THE ASSISTANT NAMES IS A REAL SCREEN IN THE NAV")
        print("=" * 74)
        # The cross-language guard for defect 2. The backend names a view; the
        # frontend renders a button for it. Nothing else in CI compares the two
        # lists, which is exactly how `shifts`, `workorders` and `documents`
        # ended up unreachable.
        nav = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "frontend", "lib", "modules.ts")
        keys = set()
        if os.path.exists(nav):
            with open(nav, encoding="utf-8") as f:
                keys = set(re.findall(r'\{\s*key:\s*"([a-z_]+)"', f.read()))
        check("the nav catalogue was found and parsed", len(keys) > 20, f"{len(keys)} keys")
        declared = {assistant._pillar(r).view for r in assistant.route_names()}
        missing = sorted(declared - keys)
        check(f"all {len(declared)} declared views exist in the nav", not missing,
              f"not in NAV_ITEMS: {missing}")
        # And the drill-in target of the two non-routable helpers, which reach
        # the UI through answer() even though route_view() never returns them.
        for extra in ("machines", "overview"):
            check(f"'{extra}' (used by the find / machine-detail answers) is a nav key",
                  extra in keys)

    finally:
        ai_copilot.PROVIDERS = original
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        db.close()

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
