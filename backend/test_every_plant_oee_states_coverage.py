"""The last plant-OEE surfaces say how much of the plant they measured.

THE DEFECT
----------
The OEE contract (docs/engineering/OEE-CONTRACT.md s4): "Every surface showing a
plant OEE must be able to state its coverage." A machine that stops reporting
leaves the pooled figure, so the plant improves exactly when visibility is lost.
#625 and #626 made the scorecard, the briefing, the copilot, the weekly report,
the Executive OEE page and the money story say "from N of M machines". Five
surfaces still did not:

  /oee-trend          the Trends card's verdict. The worst machine went silent and
                      it read "OEE up 16 pts to 72% week on week" in green: an
                      improvement that is the missing machine, stated as a result.
  /analytics/summary  the plant OEE with no coverage beside it, and so
  daily_summary.txt   printed "Plant OEE, last 7 days (pooled): 72%" as the plant.
  /analytics/management  the same, and so
  intelligence .txt   printed the same line in the downloadable report.

THE RULE
--------
Each carries oee_contract.coverage for its own window (the trend: one per week),
and each sentence that states a plant OEE adds oee_contract.coverage_phrase when
the figure did not come from every machine. It is a statement, not a correction:
the missing machine's data does not exist, and the change is still reported.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_every_plant_oee_states_coverage.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import core_routes
import models
import oee_contract
import reports_routes
from ai.oee import build_oee_summary, build_oee_trend
from database import Base

ADMIN = {"tenant": "DEFAULT", "role": "Admin"}
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
    for i in (1, 2, 3):
        db.add(models.Machine(id=i, name=f"M{i}", status="Running", utilization=80, line="L1"))
    db.commit()
    return db


def _run(db, machine_id, days_ago, good):
    # A 80%, P 100%, Q = good%.
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=100, runtime_minutes=80, ideal_cycle_time_seconds=48,
        total_count=100, good_count=good, rejected_count=100 - good,
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def _worst_goes_silent():
    """Last week all three ran, M3 badly; this week M3 reports nothing."""
    db = _session()
    for mid, good in ((1, 90), (2, 90), (3, 30)):
        _run(db, mid, days_ago=9, good=good)
    for mid in (1, 2):
        _run(db, mid, days_ago=2, good=90)
    return db


def _cov(c):
    return (c.get("machines_reporting"), c.get("machines_expected"), c.get("complete")) \
        if isinstance(c, dict) else c


def _plant_line(report):
    return next((ln for ln in report.splitlines() if ln.startswith("Plant OEE")), "")


def _reports(db):
    daily = core_routes.daily_summary_report(db=db, current_user=ADMIN).body.decode("utf-8")
    intel = reports_routes.export_intelligence_summary(db=db, current_user=ADMIN).body.decode("utf-8")
    return {"daily summary .txt": _plant_line(daily), "intelligence report .txt": _plant_line(intel)}


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. THE TREND: THE WORST MACHINE GOES SILENT, THE VERDICT SAYS SO")
    db = _worst_goes_silent()
    t = build_oee_trend(db, "DEFAULT")
    check("the plant OEE rose (the survivorship the contract describes)",
          t["direction"] == "improving" and "OEE up" in t["verdict"], t["verdict"])
    check("the verdict says this week's figure is from 2 of 3 machines",
          "This week's figure is from 2 of 3 machines" in t["verdict"], t["verdict"])
    check("...and says nothing of last week, which every machine reported",
          "Last week" not in t["verdict"] and "last week's" not in t["verdict"], t["verdict"])
    check("the trend carries this week's coverage: 2 of 3",
          _cov(t.get("coverage")) == (2, 3, False), str(t.get("coverage")))
    check("...the same coverage /oee-summary states for the same window",
          t.get("coverage") == build_oee_summary(db, "DEFAULT")["coverage"], str(t.get("coverage")))
    check("...and last week's: 3 of 3", _cov(t.get("prior_coverage")) == (3, 3, True),
          str(t.get("prior_coverage")))
    db.close()

    section("2. CONTROL: EVERY MACHINE, BOTH WEEKS: NOTHING ADDED")
    db = _session()
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=9, good=70)
        _run(db, mid, days_ago=2, good=90)
    t = build_oee_trend(db, "DEFAULT")
    check("the verdict states the change and no coverage",
          "OEE up" in t["verdict"] and " of 3 machine" not in t["verdict"], t["verdict"])
    db.close()

    section("3. EVERY BRANCH THAT STATES A FIGURE STATES ITS COVERAGE")
    db = _session()
    for mid in (1, 2):
        _run(db, mid, days_ago=2, good=90)
    v = build_oee_trend(db, "DEFAULT")["verdict"]
    check("only this week (2 of 3): 'nothing to compare yet', and from 2 of 3 machines",
          "nothing to compare yet" in v and "This week's figure is from 2 of 3 machines" in v, v)
    db.close()

    db = _session()
    for mid in (1, 2):
        _run(db, mid, days_ago=9, good=90)
    v = build_oee_trend(db, "DEFAULT")["verdict"]
    check("only last week (2 of 3): 'no production this week', and last week's from 2 of 3",
          "No production recorded this week" in v
          and "Last week's figure is from 2 of 3 machines" in v, v)
    db.close()

    db = _session()
    for mid in (1, 2):
        _run(db, mid, days_ago=9, good=70)
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=2, good=90)
    v = build_oee_trend(db, "DEFAULT")["verdict"]
    check("last week partial, this week complete: only last week is qualified",
          "Last week's figure is from 2 of 3 machines" in v and "This week's" not in v, v)
    db.close()

    db = _session()
    v = build_oee_trend(db, "DEFAULT")["verdict"]
    check("CONTROL: no production at all states no figure and no coverage",
          v == "No production recorded in the last 14 days.", v)
    db.close()

    section("4. THE SUMMARY ENDPOINTS PUBLISH THEIR COVERAGE")
    db = _worst_goes_silent()
    expected = build_oee_summary(db, "DEFAULT")["coverage"]
    summary = analytics_routes.analytics_summary(db=db, current_user=ADMIN)
    check("/analytics/summary carries 2 of 3", _cov(summary.get("coverage")) == (2, 3, False),
          str(summary.get("coverage")))
    check("...the same as /oee-summary's", summary.get("coverage") == expected)
    mgmt = analytics_routes.get_management_dashboard(db=db, current_user=ADMIN)
    check("/analytics/management carries 2 of 3", _cov(mgmt.get("coverage")) == (2, 3, False),
          str(mgmt.get("coverage")))
    check("...the same as /oee-summary's", mgmt.get("coverage") == expected)

    section("5. THE DOWNLOADABLE REPORTS SAY IT ON THE OEE LINE")
    for name, line in _reports(db).items():
        check(f"{name}: '{line}' says from 2 of 3 machines", "from 2 of 3 machines" in line, line)
    db.close()

    db = _session()
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=2, good=90)
    for name, line in _reports(db).items():
        check(f"CONTROL {name}: every machine reported, nothing added",
              line.startswith("Plant OEE") and " of 3 machine" not in line, line)
    db.close()

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


def test_every_plant_oee_states_coverage():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
