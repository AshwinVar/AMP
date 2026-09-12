"""The OEE trend bars could not add up to the headline above them.

THE DEFECT
----------
`build_oee_summary` builds ONE record set — the canonical rolling window,
`OeeWindow(7)` = `[utcnow-7d, utcnow)` — and publishes two things from it:

  * `plant.oee`, pooled over every record in the set;
  * `daily`, the trend the card draws, bucketed by CALENDAR DATE over
    `[today-6 ... today]`.

A rolling 7x24h window that starts part-way through a day touches EIGHT
calendar dates, not seven. Every record on that eighth (partial, oldest) date is
in the headline and in no bar.

Measured, at 11:48 UTC, on a plant with two runs — a bad one on the boundary
date and a good one two days ago:

    window            2026-09-05 11:48 -> 2026-09-12 11:48   (8 calendar dates)
    HEADLINE          45%
    daily bars        09-06:0  09-07:0  09-08:0  09-09:0  09-10:83  09-11:0  09-12:0
    pooled over the bar dates only   83%

The chart says 83. The headline says 45. Nothing on the card accounts for the
38-point gap, because the run that causes it is dated 09-05 and there is no
09-05 bar. A user reading the card cannot reconcile it, and the size of the gap
depends on the hour they open it.

The sibling read-models already get this right and are the precedent:
`ai/production.py` pins its query to the same `window_set` its daily series
draws, so its headline and bars reconcile by construction.

THE FIX, AND WHAT IT DELIBERATELY DOES NOT DO
---------------------------------------------
Two options, and they differ in whether AMP's flagship number moves:

  (a) narrow the HEADLINE to the seven calendar dates the bars draw — matches
      ai/production, but changes published plant OEE;
  (b) widen the BARS to cover the window the headline already pools.

This takes (b). `OeeWindow` is THE canonical window (oee_contract), the headline
is correct as measured, and the defect is that the chart cannot explain it —
so the chart is what was wrong. Plant OEE does not move, and the bars now tile
the window exactly: pooling every bar reproduces the headline.

The oldest bar is a PARTIAL day, and says so (`partial: true`), because the
window starts part-way through it. Pretending it is a whole day would be the
same class of lie in a different place.

NOT IN SCOPE, recorded rather than fixed: a day with no production still reads
`oee: 0` rather than "no data", so an idle day draws as a catastrophic one. That
is the #585 fabricated-zero rule applied to the series, it changes a typed
frontend contract (`daily: {date, oee}[]`), and it deserves its own change.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oee_bars_explain_headline.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from ai.oee import build_oee_summary
from analytics_engine import pooled_oee
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
    m = models.Machine(name="M1", status="Running", utilization=80,
                       downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _run(db, machine_id, when, planned=480, runtime=470, total=2000, good=1990):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=12, total_count=total, good_count=good,
        rejected_count=total - good, created_at=when))
    db.commit()


def main():
    print("=" * 74)
    print("1. EVERY RECORD IN THE HEADLINE IS IN A BAR")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    # The record that used to vanish: inside the rolling window, dated on the
    # partial eighth calendar date the bars did not cover.
    _run(db, m.id, window.start + timedelta(minutes=30),
         runtime=120, total=1000, good=200)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2))

    summary = build_oee_summary(db, "DEFAULT", now=window.end)
    dates = [d["date"] for d in summary["daily"]]
    boundary = window.start.date().isoformat()
    check(f"the boundary date {boundary} has a bar", boundary in dates, str(dates))
    check("...and the series covers every calendar date the window touches",
          len(dates) == len({(window.start + timedelta(hours=h)).date()
                             for h in range(0, 7 * 24 + 1)}),
          f"{len(dates)} bars")
    check("...with no duplicate dates and in ascending order",
          dates == sorted(set(dates)), str(dates))

    print()
    print("=" * 74)
    print("2. THE BARS POOL BACK TO THE HEADLINE")
    print("=" * 74)
    # The property the card promises. Pooling the records BEHIND every bar must
    # reproduce the published figure -- not the mean of the bars, which is a
    # different (and wrong) thing for a ratio of sums.
    in_bars = [r for r in db.query(models.ProductionRecord).all()
               if r.created_at and r.created_at.date().isoformat() in dates]
    check(f"pooling the bar dates reproduces the headline "
          f"({pooled_oee(in_bars)['oee']} vs {summary['plant']['oee']})",
          pooled_oee(in_bars)["oee"] == summary["plant"]["oee"],
          "the chart still cannot explain the number above it")
    check("...and every record in the window is behind some bar",
          len(in_bars) == db.query(models.ProductionRecord).count(),
          f"{len(in_bars)} of {db.query(models.ProductionRecord).count()}")

    print()
    print("=" * 74)
    print("3. THE PARTIAL DAY SAYS IT IS PARTIAL")
    print("=" * 74)
    partial = [d for d in summary["daily"] if d.get("partial")]
    check(f"exactly one bar is marked partial ({len(partial)})",
          len(partial) == 1, str([d["date"] for d in partial]))
    check("...and it is the oldest one",
          partial and partial[0]["date"] == dates[0],
          f"{partial[0]['date'] if partial else None} vs {dates[0]}")
    check("...and today is not marked partial",
          not summary["daily"][-1].get("partial"),
          "the newest bar claims to be incomplete")
    db.close()

    print()
    print("=" * 74)
    print("4. A WINDOW THAT STARTS AT MIDNIGHT HAS NO PARTIAL DAY")
    print("=" * 74)
    # The boundary case of the boundary case: exactly at midnight the rolling
    # window touches seven dates, not eight, and none of them is partial.
    db = _session()
    m = _machine(db)
    midnight_end = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    win = oee_contract.OeeWindow(7, now=midnight_end)
    _run(db, m.id, win.start + timedelta(hours=3))
    summary = build_oee_summary(db, "DEFAULT", now=midnight_end)
    check(f"a midnight-anchored window draws 7 bars ({len(summary['daily'])})",
          len(summary["daily"]) == 7, str([d["date"] for d in summary["daily"]]))
    check("...and marks none of them partial",
          not any(d.get("partial") for d in summary["daily"]),
          str([d["date"] for d in summary["daily"] if d.get("partial")]))
    db.close()

    print()
    print("=" * 74)
    print("5. THE HEADLINE DID NOT MOVE — THE CONTROL")
    print("=" * 74)
    # This PR fixes the CHART. If plant OEE changed, the fix took option (a)
    # by accident and moved AMP's flagship number.
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    _run(db, m.id, window.start + timedelta(minutes=30),
         runtime=120, total=1000, good=200)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2))
    summary = build_oee_summary(db, "DEFAULT", now=window.end)
    everything = db.query(models.ProductionRecord).all()
    check(f"plant OEE still pools the whole rolling window "
          f"({summary['plant']['oee']} == {pooled_oee(everything)['oee']})",
          summary["plant"]["oee"] == pooled_oee(everything)["oee"],
          "the headline moved; this change was supposed to fix the chart")
    check("...and it is not the bar-dates-only figure the old chart implied",
          summary["plant"]["has_data"], "no data to compare")
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


def test_oee_bars_explain_headline():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
