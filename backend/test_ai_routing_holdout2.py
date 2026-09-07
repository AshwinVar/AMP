"""A SECOND held-out routing set, because the first one is spent.

WHY A SECOND ONE
----------------
`test_ai_evaluation.py` section 1c and `test_ai_routing_holdout.py` both say the
same thing: a question set stops being held out the moment it is in the
repository, because anyone improving the router can read it.

    "THIS SET IS NOW BURNED... To measure a real improvement, write fresh
     questions and score them BEFORE touching the router."

Set one has now been read by whoever is working on routing, so it can measure a
regression but it can no longer measure an improvement. This is a fresh 52 for
the next change to be judged by. Set one stays exactly where it is — two
independent samples of the same population are better evidence than one, and
deleting the older one would discard a baseline.

THE PROTOCOL, FIXED BEFORE ANY QUESTION WAS SCORED
--------------------------------------------------
1. The pillar list came from `assistant.route_names()` AT RUNTIME. The keyword
   table (`_ROUTES`) was deliberately NOT read while these questions were being
   written — the point is to sample phrasings, not to write around a lookup.
2. All 52 were written in one pass, four per pillar, BEFORE any was run.
3. The split is mechanical and declared here: EVEN index -> TUNE,
   ODD index -> HOLDOUT. It cannot be re-drawn after seeing results.
4. The tune half prints its misses by name. **The held-out half prints only a
   total.** A question nobody can name is a question nobody can tune to.
5. The floors below were set from the FIRST run and never adjusted upward to
   make a change look good. They are a regression guard, not a target.

`briefing` is excluded: it is the fallthrough, so every unmatched question lands
there and scores as a hit, which would inflate the result with questions the
router cannot get wrong. `help` is excluded as a meta-route about the assistant
rather than about the plant.

WHAT THE FIRST RUN SAID, AND WHY IT MATTERS
--------------------------------------------
Scored once, before anything was touched:

                        tune        held out     gap
    set one (tuned)     22/26 85%   15/26 58%    +27
    set two (fresh)     15/26 58%   15/26 58%     +0

Two independent samples, written months apart by different processes, agree on
the held-out number: **58%**. That is a much steadier estimate than either alone,
and it corrects the figure the roadmap still quotes — `test_ai_evaluation.py`
section 1c said 42%, and now reports 50% on that same set after the vocabulary
work.

The load-bearing line is the gap. Set one's tune half has been tuned against and
sits at 85%. Its held-out half sits at 58% — exactly where a completely fresh set
that has never been tuned against also sits. **So the 27 points of tune-half
improvement bought zero generalisation.** That is what a plateau looks like from
the inside, and it is the strongest evidence yet for the phase-3 case: more
keyword vocabulary makes the questions you can see go green and moves the
questions you cannot see not at all.

It is NOT an argument that a model would do better — that is unmeasured, and
`assistant.answer`'s `chosen_route` seam exists precisely so it can be measured
before it is believed. It is an argument that continuing to tune the table is
spending effort on a number that has stopped meaning anything.

WHAT THIS IS AND IS NOT EVIDENCE FOR
-------------------------------------
Not a clean-room measurement, and for a sharper reason than set one's: these
were written by the same author who has been reading this codebase all session.
Vocabulary bleed is likely and the absolute percentage is optimistic against a
real plant manager who has never seen AMP.

What survives that is the DELTA. The bias applies equally before and after a
router change, so a movement in the half nobody inspected is real evidence even
though the level flatters. Anyone quoting the absolute number as "AMP routes N%
of real questions correctly" is misreading this file.

One caveat on the level, recorded because it cuts the other way: two misses in
the tune half went to `find` and several to `briefing`, and neither is a wrong
ANSWER — `briefing` is the honest fallback and `find` is a real handler. A
question routed to the fallback gets a useful reply; it is scored a miss here
because it did not reach the pillar that would answer it best. So 58% understates
usefulness while overstating precision against a naive user.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_routing_holdout2.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import assistant
from database import Base

T = "ROUTEVAL2"
failures = []

# 52 questions, four per pillar, written before any was scored and before the
# keyword table was opened. Order is fixed: the even/odd split below depends on
# it, so REORDERING THIS LIST INVALIDATES THE SPLIT and silently turns held-out
# questions into tuned ones. Append only.
QUESTIONS = [
    # downtime — unplanned stoppages and why
    ("what keeps breaking on the shop floor?", "downtime"),
    ("how much production time did we lose to faults this week?", "downtime"),
    ("is the same machine failing over and over?", "downtime"),
    ("which machine is costing us the most hours stopped?", "downtime"),
    # delivery — customer orders and dispatch
    ("can we promise the customer end of week?", "delivery"),
    ("which orders are we going to be short on?", "delivery"),
    ("how much of the order book has gone out the door?", "delivery"),
    ("is anything sitting waiting to be sent to a customer?", "delivery"),
    # quality — rejects, scrap, inspection
    ("are we throwing away more than usual?", "quality"),
    ("which part is failing inspection most?", "quality"),
    ("what is our first-pass yield?", "quality"),
    ("how much did we have to rework?", "quality"),
    # oee — overall equipment effectiveness
    ("how well are the machines actually running?", "oee"),
    ("what is our overall equipment effectiveness right now?", "oee"),
    ("are we getting the most out of the presses?", "oee"),
    ("how much of our capacity are we actually using?", "oee"),
    # production — output
    ("how many parts did we make yesterday?", "production"),
    ("how many units came off the line last week?", "production"),
    ("what is our output looking like?", "production"),
    ("are we producing enough to keep up with demand?", "production"),
    # maintenance — planned and preventive work
    ("what servicing is due?", "maintenance"),
    ("is anything overdue for a service?", "maintenance"),
    ("what preventive work is still outstanding?", "maintenance"),
    ("how far behind is the maintenance team?", "maintenance"),
    # inventory — stock levels and reorder
    ("what are we about to run out of?", "inventory"),
    ("do we have enough raw material for next week?", "inventory"),
    ("which items need reordering?", "inventory"),
    ("how much stock is sitting on the shelves?", "inventory"),
    # cost — spend and unit economics
    ("where is the money going?", "cost"),
    ("how much are we spending on materials?", "cost"),
    ("what is our cost per unit?", "cost"),
    ("is the plant getting more expensive to run?", "cost"),
    # machines — the asset list and its state
    ("give me the state of each machine", "machines"),
    ("which machines are running right now?", "machines"),
    ("how many machines do we have on the floor?", "machines"),
    ("what is the condition of the equipment?", "machines"),
    # shift — crew performance
    ("how did nights do compared to days?", "shift"),
    ("which crew is performing best?", "shift"),
    ("did the evening team hit target?", "shift"),
    ("how efficient was the last shift?", "shift"),
    # flow — WIP and bottlenecks
    ("where is work piling up?", "flow"),
    ("what is the bottleneck?", "flow"),
    ("is anything stuck between operations?", "flow"),
    ("how much work in progress is on the floor?", "flow"),
    # compliance — controlled documents
    ("are our documents up to date?", "compliance"),
    ("what paperwork needs reviewing?", "compliance"),
    ("are we ready for an audit?", "compliance"),
    ("which procedures have expired?", "compliance"),
    # trend — movement over time
    ("are things getting better or worse?", "trend"),
    ("how does this month compare with last?", "trend"),
    ("what direction are we heading?", "trend"),
    ("show me the movement over the last few weeks", "trend"),
]

TUNE = [q for i, q in enumerate(QUESTIONS) if i % 2 == 0]
HOLDOUT = [q for i, q in enumerate(QUESTIONS) if i % 2 == 1]

# Set from the FIRST run of this file, before the router was touched. Raise them
# only when a real improvement moves the held-out half; never lower them to make
# a build pass, and never raise the held-out floor to match a tuned result.
TUNE_FLOOR = 15
HOLDOUT_FLOOR = 15


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    """A plant with enough of everything that no pillar answers from an empty
    table — a read-model with nothing to say could route correctly and still
    look wrong, and this measures ROUTING, not phrasing."""
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
        db.add(models.Machine(tenant_code=T, name=f"CNC-{i:02d}", site="P1",
                              status="Breakdown" if i == 0 else "Running",
                              utilization=70 + i, downtime="10 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
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
    print("SET TWO — TUNE HALF: misses are named, because this half is for tuning")
    print("=" * 74)
    tune_hits = 0
    for question, expected in TUNE:
        got = route(Session, question)
        hit = got == expected
        tune_hits += hit
        print(f"  {'hit ' if hit else 'MISS'}  {question[:44]:<46} {expected:<12} -> {got}")

    print()
    print("=" * 74)
    print("SET TWO — HELD-OUT HALF: the total ONLY. Which ones missed is not")
    print("printed, on purpose — a question you cannot name is one you cannot tune")
    print("=" * 74)
    hold_hits = sum(route(Session, q) == e for q, e in HOLDOUT)

    tp = 100.0 * tune_hits / len(TUNE)
    hp = 100.0 * hold_hits / len(HOLDOUT)
    print()
    print(f"  TUNE:     {tune_hits:2d}/{len(TUNE)}  ({tp:.0f}%)")
    print(f"  HELD OUT: {hold_hits:2d}/{len(HOLDOUT)}  ({hp:.0f}%)   <- the number that means something")
    print()
    gap = tp - hp
    print(f"  gap: {gap:+.0f} points. A LARGE POSITIVE GAP MEANS OVERFITTING —")
    print("  the router learned these questions, not this kind of question.")

    print()
    print("=" * 74)
    print("FLOORS (regression guard, not a target)")
    print("=" * 74)
    for label, hits, total, floor in (("tune", tune_hits, len(TUNE), TUNE_FLOOR),
                                      ("held-out", hold_hits, len(HOLDOUT), HOLDOUT_FLOOR)):
        ok = hits >= floor
        if not ok:
            failures.append(f"{label} routing {hits}/{total} is BELOW its floor of {floor}")
        print(f"  {'PASS' if ok else 'FAIL'}  {label} routing holds its floor of {floor}/{total}"
              + ("" if ok else f"   [{hits}/{total} — the router got WORSE]"))

    print()
    print("=" * 74)
    print("THE SET ITSELF")
    print("=" * 74)
    check("52 questions", len(QUESTIONS) == 52, str(len(QUESTIONS)))
    check("split evenly, 26 and 26",
          len(TUNE) == 26 and len(HOLDOUT) == 26,
          f"{len(TUNE)}/{len(HOLDOUT)}")
    labels = {e for _, e in QUESTIONS}
    check("13 pillars, four questions each",
          len(labels) == 13 and all(
              sum(1 for _, e in QUESTIONS if e == p) == 4 for p in labels),
          str(sorted(labels)))
    # The fallthrough must not be a label, or unmatched questions score as hits.
    check("'briefing' is not an expected answer anywhere",
          "briefing" not in labels)
    # Every label must be a route the assistant can actually return, or a
    # question is unanswerable by construction and the floor is meaningless.
    known = set(assistant.route_names())
    check("every expected pillar is a real route", labels <= known,
          str(sorted(labels - known)))
    # And this set must not be set one wearing a hat.
    import test_ai_routing_holdout as set_one
    overlap = {q for q, _ in QUESTIONS} & {q for q, _ in set_one.QUESTIONS}
    check("no question is shared with set one", not overlap, str(overlap))

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


def test_ai_routing_holdout2():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
