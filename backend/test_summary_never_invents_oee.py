"""A plant that recorded no production has no OEE: /analytics/summary and
/reports/daily-summary.txt must say so, never estimate one.

THE DEFECT
----------
/analytics/summary pools plant OEE over THE window (oee_contract.OeeWindow(7)).
When the window held no production records it did not report "not measured": it
replaced avg_oee with `utilization x 0.9 x 0.95`, a performance and a quality
nobody measured, multiplied by a gauge. The same response said has_data false and
availability, performance and quality 0, so it contradicted itself: 0 x 0 x 0,
headline 47%. daily-summary.txt printed that figure as "Avg OEE: 47%".

It was written for a tenant "before any production exists". Since the summary
moved to the 7-day window, it fires in every week a plant does not run: a
shutdown, a holiday, a line waiting on material. /analytics/executive-oee's
per-machine rows carried the same constants (utilization, 90, 95) and were fixed
for the same reason (test_executive_oee_no_invented_machine_figures.py).

THE RULE
--------
No production measured in the window: avg_oee is what the pooled sums give (0,
with has_data false, the convention every component of this response and
/oee-summary's plant figure already follow), and the text report says "not
measured" rather than printing a number a reader would take as a result.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_summary_never_invents_oee.py
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import core_routes
import models
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
    # Utilization 80 and 30: the estimate was round((68 + 26) / 2) = 47.
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=80, line="L1"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=30, line="L1"))
    db.commit()
    return db


def _production(db, when):
    # 100 planned, 80 run, 100 made at a 48 s ideal cycle, 90 good:
    # A 80%, P 100%, Q 90%, OEE 72%.
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=100, runtime_minutes=80, ideal_cycle_time_seconds=48,
        total_count=100, good_count=90, rejected_count=10, created_at=when))
    db.commit()


def _report(db):
    return core_routes.daily_summary_report(db=db, current_user=ADMIN).body.decode("utf-8")


def _oee_lines(report):
    return [ln for ln in report.splitlines() if "OEE" in ln]


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def check_not_measured(label, db):
    out = analytics_routes.analytics_summary(db=db, current_user=ADMIN)
    check(f"{label}: has_data is false", out["has_data"] is False, str(out["has_data"]))
    check(f"{label}: avg_oee is not estimated from utilization",
          out["avg_oee"] == 0, f"avg_oee {out['avg_oee']} (the estimate was 47)")
    check(f"{label}: avg_oee agrees with its own components",
          out["avg_oee"] == round(out["avg_availability"] * out["avg_performance"]
                                  * out["avg_quality"] / 10000),
          f"OEE {out['avg_oee']} from A {out['avg_availability']} P {out['avg_performance']} "
          f"Q {out['avg_quality']}")
    report = _report(db)
    lines = _oee_lines(report)
    check(f"{label}: the report says the OEE was not measured",
          any("not measured" in ln for ln in lines), str(lines))
    check(f"{label}: the report prints no OEE percentage at all",
          not any(re.search(r"\d+%", ln) for ln in lines), str(lines))
    check(f"{label}: the report prints no availability/performance/quality percentage",
          not re.search(r"(Availability|Performance|Quality): \d+%", report),
          [ln for ln in report.splitlines() if re.search(r"Availability|Performance|Quality", ln)])


def main():
    section("1. A PLANT THAT RAN BEFORE AND NOT THIS WEEK")
    db = _session()
    _production(db, datetime.utcnow() - timedelta(days=20))
    check_not_measured("idle week", db)
    db.close()

    section("2. A PLANT THAT HAS NEVER RECORDED PRODUCTION")
    db = _session()
    check_not_measured("new plant", db)
    db.close()

    section("3. CONTROL: A PLANT THAT RAN THIS WEEK REPORTS ITS MEASURED OEE")
    db = _session()
    _production(db, datetime.utcnow() - timedelta(days=2))
    out = analytics_routes.analytics_summary(db=db, current_user=ADMIN)
    check("measured: has_data is true, avg_oee is the pooled 72%",
          out["has_data"] is True and out["avg_oee"] == 72, f"{out['has_data']}, {out['avg_oee']}")
    report = _report(db)
    lines = _oee_lines(report)
    check("measured: the report prints 72% and names its window",
          any("72%" in ln and "7 days" in ln for ln in lines), str(lines))
    check("measured: the report prints the components",
          all(s in report for s in ("Availability: 80%", "Performance: 100%", "Quality: 90%")),
          report)
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


def test_summary_never_invents_oee():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
