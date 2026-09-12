"""The failure sparkline covered 28 of the 30 days it sat under.

THE DEFECT, IN TWO PARTS
------------------------
`build_machine_reliability` publishes a machine's failure count, MTTR and MTBF
over `WINDOW_DAYS = 30`, and beside them a "Failures by week" sparkline built as

    for w in range(WEEKS - 1, -1, -1):          # WEEKS = 4
        w_start = now - timedelta(days=7 * (w + 1))
        w_end   = now - timedelta(days=7 * w)

Four buckets of seven days is **28 days**. The code says so itself — *"the last
4 whole weeks (28 of the 30 days)"* — so a failure on day 29 or 30 is counted in
the headline, in `top_modes`, in MTBF, and drawn in **no bar**. The chart cannot
account for the number it sits under, and the two oldest days simply vanish from
the picture of "is this getting worse".

Second, the bars are labelled with a bare date but cut at the QUERY'S TIME OF
DAY. A bar labelled `2026-08-16` actually covers `2026-08-16 11:48` ->
`2026-08-23 11:48`, so roughly half of 16 August sits in the bar before it. Two
adjacent bars each hold part of the same calendar day while claiming to be
"Week of" two different ones — and which half goes where moves with the hour the
drawer is opened.

THE FIX
-------
Same shape as #586 and #587: the buckets derive from the WINDOW rather than from
a hardcoded count, so they tile it exactly and sum to the headline. Thirty days
is four sevens and a remainder, so the oldest bucket is two days and says so
(`partial: true`, `days: 2`).

And each bucket publishes `week_end` alongside `week_start`, so a bar states the
range it actually covers instead of implying a calendar week it does not.

NOT IN SCOPE: re-anchoring the buckets to midnight. That would make every label
a true calendar date, but it moves the newest bucket to "today so far" and
changes which failures land in which bar — a bigger change to a published series
than this defect justifies. Stating the real range removes the ambiguity without
moving anything.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_reliability_sparkline_span.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
from ai import reliability
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


def _machine(db, name="PRESS-01"):
    m = models.Machine(name=name, status="Running", utilization=80,
                       downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _stoppage(db, machine_id, days_ago, minutes=60, reason="Bearing"):
    db.add(models.DowntimeLog(
        machine_id=machine_id, reason=reason, duration=f"{minutes} min",
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def _detail(db, machine_id):
    return reliability.build_machine_reliability(db, "DEFAULT", machine_id)


def main():
    print("=" * 74)
    print("1. EVERY FAILURE IN THE HEADLINE IS IN A BAR")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    # Day 29 is inside the 30-day headline and outside the old 28-day sparkline.
    _stoppage(db, m.id, days_ago=29, reason="Bearing")
    _stoppage(db, m.id, days_ago=3, reason="Seal")
    detail = _detail(db, m.id)
    bars = sum(w["failures"] for w in detail["weekly"])
    check(f"the bars sum to the headline failure count "
          f"({bars} vs {detail['failures']})",
          bars == detail["failures"],
          "a failure counted in the headline is drawn in no bar")
    check("...and the headline is not zero, so that is not a vacuous agreement",
          detail["failures"] > 0, str(detail["failures"]))
    minutes = sum(w["minutes"] for w in detail["weekly"])
    check(f"the bar minutes sum to the repair total ({minutes} vs "
          f"{detail['repair_minutes']})",
          minutes == detail["repair_minutes"], "downtime minutes go missing too")
    db.close()

    print()
    print("=" * 74)
    print("2. THE BUCKETS TILE THE WHOLE WINDOW")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    _stoppage(db, m.id, days_ago=1)
    detail = _detail(db, m.id)
    weekly = detail["weekly"]
    spanned = sum(w["days"] for w in weekly)
    check(f"the buckets span exactly WINDOW_DAYS ({spanned} vs "
          f"{reliability.WINDOW_DAYS})",
          spanned == reliability.WINDOW_DAYS, f"{spanned} days covered")
    check(f"...across {len(weekly)} buckets, oldest first",
          [w["week_start"] for w in weekly] == sorted(w["week_start"] for w in weekly),
          str([w["week_start"] for w in weekly]))
    # 30 = 4x7 + 2, so exactly one bucket is short and it is the oldest.
    short = [w for w in weekly if w["days"] != 7]
    check(f"exactly one bucket is not a whole week ({len(short)})",
          len(short) == 1, str([(w["week_start"], w["days"]) for w in short]))
    check("...and it is the oldest, and is flagged partial",
          short and short[0] is weekly[0] and weekly[0].get("partial") is True,
          f"partial={weekly[0].get('partial')} days={weekly[0]['days']}")
    check("...while no whole week claims to be partial",
          not any(w.get("partial") for w in weekly[1:]),
          str([w["week_start"] for w in weekly[1:] if w.get("partial")]))

    print()
    print("=" * 74)
    print("3. A BAR STATES THE RANGE IT ACTUALLY COVERS")
    print("=" * 74)
    # A bare `week_start` label on a bucket cut at the query's time of day
    # implies a calendar week it does not cover.
    check("every bucket publishes an end as well as a start",
          all("week_end" in w for w in weekly), str(weekly[0]))
    contiguous = all(weekly[i]["week_end"] == weekly[i + 1]["week_start"]
                     for i in range(len(weekly) - 1))
    check("...and consecutive buckets abut exactly (no overlap, no gap)",
          contiguous,
          str([(w["week_start"], w["week_end"]) for w in weekly]))
    db.close()

    print()
    print("=" * 74)
    print("4. NO FAILURE IS COUNTED TWICE")
    print("=" * 74)
    # The other way a tiling can be wrong. One stoppage exactly on a boundary
    # must land in exactly one bucket.
    db = _session()
    m = _machine(db)
    for day in (0, 7, 14, 21, 28, 29):
        _stoppage(db, m.id, days_ago=day)
    detail = _detail(db, m.id)
    bars = sum(w["failures"] for w in detail["weekly"])
    check(f"six stoppages spread across five buckets are counted once "
          f"({bars} bars vs {detail['failures']} headline)",
          bars == detail["failures"] == 6, f"bars={bars} headline={detail['failures']}")
    db.close()
    # The buckets must be HALF-OPEN, or a stoppage landing exactly on an edge is
    # in two bars. No fixture can reach that: the edges are derived from a
    # `utcnow()` taken inside the read-model, and a row's `created_at` comes from
    # an earlier one, so an exact hit needs a microsecond coincidence. Unlike
    # #584 — where two windows shared a boundary instant BY CONSTRUCTION — this
    # one is not reachable by data, so it is asserted structurally instead of
    # with a fixture that cannot see it. Mutation testing earned the check: `<`
    # to `<=` survived everything above.
    import os
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ai/reliability.py"), encoding="utf-8").read()
    check("the bucket comparison is half-open",
          "w_start <= d.created_at < w_end" in source,
          "the weekly bucket no longer uses a half-open comparison")

    print()
    print("=" * 74)
    print("5. THE HEADLINE DID NOT MOVE — THE CONTROL")
    print("=" * 74)
    # This fixes the CHART. A failure outside the 30-day window must still be
    # excluded from both, and one inside must still count once.
    db = _session()
    m = _machine(db)
    _stoppage(db, m.id, days_ago=31)          # outside the window entirely
    _stoppage(db, m.id, days_ago=2)
    detail = _detail(db, m.id)
    check(f"a stoppage older than the window is in neither ({detail['failures']})",
          detail["failures"] == 1, str(detail["failures"]))
    check("...and the bars agree",
          sum(w["failures"] for w in detail["weekly"]) == 1,
          str([w["failures"] for w in detail["weekly"]]))
    db.close()

    # The other end. `_window_logs` filtered `created_at >= start` and nothing
    # else, so a FUTURE-dated stoppage — a bad gateway clock, a manual entry —
    # was counted in the headline, in MTBF and in MTTR, and fell outside every
    # bucket. Same shape as the recorded-cost query fixed in #587.
    db = _session()
    m = _machine(db)
    _stoppage(db, m.id, days_ago=-3, reason="Clock skew")   # three days from now
    _stoppage(db, m.id, days_ago=2)
    detail = _detail(db, m.id)
    check(f"a future-dated stoppage is excluded from the headline "
          f"({detail['failures']})", detail["failures"] == 1,
          f"failures={detail['failures']}")
    check("...and from the bars",
          sum(w["failures"] for w in detail["weekly"]) == 1,
          str([w["failures"] for w in detail["weekly"]]))
    check("...and from the failure log",
          len(detail["failures_log"]) == 1,
          str([f["reason"] for f in detail["failures_log"]]))
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


def test_reliability_sparkline_span():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
