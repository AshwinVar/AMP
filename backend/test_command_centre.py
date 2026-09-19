"""The Command Centre answers the owner's five questions, and sizes them honestly (ADR-0024).

Run against the three-factory evaluation environment (copilot_eval.fixtures), so
a healthy plant, a broken one and one with half its data are all covered by the
same assertions. What is pinned:

  1. WHERE ARE WE. The plant OEE carries its coverage; a plant that reported
     nothing says so instead of reading 0%; a plant with no production plan says
     NOT CONFIGURED instead of 0% attainment.
  2. WHAT IS WRONG, RANKED BY MEASURED IMPACT. On FACTORY_B the machine that is
     down right now leads, then the plan that came up 600 units short, then the
     recurring hydraulic fault at 180 -- ranked by what they cost, not by anyone
     typing "high". Every problem AMP cannot size says so (NOT MEASURED) and
     never reads as zero, and the card never totals figures that overlap.
  3. WHY. A missed plan names the downtime measured on ITS OWN machine as a
     LIKELY CONTRIBUTOR, and says INSUFFICIENT EVIDENCE when there is none.
  4. WHAT IT COSTS. Money only where a unit value is set (A and C); on B, which
     has none, no money figure appears anywhere in the card.
  5. WHAT TO DO. Agent actions awaiting approval come first, each saying who
     decides; then AMP's suggestion for the biggest problem.

  6. TENANT ISOLATION: no other factory's markers appear in any card.
  7. COST: the card costs no more queries than the read-models it composes
     (a recorded budget, so a future change that adds a query is seen).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_command_centre.py
"""
import json
import sys

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import evidence as ev
from ai.command_centre import build_command_centre
from copilot_eval import fixtures as F
from currency import CURRENCY
from database import Base

QUERY_BUDGET = 60

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
    return Session, engine


def centre(Session, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return build_command_centre(db, tenant)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session, engine = session()
    cards = {t: centre(Session, t) for t in F.TENANTS}
    a, b, c = cards[F.A], cards[F.B], cards[F.C]

    print("=" * 74)
    print("1. WHERE ARE WE")
    print("=" * 74)
    check("A: the plant OEE is reported", isinstance(a["position"]["oee"], (int, float)), str(a["position"]["oee"]))
    check("A: every machine reported, so coverage is complete and the card says so",
          a["position"]["oee_coverage"]["complete"] is True and a["position"]["coverage_phrase"] == ""
          and a["state"] == ev.OK, str(a["position"]["oee_coverage"]))
    check("C: one machine never reported, so the card is PARTIAL DATA and states the coverage",
          c["state"] == ev.PARTIAL_DATA and "2 of 3" in c["position"]["coverage_phrase"],
          f"{c['state']} {c['position']['coverage_phrase']}")
    check("C: ...and the headline carries that coverage, not a bare figure",
          "2 of 3" in c["headline"], c["headline"])
    check("A: machines are counted from their live status",
          a["position"]["machines"]["total"] == 3 and a["position"]["machines"]["down"] == 0)
    check("B: the broken machine is named, not just counted",
          b["position"]["machines"]["down"] == 1 and b["position"]["machines"]["down_names"] == ["CNC-01"],
          str(b["position"]["machines"]))
    check("A/B: attainment against the plans that came due",
          a["position"]["plan"]["state"] == ev.OK and a["position"]["plan"]["attainment_rate"] == 102
          and b["position"]["plan"]["attainment_rate"] == 52,
          f"A={a['position']['plan']} B={b['position']['plan']}")
    check("C: no plan is set, so attainment is NOT CONFIGURED and has no rate",
          c["position"]["plan"]["state"] == ev.NOT_CONFIGURED and c["position"]["plan"]["attainment_rate"] is None,
          str(c["position"]["plan"]))
    check("C: ...and the headline says so rather than implying a miss",
          "no production plan is set" in c["headline"], c["headline"])

    print()
    print("=" * 74)
    print("2. WHAT IS WRONG, RANKED BY MEASURED IMPACT")
    print("=" * 74)
    check("B: the machine that is down RIGHT NOW leads, whatever it has cost so far",
          b["problems"][0]["key"] == "machines.down" and b["problems"][0]["rank_basis"] == "stopped now",
          str([(p["key"], p["rank_basis"]) for p in b["problems"]]))
    top = next(p for p in b["problems"] if p["impact_units"] is not None)
    check("B: then the biggest MEASURED loss: the plan that came up 600 units short",
          top["key"] == "plan.PLAN-B1" and top["impact_units"] == 600, f"{top['key']} {top['impact_units']}")
    check("B: ...and the recurring hydraulic fault is sized too, below it",
          any(p["key"].startswith("downtime.hydraulic") and 0 < p["impact_units"] < top["impact_units"]
              for p in b["problems"] if p["impact_units"] is not None),
          str([(p["key"], p["impact_units"]) for p in b["problems"]]))
    sized = [p["impact_units"] for p in b["problems"] if p["impact_units"] is not None]
    check("B: sized problems are in descending order of impact", sized == sorted(sized, reverse=True), str(sized))
    check("B: the card says its figures are not a total, because problems can overlap",
          "not a total" in b["overlap_note"], b["overlap_note"])
    check("B: the headline names the stoppage and the biggest measured loss",
          "down right now" in b["headline"] and "Biggest measured loss" in b["headline"], b["headline"])
    check("B: every unsized problem says NOT MEASURED, and none reads as zero",
          all(p["state"] == ev.NOT_MEASURED and p["impact_units"] is None
              for p in b["problems"] if p["impact_units"] is None),
          str([(p["key"], p["state"], p["impact_units"]) for p in b["problems"]]))
    check("B: the machine that is down right now is on the card",
          any(p["key"] == "machines.down" for p in b["problems"]), str([p["key"] for p in b["problems"]]))
    check("A: a healthy plant has few problems and none of B's",
          len(a["problems"]) <= 2 and not any("hydraulic" in p["key"] for p in a["problems"]),
          str([p["key"] for p in a["problems"]]))
    check("every problem carries the facts behind it, each with a known provenance",
          all(p["facts"] and all(f["provenance"] in ev.PROVENANCE for f in p["facts"])
              for card in cards.values() for p in card["problems"]))

    print()
    print("=" * 74)
    print("3. WHY")
    print("=" * 74)
    plan_problem = next((p for p in b["problems"] if p["key"].startswith("plan.")), None)
    check("B: a missed plan is on the card", plan_problem is not None, str([p["key"] for p in b["problems"]]))
    if plan_problem:
        why = plan_problem["why"][0]
        check("B: ...and names its own machine's downtime as a LIKELY CONTRIBUTOR",
              why["label"] == ev.LIKELY_CONTRIBUTOR and "CNC-01" in why["text"], str(why))
        check("B: ...with the minutes as a measured fact, not an assertion",
              why["facts"] and why["facts"][0]["provenance"] == ev.MEASURED, str(why.get("facts")))
        check("B: ...and never claims a confirmed cause from this evidence",
              all(w["label"] != ev.CAUSE_CONFIRMED for p in b["problems"] for w in p["why"]))

    print()
    print("=" * 74)
    print("4. WHAT IT COSTS")
    print("=" * 74)
    check("A: a unit value is set, so losses are priced",
          a["cost"]["state"] == ev.OK and a["cost"]["priced"] is True
          and isinstance(a["cost"]["loss_cost"], (int, float)), str(a["cost"]))
    check("B: NO unit value, so the cost is NOT CONFIGURED and stated unknown",
          b["cost"]["state"] == ev.NOT_CONFIGURED and b["cost"]["loss_cost"] is None
          and any(f["key"] == "losses.cost" and f["provenance"] == ev.UNKNOWN for f in b["cost"]["facts"]),
          str(b["cost"]))
    text_b = json.dumps(b, default=str)
    check("B: no money SYMBOL appears anywhere on the card", CURRENCY not in text_b,
          text_b[max(0, text_b.find(CURRENCY) - 60):text_b.find(CURRENCY) + 60])
    # And no money VALUE either. The symbol check alone missed a planted defect
    # that priced every loss at a made-up rate and simply left the symbol off.
    priced_fields = [(p["key"], p["impact_money"]) for p in b["problems"] if p["impact_money"] is not None]
    check("B: ...and no problem carries a money figure", not priced_fields, str(priced_fields))
    check("B: ...nor does the cost block", b["cost"]["loss_cost"] is None and b["cost"]["unit_value"] is None,
          str(b["cost"]))
    check("B: every problem's currency field is empty, since there is no rate",
          all(p["currency"] is None for p in b["problems"]),
          str([(p["key"], p["currency"]) for p in b["problems"]]))
    check("B: ...but the loss is still sized, in good units",
          isinstance(b["cost"]["lost_units"], int) and b["cost"]["lost_units"] > 0, str(b["cost"]["lost_units"]))
    check("A: the priced problems carry money, with the currency named",
          all(p["impact_money"] is not None and p["currency"] == CURRENCY
              for p in a["problems"] if p["impact_units"] is not None),
          str([(p["key"], p["impact_money"]) for p in a["problems"]]))

    print()
    print("=" * 74)
    print("5. WHAT TO DO")
    print("=" * 74)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        db.add(models.AgentAction(tenant_code=F.B, agent="maintenance", action_type="create_task",
                                  summary="Raise a critical maintenance task on CNC-01", status="Proposed",
                                  ref_kind="maintenance_task", ref_id=1))
        db.add(models.AgentAction(tenant_code=F.A, agent="reorder", action_type="draft_po",
                                  summary="Draft a PO for Alpha bearing", status="Proposed",
                                  ref_kind="purchase_order", ref_id=2))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    b2 = centre(Session, F.B)
    first = b2["actions"][0]
    check("B: the decision waiting comes first", first["title"].startswith("Raise a critical maintenance task"),
          str(first))
    check("B: ...and says who decides it, not that AMP will",
          "Admin or Supervisor approves" in first["who"], first["who"])
    check("B: the other factory's proposed action is NOT on B's card",
          "Alpha bearing" not in json.dumps(b2["actions"], default=str))
    check("B: AMP suggests dealing with the biggest problem",
          any(x["key"] == "next_best" and b2["problems"][0]["title"] in x["title"] for x in b2["actions"]),
          str([x["key"] for x in b2["actions"]]))
    check("B: ...and suggests setting a unit value, since money cannot be shown",
          any(x["key"] == "set_unit_value" for x in b2["actions"]))
    check("A: with a unit value set, that suggestion is not made",
          not any(x["key"] == "set_unit_value" for x in cards[F.A]["actions"]))

    print()
    print("=" * 74)
    print("6. TENANT ISOLATION")
    print("=" * 74)
    for tenant, card in cards.items():
        text = json.dumps(card, default=str)
        leaks = [f"{owner}:{m}" for owner, ms in F.MARKERS.items() if owner != tenant for m in ms if m in text]
        check(f"{tenant}: no other factory's data on the card", not leaks, str(leaks))

    print()
    print("=" * 74)
    print("7. COST")
    print("=" * 74)
    counted = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cur, statement, params, context, many):
        counted.append(statement)

    centre(Session, F.B)      # warm
    counted.clear()
    centre(Session, F.B)
    n = len(counted)
    event.remove(engine, "before_cursor_execute", _count)
    print(f"  (the card costs {n} queries)")
    check(f"the card stays within its recorded budget of {QUERY_BUDGET} queries", n <= QUERY_BUDGET, str(n))

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
