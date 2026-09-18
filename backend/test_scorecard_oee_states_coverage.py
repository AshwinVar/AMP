"""The scorecard's Plant OEE says how much of the plant it measured.

THE DEFECT
----------
The OEE contract (docs/engineering/OEE-CONTRACT.md s4): "Every surface showing a
plant OEE must be able to state its coverage." A machine that stops reporting
leaves the pooled figure, so the metric improves exactly when visibility is lost.
The dashboard's OEE card states it ("2 of 3 machines reporting");
ai/scorecard.build_scorecard took `build_oee_summary(...)["plant"]` and dropped
the `coverage` beside it. The exec home's headline tile read a higher OEE, with a
green arrow, the week the worst machine went silent, and nothing on the tile said
the figure now covered two machines of three.

THE RULE
--------
The Plant OEE KPI carries the coverage /oee-summary computes for the same window
(one computation), and the strip says "from N of M machines" whenever the figure
did not come from every machine.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_scorecard_oee_states_coverage.py
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract
from ai import assistant
from ai.briefing import build_briefing
from ai.oee import build_oee_summary
from ai.report import build_weekly_report
from ai.scorecard import build_scorecard
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


def _oee_kpi(db):
    return next(k for k in build_scorecard(db, "DEFAULT")["kpis"] if k["key"] == "oee")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. THE WORST MACHINE GOES SILENT: THE HEADLINE RISES, AND SAYS WHY")
    db = _session()
    for mid, good in ((1, 90), (2, 90), (3, 30)):       # last week: all three, M3 poor
        _run(db, mid, days_ago=9, good=good)
    for mid in (1, 2):                                   # this week: M3 reports nothing
        _run(db, mid, days_ago=2, good=90)
    kpi = _oee_kpi(db)
    cov = kpi.get("coverage")
    check("the KPI rose (the survivorship the contract describes)",
          kpi["delta"] is not None and kpi["delta"] > 0, f"delta={kpi['delta']!r}")
    check("the KPI carries its coverage", isinstance(cov, dict), f"coverage={cov!r}")
    check("...2 of 3 machines, not complete",
          isinstance(cov, dict) and (cov.get("machines_reporting"), cov.get("machines_expected"),
                                     cov.get("complete")) == (2, 3, False), str(cov))
    check("...the same coverage /oee-summary states for the same window",
          cov == build_oee_summary(db, "DEFAULT")["coverage"], f"{cov} vs the summary's")
    db.close()

    section("2. CONTROL: EVERY MACHINE REPORTED")
    db = _session()
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=2, good=90)
    cov = _oee_kpi(db).get("coverage")
    check("complete coverage says so", isinstance(cov, dict) and cov.get("complete") is True
          and cov.get("machines_reporting") == 3, str(cov))
    db.close()

    section("3. ONLY THE OEE KPI IS A PLANT OEE")
    db = _session()
    _run(db, 1, days_ago=2, good=90)
    others = [k["key"] for k in build_scorecard(db, "DEFAULT")["kpis"]
              if k["key"] != "oee" and "coverage" in k]
    check("no other KPI claims a machine coverage", not others, str(others))
    db.close()

    section("4. EVERY SENTENCE THAT STATES A PLANT OEE STATES ITS COVERAGE")
    # The briefing headline, the copilot's OEE answer and the weekly report's
    # scorecard line are what a person reads; each says "from 2 of 3 machines"
    # when two of three reported, and says nothing extra when all three did.
    for label, reporting in (("2 of 3 reporting", (1, 2)), ("all 3 reporting", (1, 2, 3))):
        db = _session()
        for mid in reporting:
            _run(db, mid, days_ago=2, good=90)
        texts = {
            "briefing headline": build_briefing(db, "DEFAULT")["headline"],
            "copilot answer": assistant.answer(db, "DEFAULT", "what is our OEE")["answer"],
            "weekly report": next(ln for ln in build_weekly_report(db, "DEFAULT")["markdown"].splitlines()
                                  if "Plant OEE" in ln),
        }
        check(f"{label}: the briefing headline's verb agrees with its count",
              "1 thing need attention" not in texts["briefing headline"]
              and re.search(r"\d+ things? needs? attention", texts["briefing headline"]) is not None
              and ("1 thing needs attention" in texts["briefing headline"]
                   or "things need attention" in texts["briefing headline"]),
              texts["briefing headline"])
        for surface, text in texts.items():
            if len(reporting) < 3:
                check(f"{label}: the {surface} says from 2 of 3 machines",
                      "from 2 of 3 machines" in text, text)
            else:
                check(f"{label}: the {surface} adds nothing", " of 3 machine" not in text, text)
        db.close()

    section("5. ONE WORDING")
    check("incomplete: 'from N of M machines'",
          oee_contract.coverage_phrase({"machines_expected": 3, "machines_reporting": 2,
                                        "complete": False}) == "from 2 of 3 machines")
    check("a one-machine plant is singular",
          oee_contract.coverage_phrase({"machines_expected": 1, "machines_reporting": 0,
                                        "complete": False}) == "from 0 of 1 machine")
    check("complete, missing, or no machines: nothing to say",
          [oee_contract.coverage_phrase(c) for c in
           ({"machines_expected": 3, "machines_reporting": 3, "complete": True}, None, {},
            {"machines_expected": 0, "machines_reporting": 0, "complete": False})] == ["", "", "", ""])

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


def test_scorecard_oee_states_coverage():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
