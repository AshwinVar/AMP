"""Does the copilot tell the truth? (AI roadmap, phase 5 — the evaluation harness)

WHY THIS EXISTS, AND WHY IT COMES BEFORE TOOLS
-----------------------------------------------
The roadmap's phase 3 is a tool-using copilot: the model picks
`get_machine_downtime(machine=4, period=yesterday)` and AMP executes it. You
cannot tell whether that is an improvement without a way to score answers
FIRST — otherwise "it feels better" is the whole evidence base. So the
evaluation harness is built before the tools it will judge.

WHAT IS SCORED
--------------
Every case below has a DETERMINISTIC answer computed from the database by this
file, independently of the code under test. Three properties, which fail for
different reasons and so are asserted separately:

  ROUTING    the question reached the right pillar (`matched`)
  GROUNDING  the sentence contains the number the database actually holds
  ISOLATION  a question asked as Factory A never surfaces Factory B's data

WHY THE RULE-BASED COPILOT AND NOT THE LLM
-------------------------------------------
`ai/assistant.answer` needs no API key, so this runs in CI on every commit —
which is the only way a regression gets caught. The LLM path
(`ai_copilot._ask_llm`) cannot be scored here without a key and a network call;
when it is scored, THESE cases are the ground truth it will be scored against,
because the facts do not depend on which engine produced the sentence.

A NOTE ON WHAT "GROUNDING" CATCHES
----------------------------------
The rule copilot reads the database, so it cannot invent a machine that does not
exist the way a language model can. What it CAN do — and what this catches — is
report a number that no longer matches the data: an off-by-one in a projection,
a filter that drifts, a count that stops respecting the tenant. That is the
failure this suite is really for.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_evaluation.py
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import assistant
from database import Base

A = "EVAL_ALPHA"
B = "EVAL_BETA"

failures = []
scored = {"routing": [0, 0], "grounding": [0, 0], "isolation": [0, 0]}


def score(kind, label, condition, detail=""):
    scored[kind][1] += 1
    if condition:
        scored[kind][0] += 1
    else:
        failures.append(f"[{kind}] {label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  [{kind:<9}] {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)

    db = Session()
    tok = tenancy.set_current_tenant(None)
    for t in (A, B):
        db.add(models.TenantConfig(tenant_code=t))
    db.commit()

    # FACTORY A — a knowable shape. Three machines, one of them broken.
    for name, status in (("ALPHA-CNC-01", "Running"),
                         ("ALPHA-PRESS-02", "Breakdown"),
                         ("ALPHA-LATHE-03", "Running")):
        db.add(models.Machine(tenant_code=A, site="P1", name=name, status=status,
                              utilization=60, downtime="0 min"))
    # FACTORY B — deliberately DIFFERENT numbers, so a leak is visible as a
    # wrong figure and not just as a wrong name.
    for i in range(7):
        db.add(models.Machine(tenant_code=B, site="P1", name=f"BETA-{i:02d}",
                              status="Breakdown", utilization=0, downtime="0 min"))
    db.commit()

    a_machines = db.query(models.Machine).filter(models.Machine.tenant_code == A).all()
    b_machines = db.query(models.Machine).filter(models.Machine.tenant_code == B).all()

    # A: exactly 2 downtime events, both "Feeder jam".
    for _ in range(2):
        db.add(models.DowntimeLog(tenant_code=A, machine_id=a_machines[1].id,
                                  reason="Feeder jam", duration="15 min"))
    # B: 11 events with a distinctive reason that must never appear in A's answer.
    for _ in range(11):
        db.add(models.DowntimeLog(tenant_code=B, machine_id=b_machines[0].id,
                                  reason="Betaonly coolant fault", duration="99 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def ask(Session, tenant, question):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return assistant.answer(db, tenant, question)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session = seed()

    print("=" * 74)
    print("1. ROUTING — the question reaches the right pillar")
    print("=" * 74)
    ROUTING_CASES = [
        ("how much downtime did we have?", "downtime"),
        ("which machines are down?", "machines"),
        ("what is our OEE?", "oee"),
        ("are we low on any stock?", "inventory"),
        ("how is quality looking?", "quality"),
        ("what is this costing me?", "cost"),
        ("is anything overdue for maintenance?", "maintenance"),
        ("what needs my attention?", "briefing"),
        ("what can I ask you?", "help"),
    ]
    for question, expected in ROUTING_CASES:
        got = ask(Session, A, question).get("matched")
        score("routing", f'"{question}" -> {expected}', got == expected, f"got {got!r}")

    print()
    print("=" * 74)
    print("2. GROUNDING — the sentence carries the number the database holds")
    print("=" * 74)
    # Ground truth computed HERE, from the rows, not from the code under test.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    a_down_events = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.tenant_code == A).count()
    a_broken = db.query(models.Machine).filter(
        models.Machine.tenant_code == A,
        models.Machine.status == "Breakdown").count()
    tenancy.reset_current_tenant(tok)
    db.close()

    dt = ask(Session, A, "how much downtime did we have?")["answer"]
    score("grounding", f"downtime answer states {a_down_events} events",
          str(a_down_events) in dt, dt)
    score("grounding", "...and names the real top cause", "Feeder jam" in dt, dt)

    mach = ask(Session, A, "which machines are down?")["answer"]
    score("grounding", f"machines answer reflects {a_broken} broken",
          str(a_broken) in mach, mach)
    score("grounding", "...and names the actual broken machine",
          "ALPHA-PRESS-02" in mach, mach)

    print()
    print("=" * 74)
    print("3. ISOLATION — Factory A's copilot never speaks about Factory B")
    print("=" * 74)
    # The property that matters most. B was seeded with numbers and words that
    # cannot occur in a correct answer for A.
    LEAKS = ("BETA-", "Betaonly", "99 min")
    for question, _ in ROUTING_CASES:
        text = ask(Session, A, question)["answer"]
        leaked = [needle for needle in LEAKS if needle in text]
        score("isolation", f'"{question[:34]}" leaks nothing', not leaked,
              f"leaked {leaked} in {text[:90]!r}")

    # And the reverse direction, so a pass cannot mean "answers are empty".
    b_dt = ask(Session, B, "how much downtime did we have?")["answer"]
    score("isolation", "CONTROL: Factory B DOES see its own 11 events",
          "11" in b_dt, b_dt)
    score("isolation", "...and A's reasons never appear in B's answer",
          "Feeder jam" not in b_dt, b_dt)

    print()
    print("=" * 74)
    print("EVALUATION SCORECARD")
    print("=" * 74)
    for kind, (ok, total) in scored.items():
        pct = (100.0 * ok / total) if total else 0.0
        print(f"  {kind:<10} {ok:>3}/{total:<3}  {pct:5.1f}%")
    overall_ok = sum(v[0] for v in scored.values())
    overall_total = sum(v[1] for v in scored.values())
    print(f"  {'OVERALL':<10} {overall_ok:>3}/{overall_total:<3}  "
          f"{100.0 * overall_ok / overall_total:5.1f}%")

    print()
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
