"""The Executive OEE page and the money-story card say how much of the plant they
measured.

THE DEFECT
----------
OEE contract s4: every surface showing a plant OEE must be able to state its
coverage, because a machine that stops reporting leaves the pooled figure and the
metric improves exactly when visibility is lost. #625 made the scorecard, the
briefing, the copilot and the weekly report say "from N of M machines". Two
screens still showed a plant OEE with nothing to qualify it:

* /analytics/executive-oee -> the Executive OEE page's "Plant OEE" tile;
* ai/recovery.build_recovery_summary -> the money-story card's "closing OEE X% ->
  85%" line and its per-year recovery upside, which a silent poor machine
  shrinks (the gap to world class narrows when the machine furthest from it
  stops reporting).

THE RULE
--------
Both payloads carry the coverage oee_contract computes for the window they pooled,
the same figure /oee-summary states, and the screens say "from N of M machines"
when it is incomplete.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_exec_oee_and_recovery_state_coverage.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
from ai.oee import build_oee_summary
from ai.recovery import build_recovery_summary
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
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=100, runtime_minutes=80, ideal_cycle_time_seconds=48,
        total_count=100, good_count=good, rejected_count=100 - good,
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def _payloads(db):
    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        exec_oee = analytics_routes.get_executive_oee(db=db, current_user={"tenant": "DEFAULT"})
    finally:
        tenancy.reset_current_tenant(tok)
    return exec_oee, build_recovery_summary(db, "DEFAULT"), build_oee_summary(db, "DEFAULT")["coverage"]


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. TWO OF THREE MACHINES REPORTED THIS WEEK")
    db = _session()
    for mid in (1, 2):
        _run(db, mid, days_ago=2, good=90)
    exec_oee, recovery, summary_cov = _payloads(db)
    for name, payload in (("executive OEE", exec_oee), ("recovery", recovery)):
        cov = payload.get("coverage")
        check(f"{name}: carries its coverage", isinstance(cov, dict), f"coverage={cov!r}")
        check(f"{name}: 2 of 3, not complete",
              isinstance(cov, dict) and (cov.get("machines_reporting"), cov.get("machines_expected"),
                                         cov.get("complete")) == (2, 3, False), str(cov))
        check(f"{name}: the same coverage /oee-summary states", cov == summary_cov,
              f"{cov} vs {summary_cov}")
    db.close()

    section("2. CONTROL: EVERY MACHINE REPORTED")
    db = _session()
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=2, good=90)
    exec_oee, recovery, _ = _payloads(db)
    for name, payload in (("executive OEE", exec_oee), ("recovery", recovery)):
        cov = payload.get("coverage") or {}
        check(f"{name}: complete coverage says so",
              cov.get("complete") is True and cov.get("machines_reporting") == 3, str(cov))
    db.close()

    section("3. THE WINDOW IS THE FIGURE'S OWN")
    db = _session()
    for mid in (1, 2, 3):
        _run(db, mid, days_ago=10, good=90)      # last week: all three
    for mid in (1, 2):
        _run(db, mid, days_ago=2, good=90)       # this week: two
    exec_oee, recovery, _ = _payloads(db)
    check("executive OEE: this week's two, not a fortnight's three",
          (exec_oee.get("coverage") or {}).get("machines_reporting") == 2, str(exec_oee.get("coverage")))
    check("recovery: this week's two, not a fortnight's three",
          (recovery.get("coverage") or {}).get("machines_reporting") == 2, str(recovery.get("coverage")))
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


def test_exec_oee_and_recovery_state_coverage():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
