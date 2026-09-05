"""A held-out routing measurement whose held-out half CANNOT be tuned against.

WHY THIS EXISTS
---------------
`test_ai_evaluation.py` section 1c records the number that matters: keyword
routing sends roughly a third of unseen phrasings to the wrong pillar, and it
says plainly what to do about it —

    "THIS SET IS NOW BURNED. It is in the repository, so it is no longer
     genuinely unseen... To measure a real improvement, write fresh questions
     and score them BEFORE touching the router."

That is what this is. It also records the trap 1c fell into once: an earlier fix
scored 100% on the set it was tuned against and made held-out routing WORSE,
38% -> 23%. Tuning to a number you can see is not improvement; it just moves the
error somewhere you are not looking.

THE PROTOCOL, FIXED BEFORE ANY QUESTION WAS SCORED
--------------------------------------------------
1. 52 questions were written first, four per pillar, in a factory manager's
   words, before running any of them.
2. The split is mechanical and declared here: EVEN index -> TUNE,
   ODD index -> HOLDOUT. It is not a judgement call and cannot be re-drawn
   after seeing results.
3. **This harness prints WHICH tune questions missed, and for the held-out half
   prints only the total.** That is the point. A misrouted held-out question
   cannot be fixed by name, because the harness will not tell anyone its name.
   Discipline that depends on remembering to be disciplined is not discipline.

WHAT THIS SET IS AND IS NOT EVIDENCE FOR
-----------------------------------------
It is NOT a clean-room measurement. These questions were written by someone who
had already read the routing table, so the absolute percentage is optimistic
against a truly naive user. What survives that contamination is the **delta**:
the bias applies equally before and after a change, so an improvement in the
half nobody inspected is real evidence even though the level is flattering.

`briefing` questions are deliberately EXCLUDED. It is the fallback route, so
every unmatched question lands there and scores as a hit; including "what should
I look at first?" would inflate the result with questions the router cannot get
wrong.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_routing_holdout.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import assistant
from database import Base

T = "ROUTEVAL"

# 52 questions, four per pillar, written before any was scored. Order is fixed:
# the even/odd split below depends on it, so REORDERING THIS LIST INVALIDATES
# THE SPLIT and silently turns held-out questions into tuned ones. Append only.
QUESTIONS = [
    # downtime
    ("why did line two keep stopping yesterday?", "downtime"),
    ("what is eating our machine hours?", "downtime"),
    ("how many stoppages did we have last week?", "downtime"),
    ("which fault keeps coming back?", "downtime"),
    # delivery
    ("are we going to miss any customer deadlines?", "delivery"),
    ("what has not shipped yet?", "delivery"),
    ("how many dispatches are behind schedule?", "delivery"),
    ("will the big account get their parts this week?", "delivery"),
    # quality
    ("how many units did we have to scrap?", "quality"),
    ("are we making more bad parts than usual?", "quality"),
    ("what is our reject rate looking like?", "quality"),
    ("which fault is failing inspection most?", "quality"),
    # maintenance
    ("what servicing is overdue?", "maintenance"),
    ("which assets need attention from the engineers?", "maintenance"),
    ("is anything past its planned service date?", "maintenance"),
    ("what jobs are waiting for sign-off?", "maintenance"),
    # oee
    ("how efficiently is the plant running?", "oee"),
    ("what is our equipment effectiveness?", "oee"),
    ("are we getting the most out of the machines?", "oee"),
    ("how good is our availability?", "oee"),
    # inventory
    ("do we have enough raw material?", "inventory"),
    ("what do we need to buy in?", "inventory"),
    ("are we going to run out of anything?", "inventory"),
    ("which parts are below minimum?", "inventory"),
    # cost
    ("what are the losses worth?", "cost"),
    ("how much did stoppages cost us?", "cost"),
    ("where are we bleeding money?", "cost"),
    ("what is the financial impact this week?", "cost"),
    # production
    ("how many units came off the line?", "production"),
    ("what was our output yesterday?", "production"),
    ("are we hitting our build numbers?", "production"),
    ("how much did we manufacture?", "production"),
    # shift
    ("how did the evening team do?", "shift"),
    ("which crew performs best?", "shift"),
    ("did we hit target on nights?", "shift"),
    ("how is shift attainment?", "shift"),
    # trend
    ("are things improving?", "trend"),
    ("how does this week compare with last?", "trend"),
    ("is performance getting better or worse?", "trend"),
    ("what has changed since last week?", "trend"),
    # flow
    ("how much is half-built right now?", "flow"),
    ("what is sitting between operations?", "flow"),
    ("how many jobs are open on the floor?", "flow"),
    ("what is our work in progress?", "flow"),
    # compliance
    ("are our procedures up to date?", "compliance"),
    ("do we have any audit paperwork outstanding?", "compliance"),
    ("which controlled documents need review?", "compliance"),
    ("are we ready for the ISO audit?", "compliance"),
    # machines
    ("is anything broken right now?", "machines"),
    ("what is not running?", "machines"),
    ("which assets are offline?", "machines"),
    ("show me the equipment status", "machines"),
]

TUNE = [(q, e) for i, (q, e) in enumerate(QUESTIONS) if i % 2 == 0]
HOLDOUT = [(q, e) for i, (q, e) in enumerate(QUESTIONS) if i % 2 == 1]

# Recorded floors: the scores measured after the vocabulary pass this harness was
# built to evaluate. They exist so the suite FAILS if a future change makes
# routing worse, which is the failure mode section 1c actually suffered.
#
# THE TWO NUMBERS ARE FAR APART ON PURPOSE, and the gap is the finding rather
# than a defect in the harness. Baseline was tune 13 / held-out 15. Adding
# thirteen words of real factory vocabulary — "broken", "dispatch", "loss",
# "procedure", "stopping", "build", "run out" and the rest — took TUNE from
# 13 to 22 and left HELD-OUT at exactly 15. Nine fixes, zero generalisation:
# the same shape as the failure 1c documents, caught this time because the
# held-out half is unprintable.
#
# Raise these only alongside a measurement, and treat a rise in TUNE that is not
# matched in HELD-OUT as evidence of nothing at all.
TUNE_FLOOR = 22
HOLDOUT_FLOOR = 15

failures = []


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    now = datetime.utcnow()
    for i in range(4):
        m = models.Machine(tenant_code=T, name=f"PRESS-{i:02d}", site="P1",
                           status="Breakdown" if i == 0 else "Running",
                           utilization=60 + i * 5, downtime=f"{i * 9} min")
        db.add(m)
        db.flush()
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, planned_minutes=480, runtime_minutes=400,
            ideal_cycle_time_seconds=30, total_count=600, good_count=560,
            rejected_count=40, created_at=now - timedelta(days=i)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def route(Session, question):
    """The MATCHED PILLAR only. Deliberately not the answer text: this measures
    routing, and nothing here should be read as evidence about phrasing."""
    db = Session()
    tok = tenancy.set_current_tenant(T)
    try:
        return assistant.answer(db, T, question).get("matched")
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session = seed()

    print("=" * 74)
    print("TUNE HALF — misses are named, because this half is for tuning")
    print("=" * 74)
    tune_hits = 0
    for question, expected in TUNE:
        got = route(Session, question)
        hit = got == expected
        tune_hits += hit
        print(f"  {'hit ' if hit else 'MISS'}  {question[:44]:<46} {expected:<12} -> {got}")

    print()
    print("=" * 74)
    print("HELD-OUT HALF — the total ONLY. Which ones missed is not printed,")
    print("on purpose: a question you cannot name is a question you cannot tune")
    print("=" * 74)
    hold_hits = sum(route(Session, q) == e for q, e in HOLDOUT)

    tp = 100.0 * tune_hits / len(TUNE)
    hp = 100.0 * hold_hits / len(HOLDOUT)
    print()
    print(f"  TUNE:     {tune_hits:2d}/{len(TUNE)}  ({tp:.0f}%)")
    print(f"  HELD OUT: {hold_hits:2d}/{len(HOLDOUT)}  ({hp:.0f}%)   <- the number that means something")
    print()
    # A large gap between the two is the overfitting signature: it is what an
    # improvement tuned to the visible half looks like from the outside.
    gap = tp - hp
    print(f"  gap: {gap:+.0f} points. A LARGE POSITIVE GAP MEANS OVERFITTING —")
    print("  the router learned these questions, not this kind of question.")

    print()
    print("=" * 74)
    for label, hits, total, floor in (("tune", tune_hits, len(TUNE), TUNE_FLOOR),
                                      ("held-out", hold_hits, len(HOLDOUT), HOLDOUT_FLOOR)):
        ok = hits >= floor
        if not ok:
            failures.append(f"{label} routing {hits}/{total} is BELOW its floor of {floor}")
        print(f"  {'PASS' if ok else 'FAIL'}  {label} routing holds its floor of {floor}/{total}"
              + ("" if ok else f"   [{hits}/{total} — the router got WORSE]"))

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
