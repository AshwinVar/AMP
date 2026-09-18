"""A shift with no target has no efficiency, on every surface.

THE DEFECT
----------
ai/shift.py states the rule and follows it: "A shift with no planned output has
no attainment -- scoring it 0% ..." (`_attainment` returns None), and the
dashboard's shift table does the same in the browser (lib/shift.ts). Four other
places kept their own copy of the formula and scored it 0%:

  analytics_engine.build_shift_kpis   /analytics/shift-kpis, the intelligence report
  /analytics/executive-oee            the Executive OEE page's shift chart (a 0% bar)
  /reports/shifts.csv                 efficiency_percent 0
  daily-summary.txt                   "Shift Efficiency, all shifts: 0%"

A shift nobody planned read as a shift that produced nothing: an unscheduled
weekend, a shift planned by line rather than by shift, a new tenant before its
first plan.

THE RULE
--------
analytics_engine.shift_attainment(actual, target) is the one formula: None when
there is no target. Every per-shift figure uses it; the Executive chart plots only
shifts with a figure; the CSV leaves the cell empty; the report says "no target";
daily-summary.txt says "not measured" when no shift had a target. The pooled
figure keeps its rule (a no-target shift's output joins the numerator only), and
/analytics/summary keeps its integer convention with a flag beside it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_no_target_no_shift_efficiency.py
"""
import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_engine
import analytics_routes
import core_routes
import models
import reports_routes
import tenancy
from ai import shift as ai_shift
from database import Base

USER = {"tenant": "DEFAULT", "role": "Admin"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session(shifts):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for name, target, actual in shifts:
        db.add(models.ShiftData(shift_name=name, target_output=target, actual_output=actual))
    db.commit()
    return db


MIXED = [("Morning", 1000, 900), ("Weekend", 0, 0), ("Unplanned", 0, 50)]


def _call(fn, **kw):
    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        return fn(**kw)
    finally:
        tenancy.reset_current_tenant(tok)


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. EVERY PER-SHIFT FIGURE: NONE WITHOUT A TARGET, THE RATIO WITH ONE")
    db = _session(MIXED)
    kpis = {r["shift_name"]: r["efficiency"] for r in _call(analytics_routes.get_shift_kpis, db=db, current_user=USER)}
    check("/analytics/shift-kpis", kpis == {"Morning": 90, "Weekend": None, "Unplanned": None}, str(kpis))
    execo = {r["shift_name"]: r["efficiency"]
             for r in _call(analytics_routes.get_executive_oee, db=db, current_user=USER)["shift_oee"]}
    check("/analytics/executive-oee shift_oee", execo == {"Morning": 90, "Weekend": None, "Unplanned": None},
          str(execo))
    resp = _call(reports_routes.export_shifts_csv, db=db, current_user=USER)
    rows = {r["shift_name"]: r["efficiency_percent"] for r in csv.DictReader(io.StringIO(resp.body.decode()))}
    check("/reports/shifts.csv leaves the cell empty", rows == {"Morning": "90", "Weekend": "", "Unplanned": ""},
          str(rows))
    report = _call(reports_routes.export_intelligence_summary, db=db, current_user=USER).body.decode()
    lines = [ln for ln in report.splitlines() if ln.startswith(("Morning:", "Weekend:", "Unplanned:"))]
    check("the intelligence report says 'no target', never 0%",
          any("Weekend:" in ln and "Efficiency=no target" in ln for ln in lines)
          and not any(("Weekend:" in ln or "Unplanned:" in ln) and "Efficiency=0%" in ln for ln in lines)
          and any("Morning:" in ln and "Efficiency=90%" in ln for ln in lines), str(lines))
    db.close()

    section("2. THE POOLED FIGURE KEEPS ITS RULE")
    db = _session(MIXED)
    summary = analytics_routes.analytics_summary(db=db, current_user=USER)
    # (900 + 0 + 50) / 1000: a no-target shift's output joins the numerator only.
    check("/analytics/summary pools 950 of 1000 = 95%, measured",
          summary["avg_shift_efficiency"] == 95 and summary.get("shift_efficiency_measured") is True,
          f"{summary['avg_shift_efficiency']}, {summary.get('shift_efficiency_measured')!r}")
    db.close()

    section("3. NO SHIFT HAD A TARGET: THE SUMMARY SAYS SO")
    db = _session([("Weekend", 0, 0), ("Unplanned", 0, 50)])
    summary = analytics_routes.analytics_summary(db=db, current_user=USER)
    check("/analytics/summary keeps its integer and flags it unmeasured",
          summary["avg_shift_efficiency"] == 0 and summary.get("shift_efficiency_measured") is False,
          f"{summary['avg_shift_efficiency']}, {summary.get('shift_efficiency_measured')!r}")
    text = core_routes.daily_summary_report(db=db, current_user=USER).body.decode()
    line = next((ln for ln in text.splitlines() if ln.startswith("Shift Efficiency")), "")
    check("daily-summary.txt says not measured, not 0%", "not measured" in line and "0%" not in line, line)
    db.close()

    section("4. ONE FORMULA")
    check("ai/shift uses the shared rule", ai_shift._attainment is analytics_engine.shift_attainment)
    check("shift_attainment: None without a target, the rounded ratio with one",
          (analytics_engine.shift_attainment(0, 0), analytics_engine.shift_attainment(50, 0),
           analytics_engine.shift_attainment(900, 1000)) == (None, None, 90))

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


def test_no_target_no_shift_efficiency():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
