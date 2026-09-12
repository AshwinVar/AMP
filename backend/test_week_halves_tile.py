""""This week vs last week" compared a week against itself.

THE DEFECT
----------
Two exec read-models publish a week-over-week delta and a coloured badge:
`ai/scorecard.py` (the "Plant OEE" KPI's arrow) and `ai/recovery.py` (the OEE
recovery card's improving / worsening / flat trend).

Their CURRENT half is the canonical rolling window — `OeeWindow(7)` =
[utcnow-7d, utcnow). Their PRIOR half was hand-rolled from calendar midnights:

    today = datetime.utcnow().date()
    lo = datetime.combine(today - timedelta(days=13), datetime.min.time())
    hi = datetime.combine(today - timedelta(days=6),  datetime.min.time())

`hi` is midnight at the START of day-6, so the prior window runs through the END
of day-7 — while the current window already STARTS part-way through day-7, at
whatever time of day it is now. The two halves therefore OVERLAP by

    24h - (time since midnight UTC)

which is ~10 hours at 14:00 UTC and a FULL 24 HOURS at midnight. Every record in
that band is counted in BOTH halves of a difference.

WHAT A USER SEES
----------------
A plant with exactly one production run, on day-7 at 18:00, loaded at 14:00 UTC:
that run is the whole of "this week" AND the whole of "last week". The scorecard
publishes a confident green arrow — "OEE improved N points week on week" — when
the honest answer is that there was no prior week to compare against. The same
data yields a DIFFERENT delta depending on the hour the dashboard is opened,
which is the tell that the windows, not the plant, are moving.

`oee_contract` already states the rule this breaks, in the comment above the
default end:

    "half-open is what makes [d-14, d-7) and [d-7, d) tile without sharing a
     record ... An explicit `now=` — what a caller passes to build adjacent
     windows — is used verbatim."

The mechanism for an adjacent window was there. These two callers hand-rolled
midnights instead.

THE FIX
-------
One anchor per request. The current window is `OeeWindow(7)`; the prior window is
`OeeWindow(7, now=current.start)`, so `prior.end is current.start` exactly — no
overlap, no gap, and no dependence on the time of day. The anchor is threaded
into `build_oee_summary` so the headline the delta is taken against is measured
over the very window the prior half abuts, rather than over one built a few
milliseconds later.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_week_halves_tile.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from ai import recovery, scorecard
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


def _machine(db, name="M1"):
    m = models.Machine(name=name, status="Running", utilization=80,
                       downtime="0 min", line="L1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _record(db, machine_id, when, planned=480, runtime=240, total=1000,
            good=500, ideal=12):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=ideal, total_count=total, good_count=good,
        rejected_count=total - good, created_at=when))
    db.commit()


def main():
    print("=" * 74)
    print("1. THE TWO HALVES TILE EXACTLY, AT EVERY HOUR OF THE DAY")
    print("=" * 74)
    # The defect was invisible at some hours and 24h wide at others, so the
    # property is asserted across the clock rather than at one convenient time.
    for hour in (0, 1, 6, 12, 14, 23):
        anchor = oee_contract.OeeWindow(7, now=datetime(2026, 9, 12, hour, 30))
        prior = oee_contract.prior_window(anchor)
        check(f"{hour:02d}:30 — prior.end is exactly current.start",
              prior.end == anchor.start,
              f"prior.end={prior.end} current.start={anchor.start}")
        check(f"{hour:02d}:30 — and the prior half is a full 7 days",
              anchor.start - prior.start == timedelta(days=7),
              str(anchor.start - prior.start))

    print()
    print("=" * 74)
    print("2. NO RECORD CAN FALL IN BOTH HALVES, OR IN NEITHER")
    print("=" * 74)
    # Stated over the records themselves, not the bounds, because that is the
    # thing the arithmetic actually double-counted.
    anchor = oee_contract.OeeWindow(7, now=datetime(2026, 9, 12, 14, 0))
    prior = oee_contract.prior_window(anchor)

    def half_of(when):
        cur = anchor.start <= when < anchor.end
        pri = prior.start <= when < prior.end
        return cur, pri

    probes = [anchor.start - timedelta(microseconds=1), anchor.start,
              prior.start, prior.end - timedelta(microseconds=1),
              datetime(2026, 9, 5, 18, 0), datetime(2026, 9, 5, 10, 0),
              datetime(2026, 9, 9, 8, 0)]
    both = [p for p in probes if all(half_of(p))]
    check(f"no probe instant is in both halves ({len(both)} found)", not both,
          str(both))
    # The boundary instant belongs to exactly one side, and it is the later one.
    cur, pri = half_of(anchor.start)
    check("the shared boundary instant belongs to the CURRENT half only",
          cur and not pri, f"current={cur} prior={pri}")
    cur, pri = half_of(anchor.start - timedelta(microseconds=1))
    check("...and the instant before it to the PRIOR half only",
          pri and not cur, f"current={cur} prior={pri}")

    print()
    print("=" * 74)
    print("2b. AND THE QUERY AGREES WITH THE ARITHMETIC, ON THE BOUNDARY ROW")
    print("=" * 74)
    # Section 2 reasons about the bounds in Python. The double-count happened in
    # SQL, so the boundary has to be exercised through the real query: mutation
    # testing showed that flipping `created_at < window.end` to `<=` survived
    # everything above, and that single character puts a record created exactly
    # on the shared instant into BOTH halves -- the defect, rebuilt.
    from ai.twin import _recent_production as _records
    db = _session()
    m = _machine(db)
    anchor = oee_contract.OeeWindow(7, now=datetime(2026, 9, 12, 14, 0))
    prior_w = oee_contract.prior_window(anchor)
    _record(db, m.id, anchor.start)                       # exactly on the seam
    in_current = _records(db, days=7, now=anchor.end)
    in_prior = _records(db, days=7, now=prior_w.end)
    check(f"a record on the seam is in exactly one half "
          f"(current={len(in_current)}, prior={len(in_prior)})",
          len(in_current) + len(in_prior) == 1,
          "the boundary row is counted twice (or lost)")
    check("...and it belongs to the current half", len(in_current) == 1,
          f"current={len(in_current)}")
    db.close()

    print()
    print("=" * 74)
    print("3. A PLANT WITH ONE RUN IS NOT COMPARED AGAINST ITSELF")
    print("=" * 74)
    # The worked example from the defect report, end to end through the real
    # read-models. One run, seven days ago, at a time of day inside the old
    # overlap band.
    db = _session()
    m = _machine(db)
    now = datetime.utcnow()
    only_run = datetime.combine((now - timedelta(days=7)).date(), datetime.min.time()) \
        + timedelta(hours=23, minutes=30)
    if only_run >= now:                         # guard: keep the probe in the past
        only_run = now - timedelta(days=6, hours=23)
    _record(db, m.id, only_run)

    card = scorecard.build_scorecard(db, "DEFAULT")
    oee_kpi = next(k for k in card["kpis"] if k["key"] == "oee")
    check("the scorecard does not publish a week-on-week delta against itself",
          oee_kpi["delta"] is None and oee_kpi["delta_tone"] is None,
          f"delta={oee_kpi['delta']} tone={oee_kpi['delta_tone']}")

    rec = recovery.build_recovery_summary(db, "DEFAULT")
    check("...and the recovery card reports 'new', not 'flat' vs itself",
          rec["oee_trend"] == "new" and rec["prior_oee"] is None,
          f"trend={rec['oee_trend']!r} prior_oee={rec['prior_oee']}")
    db.close()

    print()
    print("=" * 74)
    print("4. A REAL PRIOR WEEK IS STILL COMPARED — THE CONTROL")
    print("=" * 74)
    # Without this, deleting the comparison entirely would pass section 3.
    db = _session()
    m = _machine(db)
    now = datetime.utcnow()
    _record(db, m.id, now - timedelta(days=2), total=2400, good=2400, runtime=480)
    _record(db, m.id, now - timedelta(days=9), total=1000, good=500, runtime=240)

    card = scorecard.build_scorecard(db, "DEFAULT")
    oee_kpi = next(k for k in card["kpis"] if k["key"] == "oee")
    check("a genuine prior week still produces a delta",
          oee_kpi["delta"] is not None and oee_kpi["delta_tone"] is not None,
          f"delta={oee_kpi['delta']} tone={oee_kpi['delta_tone']}")
    rec = recovery.build_recovery_summary(db, "DEFAULT")
    check("...and the recovery card still trends it",
          rec["oee_trend"] in ("improving", "worsening", "flat")
          and rec["prior_oee"] is not None,
          f"trend={rec['oee_trend']!r} prior_oee={rec['prior_oee']}")
    db.close()

    print()
    print("=" * 74)
    print("5. NEITHER READ-MODEL HAND-ROLLS A WINDOW ANY MORE")
    print("=" * 74)
    # The defect was a second definition of "the week before", written in
    # midnights. A third one must not appear.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ai/scorecard.py", "ai/recovery.py"):
        source = open(os.path.join(here, name), encoding="utf-8").read()
        check(f"{name} builds no window from datetime.min.time()",
              "datetime.min.time()" not in source,
              "still combines a date with midnight to bound a window")
        check(f"...and {name} uses the contract's adjacent-window helper",
              "prior_window" in source, "no call to oee_contract.prior_window")
    # ONE anchor, threaded. Dropping `now=` here leaves the headline measured over
    # a window built a few milliseconds after the one its "last week" abuts: a
    # sub-millisecond GAP rather than a 24-hour overlap, so no fixture can see it
    # and mutation testing showed only a sibling suite catching it. Asserted
    # structurally, because the property is "one anchor", not "some number moved".
    scorecard_src = open(os.path.join(here, "ai/scorecard.py"), encoding="utf-8").read()
    check("the scorecard measures its headline over the window its prior half abuts",
          "build_oee_summary(db, tenant, now=" in scorecard_src,
          "build_oee_summary is called without the request's anchor")

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


def test_week_halves_tile():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
