"""The Root-Cause Explorer explains what it can, and says what it cannot (ADR-0025).

Run against the three-factory evaluation environment. What is pinned:

  1. THE GAP. FACTORY_B came 1,200 units short of the plans that came due; the
     explorer states the gap from the plan's own numbers. FACTORY_C has no plan,
     so there is no gap to explain and it says NOT CONFIGURED instead of zero.
  2. THE MECHANISMS ARE ARITHMETIC. Scrap is the recorded rejects; slow running
     is what the runtime could have made at the machines' own ideal cycle minus
     what it made; logged stoppages are the recorded minutes valued at that same
     cycle. Each is computed here, independently, from the fixture's numbers.
  3. THE LABELS MEAN SOMETHING. A measured mechanism is CAUSE CONFIRMED; a
     reason's share of stoppage minutes is a LIKELY CONTRIBUTOR; a stock-out with
     no measured link is a CORRELATED EVENT; non-running time with no reason
     logged is INSUFFICIENT EVIDENCE.
  4. THE REMAINDER IS STATED. The part of the gap the measured losses do not
     account for is reported as a figure and labelled UNKNOWN, never absorbed
     into the nearest cause.
  5. TWO DENOMINATORS, SAID OUT LOUD. The card carries the note that the gap and
     the losses are measured against different things.
  6. MONEY only where a unit value is set, and never otherwise.
  7. TENANT ISOLATION across all three factories.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_root_cause.py
"""
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tenancy
from ai import evidence as ev
from ai.root_cause import explain_production_gap
from copilot_eval import fixtures as F
from currency import CURRENCY
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


def explain(Session, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return explain_production_gap(db, tenant)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def oracle(tenant):
    """The mechanisms, computed here from the seed spec by the textbook formula."""
    rows = [r for rs in F.PRODUCTION[tenant].values() for r in rs]
    quality = sum(r[4] - r[5] for r in rows)                                  # total - good
    performance = sum(max(0, int(r[2] * 60 / r[3]) - r[4]) for r in rows)     # runtime at ideal, minus made
    availability_min = sum(max(0, r[1] - r[2]) for r in rows)
    availability_units = sum(int(max(0, r[1] - r[2]) * 60 / r[3]) for r in rows)
    logged = sum(x[2] for x in F.DOWNTIME[tenant])
    plans = F.PLANS[tenant]
    gap = max(0, sum(p[3] for p in plans) - sum(p[4] for p in plans)) if plans else None
    return {"quality": quality, "performance": performance, "availability_minutes": availability_min,
            "availability_units": availability_units, "logged_minutes": logged, "gap": gap}


def main():
    Session = session()
    out = {t: explain(Session, t) for t in F.TENANTS}
    b, c, a = out[F.B], out[F.C], out[F.A]
    ob = oracle(F.B)

    print("=" * 74)
    print("1. THE GAP")
    print("=" * 74)
    check("B: the gap comes from the plans that came due", b["gap"]["gap_units"] == ob["gap"] == 1200,
          f"{b['gap']} oracle={ob['gap']}")
    check("B: ...stated in the headline", "1,200 units short" in b["headline"], b["headline"])
    check("B: every machine reported, so the card is OK even though reasons are missing",
          b["state"] == ev.OK and b["coverage"]["complete"] is True and b["unattributed_units"] > 0,
          f"{b['state']} {b['coverage']}")
    check("C: a machine that never reported makes it PARTIAL DATA",
          c["state"] == ev.PARTIAL_DATA and c["coverage"]["complete"] is False, f"{c['state']} {c['coverage']}")
    check("C: no plan is set, so there is no gap and it says so",
          c["gap"]["state"] == ev.NOT_CONFIGURED and c["gap"]["gap_units"] is None
          and "No production plan is set" in c["headline"], f"{c['gap']} {c['headline']}")
    check("A: the plans due were met, so the gap is zero, and the losses are still reported",
          a["gap"]["gap_units"] == 0 and a["measured_loss_units"] > 0, f"{a['gap']} {a['measured_loss_units']}")

    print()
    print("=" * 74)
    print("2. THE MECHANISMS ARE ARITHMETIC")
    print("=" * 74)
    by_key = {x["key"]: x for x in b["contributors"]}
    check("B: scrap equals the recorded rejects", by_key["quality"]["units"] == ob["quality"] == 250,
          f"{by_key['quality']['units']} vs {ob['quality']}")
    check("B: slow running equals runtime-at-ideal minus units made",
          by_key["performance"]["units"] == ob["performance"] == 450,
          f"{by_key['performance']['units']} vs {ob['performance']}")
    check("B: the logged stoppage minutes are the recorded ones",
          by_key["downtime_logged"]["minutes"] == ob["logged_minutes"] == 285,
          f"{by_key['downtime_logged']['minutes']} vs {ob['logged_minutes']}")
    share = min(1.0, ob["logged_minutes"] / ob["availability_minutes"])
    check("B: ...valued at the ideal cycle, by their share of the non-running time",
          by_key["downtime_logged"]["units"] == int(round(ob["availability_units"] * share)),
          f"{by_key['downtime_logged']['units']} vs {int(round(ob['availability_units'] * share))}")
    check("B: attributed = slow running + scrap + logged stoppages",
          b["attributed_units"] == by_key["performance"]["units"] + by_key["quality"]["units"]
          + by_key["downtime_logged"]["units"], str(b["attributed_units"]))
    check("B: measured loss also counts the non-running time with no reason",
          b["measured_loss_units"] == ob["performance"] + ob["quality"] + ob["availability_units"]
          and b["unattributed_units"] == ob["availability_units"] - by_key["downtime_logged"]["units"],
          f"measured={b['measured_loss_units']} unattributed={b['unattributed_units']}")
    check("B: the headline reports BOTH, so an unlogged plant cannot read as explained",
          f"{b['measured_loss_units']:,} units of lost capacity" in b["headline"]
          and "with no reason recorded" not in b["headline"]
          and "carries no stoppage reason" in b["headline"], b["headline"])
    worst = b["by_machine"][0]
    check("B: the worst machine is named with its own losses", worst["machine"] == "CNC-01" and worst["units"] > 0,
          str(worst))

    print()
    print("=" * 74)
    print("3. THE LABELS MEAN SOMETHING")
    print("=" * 74)
    check("B: a measured mechanism is CAUSE CONFIRMED",
          by_key["quality"]["cause_label"] == by_key["performance"]["cause_label"] == ev.CAUSE_CONFIRMED)
    hydraulic = next((x for x in b["contributors"] if "hydraulic" in x["key"]), None)
    check("B: a stoppage reason's share is a LIKELY CONTRIBUTOR, not a confirmed cause",
          hydraulic is not None and hydraulic["cause_label"] == ev.LIKELY_CONTRIBUTOR, str(hydraulic))
    check("B: a stock-out with no measured link is a CORRELATED EVENT, with no units claimed",
          by_key["stock.out"]["cause_label"] == ev.CORRELATED_EVENT and by_key["stock.out"]["units"] is None,
          str(by_key.get("stock.out")))
    unlogged = by_key.get("downtime_unlogged")
    check("B: non-running time with no reason logged is INSUFFICIENT EVIDENCE",
          unlogged is not None and unlogged["cause_label"] == ev.INSUFFICIENT_EVIDENCE, str(unlogged))
    check("B: ...and it reports the minutes it cannot explain",
          unlogged["minutes"] == ob["availability_minutes"] - ob["logged_minutes"], str(unlogged["minutes"]))
    check("every contributor carries a basis and its facts",
          all(x["basis"] and x["facts"] is not None for x in b["contributors"]))
    check("every cause label is one of the four", all(x["cause_label"] in ev.CAUSE_LABELS
                                                      for t in out.values() for x in t["contributors"]))

    print()
    print("=" * 74)
    print("4. THE REMAINDER IS STATED")
    print("=" * 74)
    check("B: the unexplained part of the gap is a figure",
          b["unexplained_units"] == max(0, 1200 - b["attributed_units"]), str(b["unexplained_units"]))
    fact = next((f for f in b["facts"] if f["key"] == "rc.unexplained_units"), None)
    check("B: ...labelled UNKNOWN, not folded into a cause",
          fact is not None and fact["provenance"] == ev.UNKNOWN, str(fact))
    check("B: the attributed share of the gap is reported as a percentage",
          b["attributed_share_of_gap"] == round(min(100, b["attributed_units"] / 1200 * 100)),
          str(b["attributed_share_of_gap"]))
    fact = next((f for f in b["facts"] if f["key"] == "rc.unattributed_units"), None)
    check("B: the capacity with no recorded reason is itself labelled UNKNOWN",
          fact is not None and fact["provenance"] == ev.UNKNOWN and fact["value"] == b["unattributed_units"],
          str(fact))

    print()
    print("=" * 74)
    print("5. TWO DENOMINATORS, SAID OUT LOUD")
    print("=" * 74)
    check("the note says the gap and the losses are measured against different things",
          "different denominators" in b["denominator_note"], b["denominator_note"])

    print()
    print("=" * 74)
    print("6. MONEY ONLY WHERE A RATE IS SET")
    print("=" * 74)
    text_b = json.dumps(b, default=str)
    check("B has no unit value: no money symbol, and no money figure", CURRENCY not in text_b
          and b["measured_loss_money"] is None and b["attributed_money"] is None
          and all(x["money"] is None for x in b["contributors"]),
          str([(x["key"], x["money"]) for x in b["contributors"]]))
    check("A has one: the losses are priced", a["measured_loss_money"] is not None and a["currency"] == CURRENCY,
          str(a["measured_loss_money"]))

    print()
    print("=" * 74)
    print("7. TENANT ISOLATION")
    print("=" * 74)
    for tenant, card in out.items():
        text = json.dumps(card, default=str)
        leaks = [f"{o}:{m}" for o, ms in F.MARKERS.items() if o != tenant for m in ms if m in text]
        check(f"{tenant}: no other factory's data", not leaks, str(leaks))

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
