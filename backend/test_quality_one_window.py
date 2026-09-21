"""Every surface that publishes a fail rate publishes the SAME one.

THE DEFECT
----------
"Fail rate" was measured over three different spans at once, and no screen said
which:

  * `ai/quality.build_quality_summary` — the last seven CALENDAR dates
    (`created_at >= midnight(today - 6)`, **no upper bound**), feeding the
    Quality intel card, the quality snapshot, the morning briefing's alert, the
    command centre, the copilot context and the assistant;
  * `GET /analytics/quality` — **every inspection ever recorded**, feeding the
    Quality view's "Pass Rate" and "Fail Rate" tiles;
  * `GET /analytics/factory-command-center` — **every inspection ever
    recorded**, feeding the digital twin's "Quality Fail" tile.

`ai/twin._machine_quality` had already been moved onto the canonical rolling
window by #590, so the machine cockpit and the plant disagreed by construction.

Measured on the fixture below — a plant that ran badly last quarter and cleanly
this week — the three surfaces read:

    /analytics/quality        fail rate   21%   (lifetime)
    factory-command-center    fail rate   21%   (lifetime)
    quality intel card        fail rate    1%   (seven calendar dates)

Three true numbers, one label, no basis stated on any of them.

AND A RATE OVER NOTHING WAS ZERO
--------------------------------
`round(part / whole * 100) if whole else 0` was in five places. Zero is the
BEST value on a fail-rate scale, so a plant that inspected nothing published a
perfect quality record — the same defect the shift attainment had (#698) and
the machine health score had (#697).

THE RULE THIS FILE PINS
-----------------------
1. One window — `oee_contract.OeeWindow` — on every plant surface, and each one
   carries the window it was measured over.
2. A window that inspected no units reports None, `measured: False`, and a
   sentence that says so. Never 0%.
3. The trend's halves tile the same window pair, so the level on the trend card
   IS the level on the summary card beside it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_quality_one_window.py
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
import quality_contract                              # noqa: E402
import analytics_routes                              # noqa: E402
from ai import quality, twin                         # noqa: E402
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
    return sessionmaker(bind=engine)()


_seq = [0]


def _insp(db, machine_id, at, inspected, failed, defect="Scratch", rework=0, scrap=0):
    _seq[0] += 1
    db.add(models.QualityInspection(
        inspection_no=f"QW-{_seq[0]}", machine_id=machine_id, inspector="qa",
        inspected_quantity=inspected, passed_quantity=inspected - failed,
        failed_quantity=failed, defect_category=defect,
        rework_quantity=rework, scrap_quantity=scrap, created_at=at))


def _run(db, machine_id, at, total=1000, good=980):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=480, runtime_minutes=460,
        ideal_cycle_time_seconds=20, total_count=total, good_count=good,
        rejected_count=total - good, created_at=at))


def _plant(db, now):
    """A plant that ran badly last quarter and cleanly this week.

    In the window : 1,000 units, 10 failed  ->  1%
    Out of it     : 1,000 units, 400 failed -> 40%  (60 days ago)
    Lifetime      : 2,000 units, 410 failed -> 21%
    """
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"))
    _insp(db, 1, now - timedelta(days=2), inspected=1000, failed=10)
    _insp(db, 2, now - timedelta(days=60), inspected=1000, failed=400, defect="Dent")
    _run(db, 1, now - timedelta(days=2))
    db.commit()


def main():
    print("=" * 74)
    print("1. ONE WINDOW ON EVERY PLANT SURFACE")
    print("=" * 74)
    db = _session()
    now = datetime.utcnow()
    _plant(db, now)

    summary = quality.build_quality_summary(db, "DEFAULT")
    route = analytics_routes.get_quality_analytics(db=db, current_user={})
    centre = analytics_routes.get_factory_command_center(db=db, current_user={})
    machine = twin.build_machine_detail(db, "DEFAULT", 1)["quality"]

    rates = {"intel card (ai/quality)": summary["fail_rate"],
             "/analytics/quality": route["fail_rate"],
             "factory-command-center": centre["quality_fail_rate"]}
    check(f"all three plant surfaces read one fail rate {sorted(set(rates.values()))}",
          len(set(rates.values())) == 1, str(rates))
    check("...and it is the window's 1%, not the lifetime 21%",
          set(rates.values()) == {1}, str(rates))
    check("the machine cockpit agrees on the same machine (1%)",
          machine["fail_rate"] == 1, str(machine))
    check("the totals are the window's too, not the whole register",
          (summary["inspected"], route["inspected_quantity"]) == (1000, 1000),
          f"{summary['inspected']} / {route['inspected_quantity']}")
    check("the contract and the read-model agree unit for unit",
          quality_contract.plant_quality(db, "DEFAULT")["failed"] == summary["failed"] == 10)
    check("every plant surface names the window it measured",
          summary["window"] == route["window"] == centre["quality_window"] == "last 7 days",
          f"{summary['window']} / {route.get('window')} / {centre.get('quality_window')}")
    # The breakdowns have to reconcile with the headline they sit under (rule 3):
    # the out-of-window machine and its defect category are gone from both.
    check("the drill-downs are on the same bounds (no out-of-window Dent)",
          route["defect_counts"] == {"Scratch": 10} and route["machine_failures"] == {1: 10},
          f"{route['defect_counts']} / {route['machine_failures']}")
    check("...and the intel card's Pareto and per-machine list too",
          [d["category"] for d in summary["top_defects"]] == ["Scratch"]
          and [m["machine_id"] for m in summary["by_machine"]] == [1],
          f"{summary['top_defects']} / {summary['by_machine']}")
    db.close()

    print()
    print("=" * 74)
    print("2. A WINDOW THAT INSPECTED NOTHING HAS NO RATE")
    print("=" * 74)
    db = _session()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90))
    # Last quarter was terrible. This week the QA station logged its rounds and
    # inspected nothing: rows exist, units do not, so `inspections` is not the
    # denominator and every rate divides by zero.
    _insp(db, 1, datetime.utcnow() - timedelta(days=60), inspected=1000, failed=400)
    _insp(db, 1, datetime.utcnow() - timedelta(days=1), inspected=0, failed=0, defect="")
    # The plant IS producing — without a production record the briefing returns
    # "no production data yet" and never reaches its quality alert at all, so a
    # fixture without one cannot test that alert (it did not, and a mutation
    # restoring the crashing comparison survived because of it).
    _run(db, 1, datetime.utcnow() - timedelta(days=1))
    db.commit()

    summary = quality.build_quality_summary(db, "DEFAULT")
    route = analytics_routes.get_quality_analytics(db=db, current_user={})
    centre = analytics_routes.get_factory_command_center(db=db, current_user={})
    check("the intel card reports no fail rate, not 0%",
          summary["fail_rate"] is None and summary["measured"] is False, str(summary["fail_rate"]))
    check("...and no first-pass yield either",
          summary["first_pass_yield"] is None, str(summary["first_pass_yield"]))
    check("/analytics/quality reports no rate, not 0%",
          route["fail_rate"] is None and route["pass_rate"] is None and route["measured"] is False,
          f"{route['fail_rate']} / {route['pass_rate']}")
    check("the twin's tile reports no rate, not 0%",
          centre["quality_fail_rate"] is None and centre["quality_measured"] is False,
          str(centre["quality_fail_rate"]))
    cockpit = twin.build_machine_detail(db, "DEFAULT", 1)["quality"]
    check("the machine cockpit's drawer reports no rate, not 0%",
          cockpit["fail_rate"] is None and cockpit["measured"] is False, str(cockpit))
    check("...on a machine that did have an inspection, so it is not an empty card",
          cockpit["inspections"] == 1, str(cockpit))

    # The sentence AMP writes must not read a missing figure aloud.
    from ai import assistant
    said, _ = assistant.say_quality(summary)
    check("the assistant says nothing was inspected rather than '0%'",
          "not measured" in said and "0%" not in said, said)

    # A fact is a figure or a name, never a blank (ai/evidence.Fact): the tool
    # must label the missing rate UNKNOWN rather than ship a None as MEASURED.
    from ai.tools import factory as factory_tools
    result = factory_tools.get_quality_summary(db, "DEFAULT")
    rate_fact = next((f for f in result.facts if f.key == "quality.fail_rate"), None)
    check("the copilot tool states PARTIAL DATA, not a rate",
          result.state == "PARTIAL DATA", result.state)
    check("...and its fail-rate fact is labelled UNKNOWN, not 0",
          rate_fact is not None and rate_fact.value is None and rate_fact.provenance == "UNKNOWN",
          str(rate_fact))

    # The briefing compares the rate against a threshold. `None >= 5` is a
    # TypeError, not a quiet False — this is a crash guard, not a nicety.
    from ai import briefing
    b = briefing.build_briefing(db, "DEFAULT")
    check("the briefing reached its alert list at all (it needs production data)",
          b["has_data"] is True, str(b["headline"]))
    check("the morning briefing does not raise a quality alert off a missing rate",
          not any(a["key"] == "quality" for a in b["alerts"]),
          str([a["key"] for a in b["alerts"]]))
    check("...and claims no clean-quality win either",
          not any("yield" in w["title"].lower() for w in b["wins"]), str(b["wins"]))
    db.close()

    print()
    print("=" * 74)
    print("3. THE TREND'S HALVES TILE, AND ITS LEVEL IS THE SUMMARY'S LEVEL")
    print("=" * 74)
    db = _session()
    at = datetime(2026, 9, 21, 14, 30)
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90))
    # 3.7% this week against 2.0% last week, and one row in the instant the two
    # halves meet: it belongs to the CURRENT half and to no other (half-open).
    _insp(db, 1, at - timedelta(days=2), inspected=1000, failed=37)
    _insp(db, 1, at - timedelta(days=9), inspected=1000, failed=20)
    db.commit()

    summary = quality.build_quality_summary(db, "DEFAULT", now=at)
    trend = quality.build_quality_trend(db, "DEFAULT", now=at)
    check("the trend's current level IS the summary's level (4%)",
          summary["fail_rate"] == trend["current"]["fail_rate"] == 4,
          f"{summary['fail_rate']} vs {trend['current']['fail_rate']}")
    check("...and the movement keeps the precision the level rounds away (+1.7)",
          trend["delta_pts"] == 1.7, str(trend["delta_pts"]))

    window = oee_contract.OeeWindow(quality.WINDOW_DAYS, now=at)
    prior = oee_contract.prior_window(window)
    check("the halves tile: prior.end is current.start", prior.end == window.start)
    boundary = _session()
    boundary.add(models.Machine(id=1, name="M", status="Running", utilization=9))
    _insp(boundary, 1, window.start, inspected=100, failed=100)
    boundary.commit()
    t2 = quality.build_quality_trend(boundary, "DEFAULT", now=at)
    check("a row ON the boundary instant is in the current half and in no other",
          (t2["current"]["inspected"], t2["prior"]["inspected"]) == (100, 0),
          f"{t2['current']['inspected']} / {t2['prior']['inspected']}")
    check("...and it is counted exactly once across the two halves",
          t2["current"]["inspected"] + t2["prior"]["inspected"] == 100)
    boundary.close()
    db.close()

    print()
    print("=" * 74)
    print("4. A SERIES COVERS THE WINDOW AND NOT ONE DAY MORE")
    print("=" * 74)
    # The window is half-open, so the date its END falls on is only inside the
    # span when the window actually reaches into that date. At exactly midnight
    # it does not: `window.end.date()` is tomorrow and nothing happened there.
    # A bar for a date the window never touched is a bar of invented emptiness,
    # and it is the last bucket — the one a reader takes for "today".
    midnight = datetime(2026, 9, 21, 0, 0, 0)
    span, opens_mid_day = oee_contract.window_span(
        oee_contract.OeeWindow(quality.TREND_WINDOW_DAYS, now=midnight))
    check(f"a window ending at midnight touches exactly {quality.TREND_WINDOW_DAYS} dates",
          len(span) == quality.TREND_WINDOW_DAYS, f"{len(span)}: {span[0]}..{span[-1]}")
    check("...ending the day BEFORE the exclusive end, not on it",
          span[-1].isoformat() == "2026-09-20", span[-1].isoformat())
    check("...and it does not open mid-day, so no bucket is partial",
          opens_mid_day is False)

    db = _session()
    db.add(models.Machine(id=1, name="M", status="Running", utilization=9))
    _insp(db, 1, midnight - timedelta(hours=1), inspected=100, failed=5)
    db.commit()
    t = quality.build_quality_trend(db, "DEFAULT", now=midnight)
    check("the trend drawn at midnight has one bar per day of the fortnight",
          len(t["series"]) == quality.TREND_WINDOW_DAYS, str(len(t["series"])))
    check("...and its last bar is the last day the window covers",
          t["series"][-1]["date"] == "2026-09-20", t["series"][-1]["date"])
    check("...which is where the inspection landed",
          t["series"][-1]["inspected"] == 100, str(t["series"][-1]))
    db.close()

    print()
    print("=" * 74)
    print("5. THE CONTRACT READS ONE TENANT, EXPLICITLY")
    print("=" * 74)
    import tenancy as T
    T.install_scoping()
    db = _session()
    tok = T.set_current_tenant("GMATS")
    try:
        db.add(models.Machine(id=1, name="G1", status="Running", utilization=50))
        _insp(db, 1, datetime.utcnow() - timedelta(days=1), inspected=100, failed=5)
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(models.Machine(id=2, name="D1", status="Running", utilization=50))
        _insp(db, 2, datetime.utcnow() - timedelta(days=1), inspected=1000, failed=900)
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    g = quality_contract.plant_quality(db, "GMATS")
    check("GMATS sees its own 5%, never the neighbour's 90%",
          (g["inspected"], g["failed"], g["fail_rate"]) == (100, 5, 5), str(g))
    # The headline is filtered explicitly rather than through the ambient
    # binding, because it is also read from scripts and the AI context builder
    # (ADR-0011). Prove it: no tenant is bound at all here.
    check("...with no tenant bound in the context either",
          T.current_tenant() is None and
          quality_contract.plant_quality(db, "GMATS")["failed"] == 5,
          str(T.current_tenant()))
    db.close()

    print()
    print("=" * 74)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for f in FAILURES:
            print("   *", f)
    else:
        print("ONE WINDOW OK: every plant fail rate is the canonical week, states it, "
              "and says 'not measured' instead of 0%")
    print("=" * 74)
    return 1 if FAILURES else 0


def test_quality_one_window():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
