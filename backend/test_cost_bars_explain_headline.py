"""The cost-of-losses chart could sum to zero under a five-figure headline.

THE DEFECT, IN TWO PARTS
------------------------
`build_cost_summary` publishes three things under ONE `"days": 7` label, on
THREE different bases.

1. `loss_cost` (and `by_line`, `by_machine`, `machine_cost`) pools the canonical
   rolling window, `OeeWindow(7)` = `[utcnow-7d, utcnow)`.
2. `daily`, the trend the card draws, buckets by calendar date over
   `[today-6 ... today]` — seven dates. A rolling 7x24h window touches EIGHT,
   so every record on the partial eighth date is in the headline and in no bar.
3. `by_type`, the recorded costs beside them, queries
   `CostRecord.created_at >= midnight(today-6)` with **no upper bound at all**.

Measured, with one costly run on the boundary date and one future-dated cost:

    HEADLINE loss_cost   30,760
    daily bars           09-06:0 09-07:0 09-08:0 09-09:0 09-10:0 09-11:0 09-12:0
    sum of bars          0
    GAP                  30,760

    by_type              [{'type': 'Energy', 'amount': 9999}]
                         dated 2026-09-15 -- three days in the FUTURE

So the chart sums to **zero** under a five-figure headline, and the recorded-cost
breakdown includes money that has not been spent yet. This is the cost face of
the same root cause fixed for OEE in #586; the arithmetic is identical and the
consequence is larger, because a cost figure is read as money.

THE FIX
-------
One anchor per request, as in #586 and #584:

  * `daily` derives its buckets from the WINDOW the records came from, so the
    bars tile it and sum back to the headline. The oldest bar is flagged
    `partial` when the window opens mid-day, because it is.
  * the `CostRecord` query is bounded by the SAME window at both ends, so a
    future-dated cost cannot leak into a 7-day card.

This does not move `loss_cost`: like #586, the headline is correct as measured
and it was the chart that could not explain it. Section 4 pins that.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_cost_bars_explain_headline.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from ai.cost import build_cost_summary
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


def _run(db, machine_id, when, planned=480, runtime=0, total=1000, good=0):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=12, total_count=total, good_count=good,
        rejected_count=total - good, created_at=when))
    db.commit()


def _cost(db, when, amount=9999, no="C-1", kind="Energy"):
    db.add(models.CostRecord(cost_no=no, cost_type=kind, description="bill",
                             amount=amount, created_at=when))
    db.commit()


def main():
    print("=" * 74)
    print("1. THE BARS SUM TO THE HEADLINE")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    # The costly run that used to vanish from the chart: inside the rolling
    # window, dated on the partial eighth calendar date.
    _run(db, m.id, window.start + timedelta(minutes=30))
    _run(db, m.id, datetime.utcnow() - timedelta(days=2),
         runtime=480, total=100, good=100)
    summary = build_cost_summary(db, "DEFAULT", now=window.end)
    bars = sum(d["cost"] for d in summary["daily"])
    check(f"the daily bars sum to the headline ({bars} vs {summary['loss_cost']})",
          bars == summary["loss_cost"],
          "the chart still cannot account for the published figure")
    check("...and the headline is not zero, so that is not a vacuous agreement",
          summary["loss_cost"] > 0, str(summary["loss_cost"]))
    dates = [d["date"] for d in summary["daily"]]
    check(f"the boundary date {window.start.date()} has a bar",
          window.start.date().isoformat() in dates, str(dates))

    print()
    print("=" * 74)
    print("2. THE PARTIAL DAY SAYS IT IS PARTIAL")
    print("=" * 74)
    partial = [d for d in summary["daily"] if d.get("partial")]
    check(f"exactly one bar is marked partial ({len(partial)})", len(partial) == 1,
          str([d["date"] for d in partial]))
    check("...and it is the oldest one",
          partial and partial[0]["date"] == dates[0],
          f"{partial[0]['date'] if partial else None} vs {dates[0]}")
    db.close()

    print()
    print("=" * 74)
    print("3. RECORDED COSTS ARE BOUNDED AT BOTH ENDS")
    print("=" * 74)
    # The query had no upper bound, so money not yet spent was inside a 7-day
    # card. A cost dated in the future is not a 7-day cost.
    db = _session()
    _machine(db)
    window = oee_contract.OeeWindow(7)
    _cost(db, datetime.utcnow() + timedelta(days=3), amount=9999, no="C-FUTURE")
    _cost(db, datetime.utcnow() - timedelta(days=2), amount=500, no="C-REAL")
    _cost(db, window.start - timedelta(hours=2), amount=700, no="C-OLD")
    summary = build_cost_summary(db, "DEFAULT", now=window.end)
    amounts = {row["type"]: row["amount"] for row in summary["by_type"]}
    total = sum(row["amount"] for row in summary["by_type"])
    check(f"a future-dated cost is excluded (total {total}, expected 500)",
          total == 500, f"by_type={summary['by_type']}")
    check("...and so is one from before the window", total == 500,
          f"by_type={summary['by_type']}")
    check("...while the cost inside the window is kept",
          amounts.get("Energy") == 500, str(amounts))
    db.close()

    print()
    print("=" * 74)
    print("4. THE HEADLINE DID NOT MOVE — THE CONTROL")
    print("=" * 74)
    # Like #586: this fixes the CHART. If loss_cost changed, the fix narrowed
    # the headline by accident.
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    _run(db, m.id, window.start + timedelta(minutes=30))
    _run(db, m.id, datetime.utcnow() - timedelta(days=2),
         runtime=480, total=100, good=100)
    summary = build_cost_summary(db, "DEFAULT", now=window.end)
    # Derived independently from the records, not from the payload.
    # Rates read from the module, not retyped: a hardcoded rate makes this check
    # fail (or, worse, pass) for a reason that has nothing to do with windows.
    from ai.cost import DOWNTIME_COST_PER_MIN, SCRAP_COST_PER_UNIT
    expected = 0
    for r in db.query(models.ProductionRecord).all():
        if window.start <= r.created_at < window.end:
            expected += (max(0, (r.planned_minutes or 0) - (r.runtime_minutes or 0))
                         * DOWNTIME_COST_PER_MIN)
            expected += (r.rejected_count or 0) * SCRAP_COST_PER_UNIT
    check(f"loss_cost still pools the whole rolling window "
          f"({summary['loss_cost']} vs {expected} derived)",
          summary["loss_cost"] == expected,
          "the headline moved; this change was supposed to fix the chart")
    db.close()

    print()
    print("=" * 74)
    print("4b. ONE ANCHOR, THREADED")
    print("=" * 74)
    # Dropping `now=` leaves the records selected by a window built microseconds
    # after the one the bars and the recorded costs use: a sub-microsecond GAP,
    # not a 24-hour overlap, so no fixture can see it -- mutation testing showed
    # it surviving everything above. The property is "one anchor", not "some
    # number moved", so it is asserted structurally. Same call as #584.
    import os
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ai/cost.py"), encoding="utf-8").read()
    check("the record query uses the request's anchor",
          "_recent_production(db, days=WINDOW_DAYS, now=window.end)" in source,
          "_recent_production is called without the window it is paired with")
    check("...and the recorded-cost query is bounded at both ends by it",
          "models.CostRecord.created_at >= window.start" in source
          and "models.CostRecord.created_at < window.end" in source,
          "the CostRecord query is not bounded by the request's window")

    print()
    print("=" * 74)
    print("5. A MIDNIGHT-ANCHORED WINDOW HAS NO PARTIAL DAY")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    midnight = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    win = oee_contract.OeeWindow(7, now=midnight)
    _run(db, m.id, win.start + timedelta(hours=3))
    summary = build_cost_summary(db, "DEFAULT", now=midnight)
    check(f"a midnight window draws 7 bars ({len(summary['daily'])})",
          len(summary["daily"]) == 7, str([d["date"] for d in summary["daily"]]))
    check("...and marks none partial",
          not any(d.get("partial") for d in summary["daily"]),
          str([d["date"] for d in summary["daily"] if d.get("partial")]))
    check("...and they still sum to the headline",
          sum(d["cost"] for d in summary["daily"]) == summary["loss_cost"],
          f"{sum(d['cost'] for d in summary['daily'])} vs {summary['loss_cost']}")
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


def test_cost_bars_explain_headline():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
