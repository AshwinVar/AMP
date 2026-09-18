"""The intelligence report prints the management dashboard's week, and says so.

THE DEFECT
----------
/reports/intelligence-summary.txt ("AMP Daily Factory Intelligence Report")
prints the management summary's figures under the management dashboard's own
labels: Average OEE, Availability, Performance, Quality, Total Downtime, Top
Loss Reason, Worst Machine, Estimated Downtime Loss. /analytics/management pools
them over THE window, oee_contract.OeeWindow(7) (#591). The report pooled ALL
production ever recorded and every downtime log, and said neither. A plant that
ran badly a month ago and well this week read one OEE on the dashboard and
another in the download; a breakdown from last quarter stayed its "Top Loss
Reason" forever. The OEE contract allows all-time only for an explicit "since
commissioning" view, never as the default (docs/engineering/OEE-CONTRACT.md s2).

THE RULE
--------
The report's summary IS the management dashboard's (one computation, so the two
cannot drift), every windowed line names the window, a week with no production
reads "not measured" rather than 0%, and the per-shift section, which covers
every recorded shift, says so.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_intelligence_report_is_the_management_week.py
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import reports_routes
import tenancy
from database import Base

T = "INTEL"
USER = {"tenant": T, "role": "Admin"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    tenancy.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db, rows):
    tok = tenancy.set_current_tenant(T)
    try:
        db.add_all(rows)
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)


def _machines(db):
    _seed(db, [models.Machine(id=1, name="PRESS-01", status="Running", utilization=80),
               models.Machine(id=2, name="CNC-02", status="Running", utilization=60)])


def _run(days_ago, runtime, good, machine_id=1):
    # 100 planned, 100 made at an ideal cycle that makes performance exactly 100%.
    return models.ProductionRecord(
        machine_id=machine_id, planned_minutes=100, runtime_minutes=runtime,
        ideal_cycle_time_seconds=60 * runtime // 100, total_count=100, good_count=good,
        rejected_count=100 - good, created_at=datetime.utcnow() - timedelta(days=days_ago))


def _down(days_ago, reason, duration, machine_id):
    return models.DowntimeLog(machine_id=machine_id, reason=reason, duration=duration,
                              created_at=datetime.utcnow() - timedelta(days=days_ago))


def _both(db):
    tok = tenancy.set_current_tenant(T)
    try:
        report = reports_routes.export_intelligence_summary(db=db, current_user=USER).body.decode("utf-8")
        mgmt = analytics_routes.get_management_dashboard(db=db, current_user=USER)
    finally:
        tenancy.reset_current_tenant(tok)
    return report, mgmt


def _line(report, starts):
    return next((ln for ln in report.splitlines() if ln.startswith(starts)), None)


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. A PLANT THAT RAN BADLY A MONTH AGO AND WELL THIS WEEK")
    db = _session()
    _machines(db)
    _seed(db, [
        _run(days_ago=20, runtime=50, good=50),          # A 50  P 100  Q 50  -> OEE 25
        _run(days_ago=2, runtime=80, good=90),           # A 80  P 100  Q 90  -> OEE 72
        _down(days_ago=20, reason="Breakdown", duration="5 hrs", machine_id=2),
        _down(days_ago=2, reason="Changeover", duration="30 min", machine_id=1),
    ])
    report, mgmt = _both(db)
    oee = _line(report, "Plant OEE")
    check("the dashboard's week is the 72% run", mgmt["avg_oee"] == 72, str(mgmt["avg_oee"]))
    check("the report's plant OEE is the dashboard's, and names the window",
          oee is not None and f"{mgmt['avg_oee']}%" in oee and "last 7 days" in oee, str(oee))
    check("its components are the dashboard's",
          all(f"{name}: {mgmt[key]}%" in report for name, key in
              (("Availability", "avg_availability"), ("Performance", "avg_performance"),
               ("Quality", "avg_quality"))), report)
    down = _line(report, "Total Downtime")
    check("the report's downtime is the dashboard's week (30 min, not 330)",
          down is not None and f"{mgmt['total_downtime_minutes']} minutes" in down
          and "last 7 days" in down and mgmt["total_downtime_minutes"] == 30, str(down))
    reason = _line(report, "Top Loss Reason")
    check("the top loss reason is this week's, not last month's breakdown",
          reason is not None and mgmt["top_loss_reason"] in reason
          and "Breakdown" not in reason and "last 7 days" in reason, str(reason))
    worst = _line(report, "Worst Machine")
    check("the worst machine is the dashboard's", worst is not None
          and str(mgmt["worst_machine"]) in worst and "last 7 days" in worst, str(worst))
    db.close()

    section("2. A WEEK WITH NO PRODUCTION IS NOT MEASURED")
    db = _session()
    _machines(db)
    _seed(db, [_run(days_ago=20, runtime=50, good=50)])
    report, mgmt = _both(db)
    # The summary's lines. (The Active Alerts feed below it is /alerts/smart's,
    # judged from each machine's latest record; it is not the week's summary.)
    summary = report.split("Active Alerts")[0]
    oee_lines = [ln for ln in summary.splitlines() if "OEE" in ln]
    check("the dashboard has no data for the week", mgmt["has_data"] is False, str(mgmt["has_data"]))
    check("the report says the week's OEE was not measured",
          any("not measured" in ln for ln in oee_lines), str(oee_lines))
    check("...and prints no OEE, availability, performance or quality percentage",
          not any(re.search(r"\d+%", ln) for ln in oee_lines)
          and not re.search(r"^(Availability|Performance|Quality): \d+%", report, re.M), report)
    db.close()

    section("3. THE PER-SHIFT SECTION SAYS IT COVERS EVERY RECORDED SHIFT")
    db = _session()
    _machines(db)
    _seed(db, [models.ShiftData(shift_name="Morning", target_output=1000, actual_output=900)])
    report, _ = _both(db)
    check("the shift KPIs are labelled as all recorded shifts",
          "Shift KPIs (all recorded shifts)" in report, report)
    check("CONTROL: and still list the shift",
          "Morning: Target=1000 | Actual=900 | Efficiency=90% | Gap=100" in report, report)
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


def test_intelligence_report_is_the_management_week():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
