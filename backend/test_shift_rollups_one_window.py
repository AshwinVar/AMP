"""Four shift attainments measured three spans; now they measure one.

THE DEFECT
----------
`analytics_engine.shift_attainment` has been THE formula since
test_no_target_no_shift_efficiency.py (pooled, None without a target). The
SPAN it was applied over was never unified:

    ai/shift.build_shift_summary       the last 7 days (its own rolling cutoff)
    /analytics/summary                 every shift ever recorded
    /analytics/management              every shift ever recorded, three lines
                                       under a production sum that IS windowed
    /analytics/executive-oee           the most recent 50 ROWS

A plant that ran badly last quarter and well this week read three different
attainments for the same shifts on the same day, and the comment beside the
lifetime sum said it was "the same basis as the shift read-model". The
standard had been applied to the formula and not to the window — the shape
test_oee_rollups_one_window.py records for the plant OEE.

THE FIX
-------
`shift_contract.pooled_attainment(db, tenant, window)` is the one function,
over the canonical `oee_contract.OeeWindow` every OEE figure pools; every
surface reads it, and says its window and whether it measured anything.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_shift_rollups_one_window.py
"""
import inspect
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import core_routes
import models
import oee_contract
import shift_contract
from ai import shift as ai_shift
from database import Base

failures = []
ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


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


def _shift(db, name, target, actual, days_ago=0, tenant="DEFAULT"):
    db.add(models.ShiftData(tenant_code=tenant, shift_name=name, target_output=target,
                            actual_output=actual,
                            created_at=datetime.utcnow() - timedelta(days=days_ago)))


def _plant(db):
    """A plant that ran badly last quarter and well this week — the case where
    the spans disagree. This week: 900 of 1000 (90%). Last quarter: 3000 of
    10000 (30%). Lifetime: 3900 of 11000 = 35%. A 'most recent 50 rows' window
    holds all of it too, since there are only five rows."""
    db.add(models.Machine(name="PRESS-01", status="Running", utilization=80, downtime="0 min"))
    _shift(db, "Morning", 500, 450, days_ago=1)
    _shift(db, "Night", 500, 450, days_ago=2)
    _shift(db, "Morning", 5000, 1500, days_ago=40)
    _shift(db, "Night", 5000, 1500, days_ago=50)
    _shift(db, "Weekend", 0, 60, days_ago=3)          # unplanned: joins the numerator only
    db.commit()


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. ONE WINDOW: EVERY PLANT ATTAINMENT IS THIS WEEK'S 96%, NOT LIFETIME'S 35%")
    db = _session()
    _plant(db)
    # (450 + 450 + 60) / (500 + 500) = 960 / 1000 = 96
    plant = shift_contract.pooled_attainment(db, "DEFAULT")
    check("the contract pools the week: 96", plant["attainment"] == 96, str(plant))
    check("...and says its basis", plant["measured"] is True and plant["window"] == "last 7 days"
          and plant["days"] == 7 and plant["entries"] == 3, str(plant))
    card = ai_shift.build_shift_summary(db, "DEFAULT")
    summary = analytics_routes.analytics_summary(db=db, current_user=ADMIN)
    mgmt = analytics_routes.get_management_dashboard(db=db, current_user=ADMIN)
    execo = analytics_routes.get_executive_oee(db=db, current_user=ADMIN)
    figures = {
        "ai/shift card": card["attainment"],
        "/analytics/summary": summary["avg_shift_efficiency"],
        "/analytics/management": mgmt["target_achievement"],
        "/analytics/executive-oee": execo["production_achievement"],
    }
    check("all four surfaces say 96", all(v == 96 for v in figures.values()), str(figures))
    check("none of them says the lifetime 35", all(v != 35 for v in figures.values()), str(figures))
    check("the card's breakdown sums to its headline",
          sum(s["actual"] for s in card["shifts"]) == card["actual"] == 960
          and sum(s["target"] for s in card["shifts"]) == card["target"] == 1000, str(card))
    check("the executive chart's rows sum to its headline",
          sum(r["actual_output"] for r in execo["shift_oee"]) == execo["production_actual"] == 960
          and len(execo["shift_oee"]) == 3, str(execo["shift_oee"]))
    check("every surface names the window beside the figure",
          card["window"] == summary["shift_window"] == mgmt["shift_window"] == execo["shift_window"]
          == "last 7 days", f"{card.get('window')} {summary.get('shift_window')} "
                            f"{mgmt.get('shift_window')} {execo.get('shift_window')}")
    check("every surface says it measured something",
          card["measured"] and summary["shift_efficiency_measured"]
          and mgmt["target_achievement_measured"] and execo["production_achievement_measured"])
    text = core_routes.daily_summary_report(db=db, current_user=ADMIN).body.decode()
    line = next((ln for ln in text.splitlines() if ln.startswith("Shift Efficiency")), "")
    check("daily-summary.txt states the same window and figure",
          "last 7 days" in line and "96%" in line and "all shifts" not in line, line)
    db.close()

    section("2. NOTHING PLANNED THIS WEEK: NOT MEASURED ON EVERY SURFACE, NEVER 0%")
    db = _session()
    db.add(models.Machine(name="PRESS-01", status="Running", utilization=80, downtime="0 min"))
    _shift(db, "Morning", 5000, 4900, days_ago=40)     # a planned shift, last quarter
    _shift(db, "Weekend", 0, 60, days_ago=1)           # this week: output, no target
    db.commit()
    plant = shift_contract.pooled_attainment(db, "DEFAULT")
    check("the contract: None, not 0, and unmeasured",
          plant["attainment"] is None and plant["measured"] is False and plant["actual"] == 60, str(plant))
    card = ai_shift.build_shift_summary(db, "DEFAULT")
    summary = analytics_routes.analytics_summary(db=db, current_user=ADMIN)
    mgmt = analytics_routes.get_management_dashboard(db=db, current_user=ADMIN)
    execo = analytics_routes.get_executive_oee(db=db, current_user=ADMIN)
    check("the card says None", card["attainment"] is None and card["measured"] is False, str(card))
    check("the legacy integers keep their 0 and flag it unmeasured",
          summary["avg_shift_efficiency"] == 0 and summary["shift_efficiency_measured"] is False
          and mgmt["target_achievement"] == 0 and mgmt["target_achievement_measured"] is False
          and execo["production_achievement"] == 0 and execo["production_achievement_measured"] is False,
          f"{summary['avg_shift_efficiency']}/{summary['shift_efficiency_measured']} "
          f"{mgmt['target_achievement']}/{mgmt['target_achievement_measured']} "
          f"{execo['production_achievement']}/{execo['production_achievement_measured']}")
    check("none of them reaches back to last quarter's 98",
          summary["avg_shift_efficiency"] != 98 and mgmt["target_achievement"] != 98
          and execo["production_achievement"] != 98)
    text = core_routes.daily_summary_report(db=db, current_user=ADMIN).body.decode()
    line = next((ln for ln in text.splitlines() if ln.startswith("Shift Efficiency")), "")
    check("daily-summary.txt says not measured for this window", "not measured" in line and "0%" not in line, line)
    db.close()

    section("3. THE WINDOW IS THE OEE WINDOW, HALF-OPEN, AND TENANT-EXPLICIT")
    check("ai/shift's WINDOW_DAYS is the canonical week",
          ai_shift.WINDOW_DAYS == oee_contract.DEFAULT_WINDOW_DAYS == 7)
    src = inspect.getsource(shift_contract._sums)
    check("the sums are filtered by tenant explicitly and by the half-open window",
          "models.ShiftData.tenant_code == tenant" in src and "created_at < window.end" in src
          and "created_at >= window.start" in src, src[:200])
    db = _session()
    _shift(db, "Morning", 100, 90, days_ago=1, tenant="DEFAULT")
    _shift(db, "Morning", 100, 10, days_ago=1, tenant="OTHER")
    db.commit()
    check("another tenant's shift never enters the figure",
          shift_contract.pooled_attainment(db, "DEFAULT")["attainment"] == 90
          and shift_contract.pooled_attainment(db, "OTHER")["attainment"] == 10)
    # A caller's own instant builds the window: the same anchor gives the same
    # window, so a brief replaying a day sees that day's shifts.
    at = datetime.utcnow() - timedelta(days=30)
    then = shift_contract.pooled_attainment(db, "DEFAULT", oee_contract.OeeWindow(7, now=at))
    check("a window ending 30 days ago holds none of this week's shifts",
          then["entries"] == 0 and then["attainment"] is None, str(then))
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


def test_shift_rollups_one_window():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
