"""The Downtime card and the tile beside it measure the same week.

THE DEFECT
----------
`ai/downtime` cut the last seven CALENDAR dates and filtered the rest in
Python (`d.created_at.date() in window_set`), while `/analytics/summary` and
`/analytics/executive-oee` pass the canonical `oee_contract.OeeWindow` to
`analytics_engine.downtime_aggregates` for the very same plant. Those are
different sets of rows at BOTH ends, and the gap is not academic — the two
figures sit on the same screen. Measured on one plant at one moment:

    a six-hour breakdown late on the day the window opens
        the Downtime card   0 events,   0 min
        the dashboard tile  1 event,  360 min

    a stoppage dated later today (a gateway with a skewed clock)
        the Downtime card   1 event,  360 min
        the dashboard tile  0 events,   0 min

So the card could report a clean week beside a tile showing six hours lost, and
could count a stoppage that has not happened yet — the same future-dated defect
#590 found on the machine cockpit and #696 found in the Risk Radar.

The trend had the second half of the problem. Its halves were calendar-day
ages, so a stoppage at 03:00 on the seam date landed in `prior` here and in
`current` on the quality trend card printed beside it (`ai/quality` moved onto
the tiling window pair in #699, and this module's own `_half_of` docstring said
it was next).

THE RULE THIS FILE PINS
-----------------------
1. One window. The summary's totals equal `downtime_aggregates` over the same
   window, unit for unit — the card and the tile cannot disagree.
2. Bounded at BOTH ends: a stoppage dated in the future is in no week.
3. The series covers every date the window touches, oldest flagged `partial`.
4. The trend's halves tile: `prior.end is current.start`, a row on the boundary
   instant is counted exactly once, and the current half IS the summary's rows.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_downtime_one_window.py
"""
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import models                                        # noqa: E402
import oee_contract                                  # noqa: E402
from ai import downtime                              # noqa: E402
from analytics_engine import downtime_aggregates     # noqa: E402
from database import Base                            # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" [{detail}]" if not ok and detail else ""))
    if not ok:
        FAILURES.append(label)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=80, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=80, line="IC"))
    db.commit()
    return db


def _stop(db, machine_id, at, duration="30 min", reason="Breakdown"):
    db.add(models.DowntimeLog(machine_id=machine_id, reason=reason,
                              duration=duration, created_at=at))


def main():
    print("=" * 74)
    print("1. THE CARD AND THE TILE READ THE SAME ROWS")
    print("=" * 74)
    window = oee_contract.OeeWindow(downtime.WINDOW_DAYS)

    # The two instants where a calendar-day week and a rolling week differ.
    for label, at, expect_in in (
        ("late on the day the window opens", window.start + timedelta(minutes=30), True),
        ("dated later today, after the window ends", window.end + timedelta(hours=1), False),
    ):
        db = _session()
        _stop(db, 1, at, duration="6 hrs")
        db.commit()
        card = downtime.build_downtime_summary(db, "DEFAULT")
        tile = downtime_aggregates(db, window.start, window.end)
        check(f"{label}: card and tile agree "
              f"({card['total_events']}ev/{card['total_minutes']}min)",
              (card["total_events"], card["total_minutes"])
              == (tile["total_events"], tile["total_minutes"]),
              f"card {card['total_events']}/{card['total_minutes']} "
              f"vs tile {tile['total_events']}/{tile['total_minutes']}")
        check(f"...and the stoppage is {'counted' if expect_in else 'excluded'}, which is the "
              f"window's answer", (card["total_minutes"] == 360) is expect_in,
              str(card["total_minutes"]))
        db.close()

    print()
    print("=" * 74)
    print("2. A FULL WEEK RECONCILES, BREAKDOWNS AND ALL")
    print("=" * 74)
    db = _session()
    now = datetime.utcnow()
    _stop(db, 1, now - timedelta(days=1), "2 hrs 15 min", "Breakdown")   # 135
    _stop(db, 1, now - timedelta(days=3), "45 min", "Tooling")           # 45
    _stop(db, 2, now - timedelta(days=5), "1 hr", "Changeover")          # 60
    _stop(db, 2, now - timedelta(days=30), "10 hrs", "Breakdown")        # out of the window
    db.commit()
    card = downtime.build_downtime_summary(db, "DEFAULT")
    tile = downtime_aggregates(db, window.start, window.end)
    check("three stoppages, 240 minutes, and the month-old one is out",
          (card["total_events"], card["total_minutes"]) == (3, 240), str(card["total_minutes"]))
    check("...and the tile agrees unit for unit",
          (card["total_events"], card["total_minutes"])
          == (tile["total_events"], tile["total_minutes"]),
          f"{card['total_minutes']} vs {tile['total_minutes']}")
    check("the daily bars sum back to the headline (rule 3)",
          sum(e["minutes"] for e in card["daily"]) == card["total_minutes"],
          str(sum(e["minutes"] for e in card["daily"])))
    check("the Pareto sums back to it too",
          sum(r["minutes"] for r in card["top_reasons"]) == card["total_minutes"])
    check("the card names the window it measured",
          card["window"] == "last 7 days", str(card.get("window")))
    # The drill-down a Pareto row opens must be the same week as the row.
    drill = downtime.build_downtime_reason(db, "DEFAULT", "Breakdown")
    top = next(r for r in card["top_reasons"] if r["reason"] == "Breakdown")
    check("a reason drill-down reconciles with the Pareto row that opened it",
          (drill["total_events"], drill["total_minutes"]) == (top["count"], top["minutes"]),
          f"{drill['total_minutes']} vs {top['minutes']}")
    check("...and names the same window", drill["window"] == card["window"])
    db.close()

    print()
    print("=" * 74)
    print("3. THE SERIES COVERS THE DATES THE WINDOW TOUCHES")
    print("=" * 74)
    at = datetime(2026, 9, 21, 14, 30)          # opens mid-day: eight dates
    db = _session()
    _stop(db, 1, at - timedelta(days=2), "30 min")
    db.commit()
    card = downtime.build_downtime_summary(db, "DEFAULT", now=at)
    check("a mid-day window touches eight calendar dates, not seven",
          len(card["daily"]) == 8, str(len(card["daily"])))
    check("...the oldest is flagged partial, and only the oldest",
          card["daily"][0].get("partial") is True
          and all("partial" not in e for e in card["daily"][1:]),
          str(card["daily"][0]))
    check("...running 09-14 to 09-21",
          (card["daily"][0]["date"], card["daily"][-1]["date"]) == ("2026-09-14", "2026-09-21"),
          f"{card['daily'][0]['date']}..{card['daily'][-1]['date']}")
    db.close()

    midnight = datetime(2026, 9, 21, 0, 0, 0)
    db = _session()
    _stop(db, 1, midnight - timedelta(hours=1), "30 min")
    db.commit()
    card = downtime.build_downtime_summary(db, "DEFAULT", now=midnight)
    check("a window ending at midnight touches exactly seven dates",
          len(card["daily"]) == 7, str(len(card["daily"])))
    check("...ending the day BEFORE the exclusive end, where the stoppage is",
          card["daily"][-1]["date"] == "2026-09-20" and card["daily"][-1]["count"] == 1,
          str(card["daily"][-1]))
    db.close()

    print()
    print("=" * 74)
    print("4. THE TREND'S HALVES TILE")
    print("=" * 74)
    cur = oee_contract.OeeWindow(downtime.WINDOW_DAYS, now=at)
    pri = oee_contract.prior_window(cur)
    check("prior.end is current.start", pri.end == cur.start)

    db = _session()
    # One stoppage ON the boundary instant: it belongs to the current half and
    # to no other (half-open). Under calendar-day ages it could land in prior.
    _stop(db, 1, cur.start, "60 min")
    db.commit()
    t = downtime.build_downtime_trend(db, "DEFAULT", now=at)
    check("a stoppage on the boundary instant is in the current half",
          (t["current"]["minutes"], t["prior"]["minutes"]) == (60, 0),
          f"{t['current']['minutes']} / {t['prior']['minutes']}")
    check("...and counted exactly once across the two halves",
          t["current"]["events"] + t["prior"]["events"] == 1)
    check("the fortnight's series covers fifteen dates, oldest partial",
          len(t["series"]) == 15 and t["series"][0].get("partial") is True,
          str(len(t["series"])))
    check("...and the current half starts `half_days` buckets in, where the card shades",
          t["series"][t["half_days"]]["date"] == "2026-09-14", str(t["half_days"]))
    check("...with no second expression of that index in the payload",
          "current_from" not in t, str(sorted(t)))
    check("the series sums back to the two halves (rule 3)",
          sum(e["minutes"] for e in t["series"])
          == t["current"]["minutes"] + t["prior"]["minutes"])
    check("the trend names its half", t["window"] == "last 7 days", str(t.get("window")))
    db.close()

    db = _session()
    # A genuine week-over-week move, plus a future-dated row that must not touch it.
    _stop(db, 1, at - timedelta(days=2), "3 hrs", "Breakdown")     # 180 this week
    _stop(db, 1, at - timedelta(days=9), "30 min", "Breakdown")    # 30 last week
    # Twelve hours the far side of the seam: in the PRIOR half and in no other.
    # A current half even one day wider would swallow it, and the two halves
    # would then double-count it — which is what makes the halves "tile" a
    # testable claim rather than a description.
    _stop(db, 2, at - timedelta(days=7, hours=12), "40 min", "Tooling")
    _stop(db, 2, at + timedelta(days=2), "10 hrs", "Breakdown")    # has not happened
    db.commit()
    t = downtime.build_downtime_trend(db, "DEFAULT", now=at)
    check("the current half is this week's 180 minutes, not the future 600",
          t["current"]["minutes"] == 180, str(t["current"]["minutes"]))
    check("...the prior half is last week's 70 (30 + the 40 just past the seam)",
          t["prior"]["minutes"] == 70, str(t["prior"]["minutes"]))
    check("...the seam stoppage is in exactly one half, not both",
          t["current"]["minutes"] + t["prior"]["minutes"]
          == sum(e["minutes"] for e in t["series"]) == 250,
          f"{t['current']['minutes']} + {t['prior']['minutes']} vs "
          f"{sum(e['minutes'] for e in t['series'])}")
    check("...so the move is +110 and reads as worsening",
          t["delta_minutes"] == 110 and t["direction"] == "worsening",
          f"{t['delta_minutes']} / {t['direction']}")
    check("...and the machine blamed is the one that actually stopped",
          [m["name"] for m in t["worsening_machines"]] == ["PRESS-01"],
          str(t["worsening_machines"]))
    # The summary over the same instant must see exactly the current half's rows.
    card = downtime.build_downtime_summary(db, "DEFAULT", now=at)
    check("the summary and the trend's current half are the same week",
          (card["total_events"], card["total_minutes"])
          == (t["current"]["events"], t["current"]["minutes"]),
          f"{card['total_minutes']} vs {t['current']['minutes']}")
    db.close()

    print()
    print("=" * 74)
    print("5. ONE LABEL FOR AN UNLABELLED STOP")
    print("=" * 74)
    # ai.downtime._norm_reason and analytics_engine.normalize_downtime_reason
    # used to be two copies of the same rule; the engine's docstring required
    # them to agree. They are now one function, so they cannot drift.
    db = _session()
    _stop(db, 1, now - timedelta(days=1), "30 min", reason="   ")
    db.commit()
    card = downtime.build_downtime_summary(db, "DEFAULT")
    tile = downtime_aggregates(db, window.start, window.end)
    check("a blank reason reads 'Unknown' on the card",
          [r["reason"] for r in card["top_reasons"]] == ["Unknown"], str(card["top_reasons"]))
    check("...and identically in the engine's tally",
          list(tile["events_by_reason"]) == ["Unknown"], str(tile["events_by_reason"]))
    db.close()

    print()
    print("=" * 74)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for f in FAILURES:
            print("   *", f)
    else:
        print("ONE WINDOW OK: the Downtime card, its drill-down, its trend and the "
              "dashboard tile all measure the same week")
    print("=" * 74)
    return 1 if FAILURES else 0


def test_downtime_one_window():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
