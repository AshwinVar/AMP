"""The Risk Radar states a rule and a measurement for every risk (ADR-0026).

Run against the three-factory environment. What is pinned:

  1. EVERY RISK CARRIES ITS RULE. Not a severity word someone typed: the rule,
     with the threshold in it, and the facts it was computed from.
  2. THE RULES FIRE WHERE THEY SHOULD. On FACTORY_B: an order past its date, a
     machine over the rule-score threshold, overdue maintenance, an item with
     nothing on hand. On the healthy factory, nothing is LIKELY.
  3. NO PREDICTION IS CLAIMED. No probability anywhere, no "machine learning",
     and the machine risk says in the fact itself that it is a hand-weighted
     rule. The card carries the same statement.
  4. A RATE IS MEASURED, NOT ASSUMED. The order rule uses the plant's own
     good-units-a-day over the window, recomputed here from the seed. With no
     production recorded, the radar says so (INSUFFICIENT HISTORY) instead of
     guessing whether a date is reachable.
  5. LIKELIHOOD WORDS ARE THE SHARED VOCABULARY, and the ordering puts LIKELY
     first.
  6. MONEY only where a unit value is set; TENANT ISOLATION across all three.
  7. EVERY RISK ENDS IN SOMETHING TO DO (ADR-0039), and a PROPOSAL only where
     AMP can actually carry it out. This card used to end in a deep link.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_risk_radar.py
"""
import json
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import evidence as ev
from ai.risk_radar import (COVER_LIKELY_DAYS, MACHINE_LIKELY_SCORE, build_risk_radar)
from copilot_eval import fixtures as F
from currency import CURRENCY
from database import Base

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session(seed=True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    if seed:
        F.seed(Session)
    return Session


def radar(Session, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return build_risk_radar(db, tenant)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session = session()
    out = {t: radar(Session, t) for t in F.TENANTS}
    a, b, c = out[F.A], out[F.B], out[F.C]
    by_key = {r["key"]: r for r in b["risks"]}

    print("=" * 74)
    print("1. EVERY RISK CARRIES ITS RULE")
    print("=" * 74)
    check("B has risks to check (the assertions below are not vacuous)", len(b["risks"]) >= 4,
          str(len(b["risks"])))
    check("every risk states a rule", all(r["rule"] for t in out.values() for r in t["risks"]))
    check("every risk carries the facts it was computed from",
          all(r["facts"] for t in out.values() for r in t["risks"]))
    check("every fact has a known provenance",
          all(f["provenance"] in ev.PROVENANCE for t in out.values() for r in t["risks"] for f in r["facts"]))
    check("every risk names where to go", all(r["view"] and r["module"] for t in out.values() for r in t["risks"]))

    print()
    print("=" * 74)
    print("2. THE RULES FIRE WHERE THEY SHOULD")
    print("=" * 74)
    late = by_key.get("order.ORD-B-9")
    check("B: the order past its date is LIKELY, and says why",
          late is not None and late["likelihood"] == ev.LIKELY and "due date has passed" in late["rule"],
          str(late))
    check("B: ...sized by the units still to ship", late and late["impact_units"] == 400, str(late["impact_units"]))
    machine = next((r for r in b["risks"] if r["key"].startswith("machine.")), None)
    check("B: the machine over the rule-score threshold is LIKELY",
          machine is not None and machine["likelihood"] == ev.LIKELY
          and f"at or above the {MACHINE_LIKELY_SCORE}" in machine["rule"], str(machine))
    check("B: ...and it is CNC-01, the machine in breakdown", machine and "CNC-01" in machine["title"],
          str(machine["title"]) if machine else "")
    check("B: overdue maintenance is on the radar",
          by_key.get("maintenance.overdue", {}).get("likelihood") == ev.LIKELY, str(by_key.get("maintenance.overdue")))
    stock = next((r for r in b["risks"] if r["key"].startswith("stock.")), None)
    check("B: the item with nothing on hand is LIKELY, and says there is no stock",
          stock is not None and stock["likelihood"] == ev.LIKELY and "no stock on hand" in stock["rule"], str(stock))
    check("B: ...and no units are claimed for it, because AMP has no measured link to output",
          stock["impact_units"] is None, str(stock.get("impact_units")))
    soon = by_key.get("order.ORD-B-10")
    check("B: an order that is reachable at the measured rate is not LIKELY",
          soon is not None and soon["likelihood"] in (ev.WATCH, ev.POSSIBLE)
          and "against a measured" in soon["rule"], str(soon))
    check("A: a healthy plant has nothing LIKELY",
          not [r for r in a["risks"] if r["likelihood"] == ev.LIKELY],
          str([(r["key"], r["likelihood"]) for r in a["risks"]]))
    check("A: ...and the headline says so", "Nothing is likely" in a["headline"], a["headline"])

    print()
    print("=" * 74)
    print("3. NO PREDICTION IS CLAIMED")
    print("=" * 74)
    text = json.dumps(out, default=str).lower()
    for word in ("probability", "ml model", "predicted", "forecasted", "% chance"):
        check(f"the radar never says {word!r}", word not in text)
    # "machine learning" appears only where AMP DENIES it.
    claims = [seg for seg in text.split("machine learning")[:-1] if not seg.endswith("not ")]
    check("'machine learning' appears only as a denial, never as a claim", not claims,
          str([s[-60:] for s in claims]))
    check("the machine risk says in its own fact that it is a hand-weighted rule",
          any("not machine learning" in (f.get("detail") or "") for f in machine["facts"]), str(machine["facts"]))
    score_fact = next((f for f in machine["facts"] if f["key"].endswith(".score")), None)
    check("the machine SCORE itself is labelled RULE-BASED ASSESSMENT",
          score_fact is not None and score_fact["provenance"] == ev.RULE, str(score_fact))
    # AMP's one adopted model is evaluated on synthetic machines only (ADR-0020).
    # Nothing on this card may carry its label, on any factory.
    model_facts = [f["key"] for t in out.values() for r in t["risks"] for f in r["facts"]
                   if f["provenance"] == ev.MODEL]
    check("no risk fact claims to be a MODEL ESTIMATE: no model is used here", not model_facts,
          str(model_facts))
    check("the card's note says these are rules over measured data, not a model",
          "rule over measured data" in b["note"] and "None of it is a prediction from a trained model" in b["note"],
          b["note"])

    print()
    print("=" * 74)
    print("4. A RATE IS MEASURED, NOT ASSUMED")
    print("=" * 74)
    rows = [r for rs in F.PRODUCTION[F.B].values() for r in rs]
    expected = round(sum(r[5] for r in rows) / 7, 1)
    check("B: the rate is the plant's own good units a day over the window",
          b["measured_rate_per_day"] == expected, f"{b['measured_rate_per_day']} vs {expected}")
    empty = session(seed=False)
    db = empty()
    tok = tenancy.set_current_tenant("EMPTY")
    try:
        db.add(models.CustomerOrder(tenant_code="EMPTY", order_no="ORD-1", customer_name="X", product_name="P",
                                    order_quantity=100, dispatched_quantity=0,
                                    due_date=datetime.utcnow().date(), status="Pending"))
        db.commit()
        blank = build_risk_radar(db, "EMPTY")
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    check("with no production recorded, the radar says it cannot judge the date",
          blank["state"] == ev.INSUFFICIENT_HISTORY and blank["measured_rate_per_day"] is None
          and "cannot say" in blank["headline"], f"{blank['state']} {blank['headline']}")

    print()
    print("=" * 74)
    print("5. LIKELIHOOD WORDS, AND THE ORDER")
    print("=" * 74)
    check("every likelihood is one of the three words",
          all(r["likelihood"] in ev.LIKELIHOOD for t in out.values() for r in t["risks"]))
    order = [ev.LIKELIHOOD.index(r["likelihood"]) for r in b["risks"]]
    check("LIKELY comes before POSSIBLE comes before WATCH", order == sorted(order), str(order))

    print()
    print("=" * 74)
    print("6. MONEY, AND ISOLATION")
    print("=" * 74)
    text_b = json.dumps(b, default=str)
    check("B has no unit value: no money figure and no symbol",
          CURRENCY not in text_b and all(r["impact_money"] is None for r in b["risks"]),
          str([(r["key"], r["impact_money"]) for r in b["risks"]]))
    db = Session()
    tok = tenancy.set_current_tenant(F.A)
    try:
        db.add(models.CustomerOrder(tenant_code=F.A, order_no="ORD-A-LATE", customer_name="Aurora Foods",
                                    product_name="FG-001", order_quantity=500, dispatched_quantity=0,
                                    due_date=datetime.utcnow().date() - timedelta(days=2),
                                    status="Pending"))
        db.commit()
        priced = build_risk_radar(db, F.A)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    late_a = next((r for r in priced["risks"] if r["key"] == "order.ORD-A-LATE"), None)
    check("A has a unit value: the same risk carries money",
          late_a is not None and late_a["impact_money"] == round(500 * F.UNIT_VALUE[F.A])
          and late_a["currency"] == CURRENCY, str(late_a))
    for tenant, card in out.items():
        blob = json.dumps(card, default=str)
        leaks = [f"{o}:{m}" for o, ms in F.MARKERS.items() if o != tenant for m in ms if m in blob]
        check(f"{tenant}: no other factory's data", not leaks, str(leaks))

    print()
    print("=" * 74)
    print("7. ONE INSTANT FOR EVERY RULE")
    print("=" * 74)
    # `now` used to reach the order rule's own arithmetic and generated_at, while
    # the delivery outlook the rule READS judged the order book at the wall
    # clock. A replay of a past day therefore saw that day's arithmetic over
    # today's states. Far from the real clock, so nothing here passes by the
    # day it runs on: an order due 2030-01-10 is past its date on the 11th and
    # not on the 9th — and only if delivery is judged at the instant named.
    due = date(2030, 1, 10)
    db = Session()
    tok = tenancy.set_current_tenant(F.A)
    try:
        db.add(models.CustomerOrder(tenant_code=F.A, order_no="ORD-A-2030", customer_name="Aurora Foods",
                                    product_name="FG-001", order_quantity=300, dispatched_quantity=0,
                                    due_date=due, status="Pending"))
        db.commit()
        after = build_risk_radar(db, F.A, now=datetime(2030, 1, 11, 9, 0, 0))
        before = build_risk_radar(db, F.A, now=datetime(2030, 1, 9, 9, 0, 0))
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    past = next((r for r in after["risks"] if r["key"] == "order.ORD-A-2030"), None)
    check("judged the day after its date, the order is already past it (a fact, LIKELY)",
          past is not None and past["likelihood"] == ev.LIKELY and "past its date" in past["title"],
          str(past))
    early = next((r for r in before["risks"] if r["key"] == "order.ORD-A-2030"), None)
    check("judged the day before its date, it is not",
          early is None or "past its date" not in early["title"], str(early))
    check("the card is stamped with the instant it was judged at",
          after["generated_at"].startswith("2030-01-11") and before["generated_at"].startswith("2030-01-09"),
          f"{after['generated_at']} / {before['generated_at']}")

    # ── 7. every risk ends in something to do (ADR-0039) ───────────
    print()
    print("=" * 74)
    print("7. EVERY RISK ENDS IN SOMETHING TO DO, AND A PROPOSAL ONLY WHERE AMP CAN ACT")
    print("=" * 74)
    every = [r for t in F.TENANTS for r in out[t]["risks"]]
    check("there are risks to check", len(every) >= 3, str(len(every)))
    without = [r["key"] for r in every if not (r.get("action") or "").strip()]
    check("every risk says what to do about it", not without, str(without))

    # A proposal is the stricter thing: it must name a kind AMP can execute, and
    # a machine. "Chase the customer" and "call the supplier" are not AMP's to
    # do, and a button for them would be a promise it cannot keep.
    proposals = [(r["key"], r["propose"]) for r in every if r.get("propose")]
    check("something is proposable", bool(proposals), "no risk offered a proposal")
    bad_kind = [k for k, pr in proposals if pr.get("kind") not in ev.PROPOSABLE_KINDS]
    check("every proposal names a kind AMP can carry out", not bad_kind, str(bad_kind))
    bad_machine = [k for k, pr in proposals if not isinstance(pr.get("machine_id"), int)]
    check("every proposal names a machine by id", not bad_machine, str(bad_machine))
    check("only MACHINE risks are proposable - the rest are advice, not buttons",
          all(k.startswith("machine.") for k, _ in proposals), str([k for k, _ in proposals]))

    # The machine id is the tenant's own. A proposal carrying another factory's
    # machine would hand the write route an id it must refuse -- the radar
    # should never offer it in the first place.
    for t in F.TENANTS:
        db = Session()
        tok = tenancy.set_current_tenant(None)
        own = {m.id for m in db.query(models.Machine).filter(models.Machine.tenant_code == t).all()}
        tenancy.reset_current_tenant(tok)
        db.close()
        offered = {r["propose"]["machine_id"] for r in out[t]["risks"] if r.get("propose")}
        check(f"{t}: every proposed machine is its own", offered <= own, f"{offered} vs {own}")

    # A kind outside the closed set is refused at construction, so a future risk
    # family cannot invent one.
    from ai import risk_radar as rr
    refused = False
    try:
        rr._risk("k", "t", "d", ev.WATCH, "r", "now", "m", "v", [],
                 propose={"kind": "send_email", "machine_id": 1})
    except ValueError:
        refused = True
    check("a risk cannot propose a kind AMP has no path for", refused,
          "send_email was accepted as a proposable action")

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
