"""The OEE trend's "this week" is the headline's week (oee_contract windows).

THE DEFECT
----------
/oee-trend split its fortnight by CALENDAR DAY: "this week" was a record's date
0..6 days before today, "last week" 7..13. /oee-summary, the scorecard and the
recovery card pool THE window, `oee_contract.OeeWindow(7)` = [now-7d, now). So a
record from late on day 7 (after the same clock time seven days ago) was in the
headline's week and in the trend's PRIOR week, and the trend's "current" OEE was
a different figure from the plant OEE printed beside it: one plant, one moment,
two answers to "OEE this week", and a week-on-week delta computed over neither.
ai/scorecard.py and ai/recovery.py were fixed for the same hand-rolled halves
(test_week_halves_tile.py); this read-model kept its own.

THE RULE
--------
One anchor, two adjacent windows: current = OeeWindow(7), prior =
prior_window(current). This file checks that, from the outside.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oee_trend_uses_the_contract_windows.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract
from ai import oee
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
    m = models.Machine(name="M1", status="Running", utilization=80, downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _record(db, machine_id, when, good):
    # 480 planned, 240 run, 1000 made at a 12 s ideal cycle: only QUALITY varies,
    # so each record's OEE is set by `good` and the pooled figure reveals which
    # records a window counted.
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=480, runtime_minutes=240,
        ideal_cycle_time_seconds=12, total_count=1000, good_count=good,
        rejected_count=1000 - good, created_at=when))
    db.commit()


def section_the_edge_record():
    print("=" * 74)
    print("1. A RECORD LATE ON DAY 7 IS IN THE HEADLINE'S WEEK, SO IN THE TREND'S")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    t0 = datetime.utcnow()
    # 30 minutes inside the rolling week's start: in [now-7d, now) whenever the
    # trend is built in the next half hour, and on calendar day 7 for the old split
    # (unless this runs in the last half hour before midnight UTC, when both agree).
    _record(db, m.id, t0 - timedelta(days=7) + timedelta(minutes=30), good=200)
    _record(db, m.id, t0 - timedelta(days=2), good=900)
    _record(db, m.id, t0 - timedelta(days=10), good=600)
    summary = oee.build_oee_summary(db, "DEFAULT")
    trend = oee.build_oee_trend(db, "DEFAULT")
    check("the trend's current OEE is the headline's plant OEE",
          trend["current"]["oee"] == summary["plant"]["oee"],
          f"trend {trend['current']['oee']} vs headline {summary['plant']['oee']}")
    check("CONTROL: the edge record matters (the headline differs without it)",
          summary["plant"]["oee"] != oee.build_oee_summary(db, "DEFAULT",
                                                           now=t0 + timedelta(hours=1))["plant"]["oee"])
    db.close()


def section_adjacent_windows():
    print()
    print("=" * 74)
    print("2. THE TWO WEEKS TILE: EVERY RECORD IN EXACTLY ONE, AT ANY TIME OF DAY")
    print("=" * 74)
    for label, now in (("at 14:00 UTC", datetime(2026, 9, 18, 14, 0)),
                       ("at 00:05 UTC", datetime(2026, 9, 18, 0, 5)),
                       ("at 23:55 UTC", datetime(2026, 9, 18, 23, 55))):
        db = _session()
        m = _machine(db)
        current = oee_contract.OeeWindow(7, now=now)
        prior = oee_contract.prior_window(current)
        # A record at each window's first and last instant, so the seam and both
        # outer edges are each tested from both sides. The two weeks pool to
        # DIFFERENT figures (50% vs 80% quality), so a record that lands in the
        # wrong week, or a prior week that is secretly the current one, moves one.
        _record(db, m.id, current.start, good=200)                        # current, first instant
        _record(db, m.id, now - timedelta(seconds=1), good=800)            # current, last second
        _record(db, m.id, current.start - timedelta(seconds=1), good=900)  # prior, last second
        _record(db, m.id, prior.start, good=700)                           # prior, first instant
        _record(db, m.id, prior.start - timedelta(seconds=1), good=0)      # neither: before both
        _record(db, m.id, now, good=0)                                     # neither: not yet past
        trend = oee.build_oee_trend(db, "DEFAULT", now=now)
        headline = oee.build_oee_summary(db, "DEFAULT", now=now)["plant"]["oee"]
        check(f"{label}: current is the headline's week",
              trend["current"]["oee"] == headline, f"{trend['current']['oee']} vs {headline}")
        check(f"{label}: current pools exactly its two records (quality 50%)",
              trend["current"]["quality"] == 50, str(trend["current"]))
        check(f"{label}: prior pools exactly its two records (quality 80%)",
              trend["prior"]["quality"] == 80, str(trend["prior"]))
        db.close()


def main():
    section_the_edge_record()
    section_adjacent_windows()
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


def test_oee_trend_uses_the_contract_windows():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
