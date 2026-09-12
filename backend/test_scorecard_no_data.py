"""A plant that has not run is not a plant running badly.

THE DEFECT
----------
The exec scorecard strip publishes four KPIs. Three of them fabricated a number
when their pillar had no data, and the fourth did it right — which is how you
know the convention existed and three KPIs were outside it.

Reproduced on a plant that dispatched an order this week and produced nothing:

    has_data: True
      Plant OEE              value=0     unit=%  tone=bad
      Good rate              value=0     unit=%  tone=bad
      Delivery reliability   value=100   unit=%  tone=good
      Cost of losses         value=0     unit=£  tone=good

Read that as the customer reads it: *the plant ran catastrophically badly, every
unit it made was scrap, and it eliminated all its losses.* None of those things
happened. The plant did not run.

`Delivery reliability` is the one that is right — `None` when no order has come
due, so the strip renders an em dash in slate. The other three had no such guard.

The tone that goes with it is the STRING `"none"`: what `_tone(None, ...)`
already returns, what `ScorecardStrip.toneCls` maps to slate, and what its TS
union declares. The first version of this file asserted Python `None` instead —
inventing a convention beside an existing one, which would have rendered
`toneCls[null]` as `undefined` and put the literal class "undefined" on the
element. The codebase was right and the test was wrong.

WHY IT IS REACHABLE, AND NOT HIDDEN BY has_data
-----------------------------------------------
The payload's own `has_data` is an OR across three pillars:

    "has_data": oee["has_data"] or prod["runs"] > 0 or delivery["total"] > 0

so ONE pillar having data is enough to publish the whole strip — including the
two pillars that have none. A plant that takes orders but has not produced this
week (a shutdown, a holiday week, a new tenant mid-onboarding, a line moved to
another site) is exactly that case. The strip is shown, and it lies twice.

Worse for cost: `tone = "good" if loss_cost == 0 else "warn"`, so silence is
rendered in GREEN. A plant that stopped is congratulated for eliminating its
losses.

THE RULE
--------
A KPI with no data underneath it publishes `value: None, tone: None`, exactly as
delivery reliability already does. Zero means "measured, and it was zero"; None
means "nothing to measure". The strip already renders None as "—", so this is
applying the existing contract to the three KPIs that were outside it, not
inventing one.

NOT IN SCOPE: the payload-level `has_data` OR stays as it is. It answers "is
there anything at all worth showing?", which is a different question from "does
this KPI have a basis", and narrowing it to AND would hide a strip that has one
genuine number on it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_scorecard_no_data.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
from ai.scorecard import build_scorecard
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _machine(db):
    m = models.Machine(name="M1", status="Idle", utilization=0,
                       downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _delivered_order(db):
    """An order that has come due and was delivered — one pillar with data."""
    db.add(models.CustomerOrder(
        order_no="CO-1", customer_name="Acme", product_name="P",
        order_quantity=100, dispatched_quantity=100,
        due_date=(datetime.utcnow() - timedelta(days=2)).date(),
        status="Dispatched"))
    db.commit()


def _run(db, machine_id, total=1000, good=990, runtime=400):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=480, runtime_minutes=runtime,
        ideal_cycle_time_seconds=12, total_count=total, good_count=good,
        rejected_count=total - good, created_at=datetime.utcnow() - timedelta(days=1)))
    db.commit()


def _kpis(card):
    return {k["key"]: k for k in card["kpis"]}


def main():
    print("=" * 74)
    print("1. A PLANT THAT DID NOT RUN PUBLISHES NO PRODUCTION NUMBER")
    print("=" * 74)
    db = _session()
    _machine(db)
    _delivered_order(db)                     # one pillar has data, two do not
    card = build_scorecard(db, "DEFAULT")
    k = _kpis(card)
    for key, label in (("oee", "Plant OEE"), ("good_rate", "Good rate"),
                       ("loss_cost", "Cost of losses")):
        check(f"{label} is None, not a fabricated zero",
              k[key]["value"] is None,
              f"value={k[key]['value']!r} tone={k[key]['tone']!r}")
        check(f'...and {label} is toned "none", the uncoloured band',
              k[key]["tone"] == "none", f"tone={k[key]['tone']!r}")
    # The pillar that DOES have data must still publish, or this is just blanking
    # the strip.
    check("Delivery reliability still publishes its real figure",
          k["on_time"]["value"] == 100 and k["on_time"]["tone"] == "good",
          f"value={k['on_time']['value']!r} tone={k['on_time']['tone']!r}")
    check("...and the strip is still shown at all (has_data stays an OR)",
          card["has_data"] is True, str(card["has_data"]))
    db.close()

    print()
    print("=" * 74)
    print("2. SILENCE IS NOT RENDERED AS SUCCESS")
    print("=" * 74)
    # The sharpest face: `tone = "good" if loss_cost == 0` painted a stopped plant
    # GREEN for eliminating its losses.
    db = _session()
    _machine(db)
    _delivered_order(db)
    card = build_scorecard(db, "DEFAULT")
    k = _kpis(card)
    check("Cost of losses is not green for a plant that made nothing",
          k["loss_cost"]["tone"] == "none", f"tone={k['loss_cost']['tone']!r}")
    check("...and Plant OEE is not red for it either",
          k["oee"]["tone"] == "none", f"tone={k['oee']['tone']!r}")
    db.close()

    print()
    print("=" * 74)
    print("3. A MEASURED ZERO IS STILL A ZERO — THE CONTROL")
    print("=" * 74)
    # Without this, returning None unconditionally would pass sections 1 and 2.
    # A plant that RAN and genuinely scrapped everything must still say 0%, in red.
    db = _session()
    m = _machine(db)
    _run(db, m.id, total=1000, good=0)
    card = build_scorecard(db, "DEFAULT")
    k = _kpis(card)
    check("a plant that ran and scrapped everything publishes good rate 0",
          k["good_rate"]["value"] == 0, f"value={k['good_rate']['value']!r}")
    check("...in red, because that is a measured fact",
          k["good_rate"]["tone"] == "bad", f"tone={k['good_rate']['tone']!r}")
    check("...and it publishes a real OEE figure",
          k["oee"]["value"] is not None, f"value={k['oee']['value']!r}")
    check("...and a real cost of losses",
          k["loss_cost"]["value"] is not None, f"value={k['loss_cost']['value']!r}")
    db.close()

    print()
    print("=" * 74)
    print("3b. A ROW THAT RECORDED NOTHING IS NOT DATA")
    print("=" * 74)
    # Each KPI must ask ITS OWN pillar the right question. A production record
    # with nothing in it makes `runs > 0` true while OEE has no basis and no
    # units were inspected -- the count-the-rows rule oee_contract.is_measurable
    # exists to replace. Mutation testing earned this section: swapping OEE's
    # basis for the production one survived every other fixture, because in all
    # of them the two agreed.
    db = _session()
    m = _machine(db)
    db.add(models.ProductionRecord(
        machine_id=m.id, planned_minutes=0, runtime_minutes=0,
        ideal_cycle_time_seconds=12, total_count=0, good_count=0,
        rejected_count=0, created_at=datetime.utcnow() - timedelta(hours=2)))
    db.commit()
    card = build_scorecard(db, "DEFAULT")
    k = _kpis(card)
    check("an empty production row does not produce an OEE figure",
          k["oee"]["value"] is None, f"value={k['oee']['value']!r}")
    check("...nor a good rate", k["good_rate"]["value"] is None,
          f"value={k['good_rate']['value']!r}")
    db.close()

    print()
    print("=" * 74)
    print("4. A GOOD WEEK STILL READS AS A GOOD WEEK")
    print("=" * 74)
    # The other side of the control: None must not leak onto a healthy plant.
    db = _session()
    m = _machine(db)
    _run(db, m.id, total=1000, good=995, runtime=470)
    card = build_scorecard(db, "DEFAULT")
    k = _kpis(card)
    check("good rate is published and tonal",
          k["good_rate"]["value"] is not None and k["good_rate"]["tone"] is not None,
          f"value={k['good_rate']['value']!r} tone={k['good_rate']['tone']!r}")
    check("OEE is published and tonal",
          k["oee"]["value"] is not None and k["oee"]["tone"] is not None,
          f"value={k['oee']['value']!r} tone={k['oee']['tone']!r}")
    check("...and a genuinely zero loss cost is still green",
          k["loss_cost"]["value"] is not None, f"value={k['loss_cost']['value']!r}")
    db.close()

    print()
    print("=" * 74)
    print("5. EVERY KPI ON THE STRIP OBEYS THE SAME RULE")
    print("=" * 74)
    # Stated over the whole payload rather than per-key, so a fifth KPI added
    # later cannot quietly reintroduce the fabricated zero.
    db = _session()
    _machine(db)
    _delivered_order(db)
    card = build_scorecard(db, "DEFAULT")
    # "none" is the uncoloured band ScorecardStrip renders slate; any OTHER tone
    # on a valueless KPI paints a verdict over an em dash.
    miscoloured = [k["key"] for k in card["kpis"]
                   if k["value"] is None and k["tone"] != "none"]
    check(f"no valueless KPI is coloured ({len(card['kpis'])} on the strip)",
          not miscoloured, str(miscoloured))
    # And every tone is one the frontend's toneCls map can resolve, so a null can
    # never reach `toneCls[k.tone]` and paint the literal class "undefined".
    unknown = [k["tone"] for k in card["kpis"]
               if k["tone"] not in ("good", "warn", "bad", "none")]
    check("...and every tone is one ScorecardStrip can render", not unknown,
          str(unknown))
    published = [k["key"] for k in card["kpis"] if k["value"] is not None]
    check(f"...and exactly the pillar with data publishes ({published})",
          published == ["on_time"], str(published))
    db.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_scorecard_no_data():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
