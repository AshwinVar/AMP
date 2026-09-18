"""The cost trend's "this week" is the Costing card's week (oee_contract windows).

THE DEFECT
----------
The Costing view's intelligence card prints two figures for one week, side by
side: "Total lost" from /cost-summary, and the verdict from /cost-trend, "Losses
up £X to £Y week on week". The summary pools THE window, `OeeWindow(7)` =
[now-7d, now), as the scorecard's "Cost of losses" KPI and its week-on-week delta
do. The trend split its fortnight by CALENDAR DAY: "this week" was a record's date
0..6 days before today, "last week" 7..13. So a loss from late on the eighth date
(after the same clock time seven days ago) was in the card's "Total lost" and in
the trend's PRIOR week: £Y in the verdict was not the total printed beside it,
and the week-on-week move was measured over neither of the scorecard's weeks.

The fetch was the rolling fortnight [now-14d, now), so a record on date today-14
was fetched, landed in no half and no bar, and still set the noise floor's run
rate.

THE RULE
--------
One anchor, two adjacent windows: current = OeeWindow(7), prior =
prior_window(current), exactly as ai/oee.build_oee_trend, ai/scorecard and
ai/recovery build theirs. The daily series covers every date the fortnight
touches, flags a part-covered oldest date `partial`, and a seam date that holds
the end of one week and the start of the next shows both, so the series still
sums to the two weeks (rule 3).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_cost_trend_uses_the_contract_windows.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract
from ai import cost
from currency import money
from database import Base

UNIT_VALUE = 2     # £ per good unit

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
    db = sessionmaker(bind=engine)()
    db.add(models.TenantConfig(tenant_code="DEFAULT", plan="Pro", unit_value_gbp=UNIT_VALUE))
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.commit()
    return db


def _record(db, when, scrap):
    # 60 planned, 60 run at one good unit a minute: no downtime, so a record's loss
    # is exactly its scrap, and each window's lost units are the sum of its scrap.
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=60, runtime_minutes=60, ideal_cycle_time_seconds=60,
        total_count=60 + scrap, good_count=60, rejected_count=scrap, created_at=when))
    db.commit()


def section_the_card_agrees_with_itself():
    print("=" * 74)
    print("1. THE VERDICT'S 'THIS WEEK' IS THE 'TOTAL LOST' PRINTED BESIDE IT")
    print("=" * 74)
    db = _session()
    t0 = datetime.utcnow()
    # 30 minutes inside the rolling week's start: in [now-7d, now) whenever the card
    # is built in the next half hour, and on the eighth date for the calendar split
    # (unless this runs in the last half hour before midnight UTC, when both agree).
    _record(db, t0 - timedelta(days=7) + timedelta(minutes=30), scrap=40)
    _record(db, t0 - timedelta(days=2), scrap=10)
    _record(db, t0 - timedelta(days=3), scrap=5)
    _record(db, t0 - timedelta(days=10), scrap=20)
    _record(db, t0 - timedelta(days=11), scrap=25)
    summary = cost.build_cost_summary(db, "DEFAULT")
    trend = cost.build_cost_trend(db, "DEFAULT")
    check("the trend's current lost units are the summary's",
          trend["current"]["lost_units"] == summary["lost_units"],
          f"trend {trend['current']['lost_units']} vs summary {summary['lost_units']}")
    check("the trend's current cost is the card's 'Total lost'",
          trend["current"]["cost"] == summary["loss_cost"],
          f"trend {trend['current']['cost']} vs summary {summary['loss_cost']}")
    # "... to £Y week on week" when it moved, "steady at £Y ..." when it did not.
    check("the verdict quotes the card's 'Total lost'",
          any(f"{w} {money(summary['loss_cost'])}" in trend["verdict"] for w in ("to", "at")),
          f"{trend['verdict']!r} vs {money(summary['loss_cost'])}")
    check("CONTROL: the edge record matters (the summary differs without it)",
          summary["lost_units"] != cost.build_cost_summary(
              db, "DEFAULT", now=t0 + timedelta(hours=1))["lost_units"])
    db.close()


def section_the_weeks_tile():
    print()
    print("=" * 74)
    print("2. THE TWO WEEKS TILE: EVERY RECORD IN EXACTLY ONE, AT ANY TIME OF DAY")
    print("=" * 74)
    for label, now in (("at 14:00 UTC", datetime(2026, 9, 18, 14, 0)),
                       ("at 00:05 UTC", datetime(2026, 9, 18, 0, 5)),
                       ("at 23:55 UTC", datetime(2026, 9, 18, 23, 55)),
                       ("at midnight", datetime(2026, 9, 18, 0, 0))):
        db = _session()
        current = oee_contract.OeeWindow(7, now=now)
        prior = oee_contract.prior_window(current)
        # A record at each window's first and last instant, so the seam and both
        # outer edges are each tested from both sides. Every scrap figure is a
        # distinct power of two, so each total names exactly the records it counted.
        _record(db, current.start, scrap=1)                         # current, first instant
        _record(db, now - timedelta(seconds=1), scrap=2)            # current, last second
        _record(db, current.start - timedelta(seconds=1), scrap=4)  # prior, last second
        _record(db, prior.start, scrap=8)                           # prior, first instant
        _record(db, prior.start - timedelta(seconds=1), scrap=16)   # neither: before both
        _record(db, now, scrap=32)                                  # neither: not yet past
        trend = cost.build_cost_trend(db, "DEFAULT", now=now)
        summary = cost.build_cost_summary(db, "DEFAULT", now=now)
        check(f"{label}: current is the summary's week",
              trend["current"]["lost_units"] == summary["lost_units"] == 3,
              f"trend {trend['current']['lost_units']}, summary {summary['lost_units']}")
        check(f"{label}: prior is exactly its two records",
              trend["prior"]["lost_units"] == 12, str(trend["prior"]["lost_units"]))
        check(f"{label}: the series sums to the two weeks (rule 3)",
              sum(d["lost_units"] for d in trend["series"]) == 15
              and sum(d["cost"] for d in trend["series"]) == 15 * UNIT_VALUE,
              str([(d["date"], d["lost_units"]) for d in trend["series"] if d["lost_units"]]))
        first, last = prior.start.date(), (now - timedelta(microseconds=1)).date()
        dates = [d["date"] for d in trend["series"]]
        check(f"{label}: the series covers every date the fortnight touches, oldest first",
              dates == [(first + timedelta(days=i)).isoformat()
                        for i in range((last - first).days + 1)], f"{dates[0]}..{dates[-1]} ({len(dates)})")
        mid_day = prior.start.time() != datetime.min.time()
        flagged = [d["date"] for d in trend["series"] if d.get("partial")]
        check(f"{label}: only a part-covered oldest date is flagged partial",
              flagged == ([first.isoformat()] if mid_day else []), str(flagged))
        db.close()


def section_the_seam_date():
    print()
    print("=" * 74)
    print("3. THE SEAM DATE HOLDS THE END OF ONE WEEK AND THE START OF THE NEXT")
    print("=" * 74)
    now = datetime(2026, 9, 18, 14, 0)
    db = _session()
    current = oee_contract.OeeWindow(7, now=now)
    # Both on 2026-09-11: one before 14:00 (last week), one after (this week).
    _record(db, current.start - timedelta(hours=2), scrap=7)
    _record(db, current.start + timedelta(hours=2), scrap=9)
    trend = cost.build_cost_trend(db, "DEFAULT", now=now)
    seam = [d for d in trend["series"] if d["date"] == current.start.date().isoformat()]
    check("the weeks split the seam date's records", (trend["prior"]["lost_units"],
          trend["current"]["lost_units"]) == (7, 9),
          f"{trend['prior']['lost_units']}, {trend['current']['lost_units']}")
    check("the seam date's bar shows both weeks' losses, not one overwriting the other",
          len(seam) == 1 and seam[0]["lost_units"] == 16 and seam[0]["cost"] == 16 * UNIT_VALUE,
          str(seam))
    db.close()

    # Without a unit value there is no £ anywhere, and adding two unknowns on the
    # seam date must not invent a £0.
    db = _session()
    db.query(models.TenantConfig).delete()
    db.commit()
    _record(db, current.start - timedelta(hours=2), scrap=7)
    _record(db, current.start + timedelta(hours=2), scrap=9)
    trend = cost.build_cost_trend(db, "DEFAULT", now=now)
    seam = [d for d in trend["series"] if d["date"] == current.start.date().isoformat()]
    check("unpriced: the seam date keeps its units and has no £ figure",
          trend["priced"] is False and len(seam) == 1 and seam[0]["lost_units"] == 16
          and seam[0]["cost"] is None and seam[0]["scrap_cost"] is None, str(seam))
    db.close()


def main():
    section_the_card_agrees_with_itself()
    section_the_weeks_tile()
    section_the_seam_date()
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


def test_cost_trend_uses_the_contract_windows():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
