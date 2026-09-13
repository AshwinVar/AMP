"""The machine cockpit measured the same machine over three different weeks.

THE DEFECT
----------
`build_machine_detail` renders one card for one machine, every panel labelled
"last 7 days". It used THREE bases:

  * the **OEE panel** — `_oee_from_records(_recent_production(db, machine_id))`,
    the canonical ROLLING window `[utcnow-7d, utcnow)`, which touches EIGHT
    calendar dates;
  * **production_7d** and **downtime_7d** — the rolling query narrowed by a
    `window_set` of SEVEN calendar dates;
  * **quality** — `created_at >= midnight(today-6)` with **no upper bound at
    all**, so seven calendar dates plus everything dated in the future.

`_machine_quality`'s own docstring claims the opposite:

    "Every other cockpit panel — downtime_7d, production_7d — is the same 7
     days, so one basis for the whole card."

Measured on one machine with a bad run on the partial boundary date, a good run
two days ago, and one inspection dated two days in the FUTURE:

    OEE Quality bar        55%
    Production good rate  100%
    quality fail_rate      34%   (inspected 1500 — 500 of them not yet happened)

The OEE Quality bar and the Production good rate are **the same ratio**, good
over total, on one card, forty-five points apart. Neither is wrong arithmetic;
they are measuring different weeks. And the fail rate counts failures that have
not occurred.

THE FIX
-------
One anchor, one window, for the whole card — the canonical `OeeWindow`, which is
what #586 kept the plant OEE on. The panels that were narrowed to seven calendar
dates now use the window the OEE panel already used, and the daily series derive
their buckets from it (oldest bucket `partial`), so a series tiles the window it
is drawn under. `_machine_quality` is bounded at both ends.

WHAT MOVES: `production_7d`, `downtime_7d`, `quality` and their series widen by
the partial boundary date. The OEE panel does not move — same choice, and the
same reason, as #586: the canonical window is the contract, and the panels that
disagreed with it were the ones that had to change.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_twin_cockpit_one_window.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from ai import twin
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
    m = models.Machine(name="PRESS-01", status="Running", utilization=80,
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


def _inspection(db, machine_id, when, inspected=1000, failed=5, no="QI-1"):
    db.add(models.QualityInspection(
        inspection_no=no, machine_id=machine_id, inspector="X",
        inspected_quantity=inspected, passed_quantity=inspected - failed,
        failed_quantity=failed, created_at=when))
    db.commit()


def _stoppage(db, machine_id, when, no=1):
    db.add(models.DowntimeLog(machine_id=machine_id, reason="Bearing",
                              duration="30 min", created_at=when))
    db.commit()


def main():
    print("=" * 74)
    print("1. THE SAME RATIO READS THE SAME ON ONE CARD")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    # A bad run on the partial boundary date: inside the rolling window the OEE
    # panel used, outside the seven calendar dates the others used.
    _run(db, m.id, window.start + timedelta(minutes=20),
         runtime=60, total=1000, good=100)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2))
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    oee_q = detail["oee"]["quality"]
    good_rate = detail["production_7d"]["good_rate"]
    check(f"the OEE Quality bar and the Production good rate agree "
          f"({oee_q} vs {good_rate})", oee_q == good_rate,
          "the same ratio is being measured over two different weeks")
    check("...and both saw the boundary run, so that is not agreement on nothing",
          detail["production_7d"]["total"] == 2000,
          f"total={detail['production_7d']['total']} (expected both runs)")
    db.close()

    print()
    print("=" * 74)
    print("2. QUALITY IS BOUNDED AT BOTH ENDS")
    print("=" * 74)
    # The query had no upper bound, so failures that have not happened yet were
    # in the cockpit's fail rate.
    db = _session()
    m = _machine(db)
    _inspection(db, m.id, datetime.utcnow() + timedelta(days=2),
                inspected=500, failed=500, no="QI-FUTURE")
    _inspection(db, m.id, datetime.utcnow() - timedelta(days=2),
                inspected=1000, failed=5, no="QI-REAL")
    _inspection(db, m.id, oee_contract.OeeWindow(7).start - timedelta(hours=2),
                inspected=800, failed=800, no="QI-OLD")
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    q = detail["quality"]
    check(f"a future-dated inspection is excluded (inspected {q['inspected']}, "
          f"expected 1000)", q["inspected"] == 1000, str(q))
    check("...and so is one from before the window", q["inspected"] == 1000, str(q))
    check(f"...so the fail rate is the real one ({q['fail_rate']}%)",
          q["fail_rate"] < 5, str(q))
    db.close()

    # Downtime has the same upper end, and it is reachable in a way the others
    # are not: a stoppage timestamped LATER TODAY carries today's date, which is
    # in the span, so an unbounded query counts it into today's bar. Mutation
    # testing caught the omission.
    db = _session()
    m = _machine(db)
    _stoppage(db, m.id, datetime.utcnow() + timedelta(hours=2))
    _stoppage(db, m.id, datetime.utcnow() - timedelta(hours=2))
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    counted = sum(d["count"] for d in detail["downtime_7d"])
    check(f"a stoppage timestamped later today is not counted ({counted})",
          counted == 1, f"downtime_7d={detail['downtime_7d']}")
    db.close()

    print()
    print("=" * 74)
    print("3. EVERY SERIES TILES THE WINDOW IT IS DRAWN UNDER")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    _run(db, m.id, window.start + timedelta(minutes=20))
    _stoppage(db, m.id, window.start + timedelta(minutes=25))
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    touched = len({(window.start + timedelta(hours=h)).date()
                   for h in range(0, 7 * 24 + 1)})
    for key, series in (("production_7d.daily", detail["production_7d"]["daily"]),
                        ("downtime_7d", detail["downtime_7d"])):
        dates = [d["date"] for d in series]
        check(f"{key} covers every calendar date the window touches "
              f"({len(dates)} vs {touched})", len(dates) == touched, str(dates))
        check(f"...and {key} includes the boundary date",
              window.start.date().isoformat() in dates, str(dates))
    check("the production series sums to the panel's good count",
          sum(d["count"] for d in detail["production_7d"]["daily"])
          == detail["production_7d"]["good"],
          f"{sum(d['count'] for d in detail['production_7d']['daily'])} vs "
          f"{detail['production_7d']['good']}")
    db.close()

    print()
    print("=" * 74)
    print("4. THE OEE PANEL DID NOT MOVE — THE CONTROL")
    print("=" * 74)
    # Like #586: the canonical window is the contract. The panels that disagreed
    # with it are the ones that changed.
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    _run(db, m.id, window.start + timedelta(minutes=20),
         runtime=60, total=1000, good=100)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2))
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    from analytics_engine import pooled_oee
    everything = db.query(models.ProductionRecord).all()
    check(f"the OEE panel still pools the whole rolling window "
          f"({detail['oee']['oee']} vs {pooled_oee(everything)['oee']})",
          detail["oee"]["oee"] == pooled_oee(everything)["oee"],
          "the OEE panel moved; this change was supposed to move the others")
    db.close()

    # ONE anchor, threaded to all four panels. Dropping `now=` on any of them
    # leaves that panel measuring a window built microseconds later: a
    # sub-microsecond gap no fixture can see, which is why mutation testing
    # showed it surviving everything above. The property is "one anchor", so it
    # is asserted structurally — the same call as #584 and #587.
    import os
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ai/twin.py"), encoding="utf-8").read()
    for panel in ("_machine_production(db, machine_id, now=window.end)",
                  "_machine_quality(db, machine_id, now=window.end)",
                  "_downtime_trend(db, machine_id, now=window.end)",
                  "_recent_production(db, machine_id, now=window.end)"):
        check(f"the card threads its anchor into {panel.split('(')[0]}",
              panel in source, f"{panel} is called without the card's anchor")

    print()
    print("=" * 74)
    print("5. AN EMPTY MACHINE IS STILL EMPTY, NOT WRONG")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    detail = twin.build_machine_detail(db, "DEFAULT", m.id)
    check("no production, no crash", detail["production_7d"]["total"] == 0,
          str(detail["production_7d"]))
    check("...no inspections, no fail rate", detail["quality"]["inspected"] == 0,
          str(detail["quality"]))
    check("...and the series are still drawn, at zero",
          len(detail["downtime_7d"]) > 0
          and all(d["count"] == 0 for d in detail["downtime_7d"]),
          str(detail["downtime_7d"]))
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


def test_twin_cockpit_one_window():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
