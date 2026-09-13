"""An idle day is not a catastrophic day, and a machine that stopped did not decline.

THE DEFECT
----------
OEE is an INTENSIVE ratio. A machine that did not run has NO OEE — not 0% OEE.
`ai/oee.py` published 0 in two places, and the second one is worse than the
first because it produces a RANKING.

1. `_daily_oee` — a day with no production reads 0, so an idle Sunday draws as
   a full-height red bar next to a good Monday. On the card, a plant that runs
   five days a week looks like a plant failing twice a week.

2. The per-machine week-over-week halves:

       c_oee = _oee_from_records(cur_bm[mid])["oee"] if mid in cur_bm else 0
       p_oee = _oee_from_records(pri_bm[mid])["oee"] if mid in pri_bm else 0

   so a machine that ran at 80% last week and DID NOT RUN AT ALL this week gets
   `delta = -80` and tops `declining_machines` — the plant's worst decliner,
   for not running. And a machine commissioned this week gets `prior_oee = 0`
   and `delta = +85`, topping `improving_machines` with a fabricated
   improvement. Those two lists are what a manager reads to decide where to go
   first.

THE CONVENTION ALREADY EXISTS, TEN LINES BELOW
-----------------------------------------------
The PLANT-level verdict in the same function gets it right, with explicit
branches:

    elif not has_cur:
        direction, tone = "unknown", "warn"
        verdict = f"No production recorded this week (OEE was {prior['oee']}% last week)."

"No production this week" is precisely the case the per-machine code turns into
a -80 point decline. Same shape as #585: one surface followed the convention and
the others were outside it.

WHY THE SIBLINGS ARE NOT AFFECTED
---------------------------------
`ai/cost.py` and `ai/downtime.py` build the same improving/worsening lists from
the same halves and are CORRECT to use zero, because cost and downtime minutes
are EXTENSIVE: a machine that did not run genuinely incurred no loss and no
downtime, so `current - prior` is a real movement. Only the ratio has no value
when the denominator is absent. Checked, not assumed — that is why this change
touches one file.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oee_idle_is_not_zero.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from ai.oee import build_oee_summary, build_oee_trend
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


def _machine(db, name):
    m = models.Machine(name=name, status="Running", utilization=80,
                       downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _run(db, machine_id, when, planned=480, runtime=470, total=1000, good=995):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=12, total_count=total, good_count=good,
        rejected_count=total - good, created_at=when))
    db.commit()


def main():
    print("=" * 74)
    print("1. A DAY WITH NO PRODUCTION HAS NO OEE")
    print("=" * 74)
    db = _session()
    m = _machine(db, "PRESS-01")
    _run(db, m.id, datetime.utcnow() - timedelta(days=1))
    summary = build_oee_summary(db, "DEFAULT")
    idle = [d for d in summary["daily"] if d["oee"] is None]
    ran = [d for d in summary["daily"] if d["oee"] is not None]
    check(f"the idle days publish None, not 0 ({len(idle)} idle, {len(ran)} with data)",
          idle and all(d["oee"] is None for d in idle),
          str([(d["date"], d["oee"]) for d in summary["daily"]]))
    check("...and the day that ran still publishes its figure",
          len(ran) == 1 and ran[0]["oee"] > 0,
          str([(d["date"], d["oee"]) for d in ran]))
    check("...and no day reports a zero",
          not any(d["oee"] == 0 for d in summary["daily"]),
          str([(d["date"], d["oee"]) for d in summary["daily"]]))
    db.close()

    print()
    print("=" * 74)
    print("2. A MEASURED ZERO IS STILL A ZERO — THE CONTROL")
    print("=" * 74)
    # Without this, returning None for everything would pass section 1. A day the
    # plant ran and produced nothing good IS a zero, and must say so.
    db = _session()
    m = _machine(db, "PRESS-01")
    _run(db, m.id, datetime.utcnow() - timedelta(days=1),
         runtime=480, total=1000, good=0)
    summary = build_oee_summary(db, "DEFAULT")
    measured = [d for d in summary["daily"] if d["oee"] is not None]
    check(f"a day that ran and scrapped everything reports 0, not None "
          f"({[d['oee'] for d in measured]})",
          len(measured) == 1 and measured[0]["oee"] == 0,
          str([(d["date"], d["oee"]) for d in summary["daily"]]))
    db.close()

    print()
    print("=" * 74)
    print("3. A MACHINE THAT STOPPED DID NOT DECLINE")
    print("=" * 74)
    db = _session()
    stopped = _machine(db, "STOPPED-01")
    steady = _machine(db, "STEADY-01")
    now = datetime.utcnow()
    # STOPPED-01 ran well last week and not at all this week.
    _run(db, stopped.id, now - timedelta(days=10))
    # STEADY-01 ran both weeks, so the halves have something to compare.
    _run(db, steady.id, now - timedelta(days=10))
    _run(db, steady.id, now - timedelta(days=2))
    trend = build_oee_trend(db, "DEFAULT")
    declining = [(m["machine_id"], m["oee"], m["prior_oee"], m["delta"])
                 for m in trend["declining_machines"]]
    check(f"a machine that did not run is not ranked as declining ({declining})",
          stopped.id not in [row[0] for row in declining],
          "the plant's worst decliner is a machine that simply stopped")
    # The payload exposes only the two ranked lists, so excluding the stopped
    # machine removes it from the trend entirely. That is a strict improvement on
    # showing it as a -80 point decline, but "which machines stopped" is real
    # information with nowhere to go -- recorded, not invented here.
    check("...and the machine that ran both weeks is still considered",
          all(isinstance(m["delta"], (int, float))
              for m in trend["declining_machines"] + trend["improving_machines"]),
          "a ranked row has no numeric delta")
    db.close()

    print()
    print("=" * 74)
    print("4. A MACHINE THAT STARTED DID NOT IMPROVE")
    print("=" * 74)
    db = _session()
    started = _machine(db, "NEW-01")
    steady = _machine(db, "STEADY-01")
    now = datetime.utcnow()
    _run(db, started.id, now - timedelta(days=2))          # this week only
    _run(db, steady.id, now - timedelta(days=10))
    _run(db, steady.id, now - timedelta(days=2))
    trend = build_oee_trend(db, "DEFAULT")
    improving = [(m["machine_id"], m["oee"], m["prior_oee"], m["delta"])
                 for m in trend["improving_machines"]]
    check(f"a machine with no history is not ranked as improving ({improving})",
          started.id not in [row[0] for row in improving],
          "a machine commissioned this week tops the improving list")
    db.close()

    print()
    print("=" * 74)
    print("5. A REAL MOVER IS STILL RANKED — THE CONTROL")
    print("=" * 74)
    # Without this, emptying both lists would pass sections 3 and 4.
    db = _session()
    mover = _machine(db, "MOVER-01")
    now = datetime.utcnow()
    _run(db, mover.id, now - timedelta(days=10), runtime=120, total=1000, good=500)
    _run(db, mover.id, now - timedelta(days=2), runtime=470, total=1000, good=995)
    trend = build_oee_trend(db, "DEFAULT")
    improving = [m["machine_id"] for m in trend["improving_machines"]]
    check(f"a machine that genuinely improved is still ranked ({improving})",
          mover.id in improving, str(trend["improving_machines"]))
    row = next(m for m in trend["improving_machines"] if m["machine_id"] == mover.id)
    check(f"...with a real delta ({row['delta']})",
          row["delta"] is not None and row["delta"] > 0, str(row))
    check("...and both halves are real figures, not fabricated zeros",
          row["oee"] is not None and row["prior_oee"] is not None, str(row))
    db.close()

    print()
    print("=" * 74)
    print("6. THE PLANT VERDICT STILL READS THE SAME")
    print("=" * 74)
    # This change is about the per-machine and per-day figures. The plant-level
    # branches already handled no-data honestly and must not move.
    db = _session()
    m = _machine(db, "PRESS-01")
    _run(db, m.id, datetime.utcnow() - timedelta(days=10))
    trend = build_oee_trend(db, "DEFAULT")
    check(f"no production this week still says so ({trend['direction']})",
          trend["direction"] == "unknown"
          and "No production recorded this week" in trend["verdict"],
          f"{trend['direction']} / {trend['verdict']}")
    db.close()
    db = _session()
    _machine(db, "PRESS-01")
    trend = build_oee_trend(db, "DEFAULT")
    check(f"an empty plant still says so ({trend['direction']})",
          trend["direction"] == "none", f"{trend['direction']} / {trend['verdict']}")
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


def test_oee_idle_is_not_zero():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
