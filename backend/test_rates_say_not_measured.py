"""A rate over nothing is not zero, on the six tiles that still said it was.

THE RULE
--------
`round(part / whole * 100) if whole else 0` publishes a real reading for an
empty denominator. Which reading depends on the scale, and every one of them is
a lie a manager can act on:

    "Achievement 0%"     no work order carries a target -> the book read as a
                         total failure to deliver
    "Quality 0%"         no job logged a unit -> every part the crew made read
                         as scrap
    "Avg Repair 0m"      no task has been completed -> the best possible
                         maintenance record
    "Autonomy 0%"        no agent decision has been made -> every decision read
                         as needing a human
    "Avg Utilization 0%" no machine has reported -> an idle plant

#697 closed this on the machine health score, #698 on the shift attainment and
#699 on the quality rates. This file closes it on the six that were left, and
draws the line the rule actually has:

    A COUNT or a SUM over an empty set really IS zero. Money not spent is zero
    money. The rule is about an empty DENOMINATOR, not an empty table — so
    `total_cost` stays a number and is pinned here as such.

AND ONE WINDOW, WHILE WE ARE HERE
---------------------------------
Two of these figures also disagreed about their span:

  * the command header's Autonomy tile showed the LIFETIME auto-approval rate
    under a caption reading "N actions / 7d" — the only window named on the
    tile, attached to the denominator of an all-time rate. `ai/impact` also cut
    its own `utcnow() - 7d` with no upper bound, so a future-dated agent action
    inflated the count beside it;
  * `/analytics/final-executive-summary` pooled a FOURTH lifetime copy of
    `passed / inspected` — the figure #699 had just unified across three
    surfaces — so an executive page could contradict the Quality view beside it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_rates_say_not_measured.py
"""
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import analytics_routes                              # noqa: E402
import core_routes                                   # noqa: E402
import models                                        # noqa: E402
import oee_contract                                  # noqa: E402
from ai import impact, pulse                         # noqa: E402
from database import Base                            # noqa: E402

# A fixed calendar day for the NOT NULL date columns the fixtures need; the
# figures under test do not read it (date_basis guard: never date.today()).
DAY = datetime(2026, 9, 21).date()

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


def main():
    print("=" * 74)
    print("1. AN EMPTY DENOMINATOR IS NOT MEASURED, ON EVERY ONE OF THE SIX")
    print("=" * 74)
    db = _session()
    # Rows exist, but none of them carries a denominator: work orders with no
    # target, a plan with nothing planned, a maintenance task not yet completed,
    # an operator job that logged no unit, a machine that has never reported.
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=0))
    db.add(models.WorkOrder(work_order_no="WO-1", part_number="P-1", batch_number="B-1",
                            status="Planned", target_quantity=0, actual_quantity=0))
    db.add(models.ProductionPlan(plan_no="PP-1", status="Planned", plan_date=DAY,
                                 shift_name="A", planned_quantity=0, actual_quantity=0))
    db.add(models.MaintenanceTask(task_no="MT-1", machine_id=1, task_type="Preventive",
                                  assigned_to="Ada", planned_date=datetime.utcnow(),
                                  status="Open", downtime_minutes=0))
    db.add(models.OperatorJobExecution(execution_no="OJ-1", operator_name="Ada",
                                       machine_id=1, job_status="Started",
                                       good_count=0, rejected_count=0))
    db.commit()
    # A machine with NO reading at all — the ORM default would write 0, which is
    # a reading, so the NULL is set explicitly (the #407 trap).
    db.execute(text("UPDATE machines SET utilization = NULL WHERE id = 1"))
    db.commit()
    db.expire_all()

    wo = analytics_routes.get_work_order_analytics(db=db, current_user={})
    check("work orders: no target anywhere -> achievement not measured",
          wo["achievement"] is None and wo["achievement_measured"] is False, str(wo["achievement"]))
    check("...and the count beside it is still a real 1",
          wo["total_work_orders"] == 1, str(wo["total_work_orders"]))

    plan = analytics_routes.get_production_plan_analytics(db=db, current_user={})
    check("production plans: nothing planned -> achievement not measured",
          plan["achievement"] is None and plan["achievement_measured"] is False,
          str(plan["achievement"]))

    maint = analytics_routes.get_maintenance_analytics(db=db, current_user={})
    check("maintenance: no completed task -> average repair not measured",
          maint["avg_repair_minutes"] is None and maint["avg_repair_measured"] is False,
          str(maint["avg_repair_minutes"]))

    op = analytics_routes.get_operator_terminal_analytics(db=db, current_user={})
    check("operator terminal: no unit logged -> quality rate not measured",
          op["quality_rate"] is None and op["quality_measured"] is False, str(op["quality_rate"]))
    check("...and the job it came from is still counted",
          op["total_jobs"] == 1, str(op["total_jobs"]))

    summary = analytics_routes.analytics_summary(db=db, current_user={})
    check("the plant: no machine has reported -> average utilization not measured",
          summary["avg_utilization"] is None and summary["utilization_measured"] is False,
          str(summary["avg_utilization"]))
    check("...and it says it covers 0 of the 1 machine on the floor",
          summary["utilization_machines"] == 0 and summary["machines"] == 1, str(summary["machines"]))

    imp = impact.build_impact(db, "DEFAULT")
    check("agents: nothing decided -> autonomy not measured",
          imp["auto_rate"] is None and imp["auto_measured"] is False, str(imp["auto_rate"]))
    p = pulse.build_pulse(db, "DEFAULT")
    check("...and the command header's tile says so too",
          p["agents"]["auto_rate"] is None and p["agents"]["auto_measured"] is False,
          str(p["agents"]["auto_rate"]))

    ex = analytics_routes.get_final_executive_summary(db=db, current_user={})
    check("the executive page: nothing inspected -> quality rate not measured",
          ex["quality_rate"] is None and ex["quality_measured"] is False, str(ex["quality_rate"]))
    check("...and no order raised -> dispatch rate not measured",
          ex["dispatch_rate"] is None and ex["dispatch_measured"] is False, str(ex["dispatch_rate"]))

    print()
    print("=" * 74)
    print("2. A SUM OVER AN EMPTY SET REALLY IS ZERO")
    print("=" * 74)
    # The line the rule actually draws. Money not spent is zero money; a count
    # of nothing is zero things. Turning these into None would be the same
    # defect from the other side — refusing to state a figure we do have.
    check("total cost of an empty register is 0, not 'not measured'",
          ex["total_cost"] == 0, str(ex["total_cost"]))
    check("...and so are the counts beside every unmeasured rate",
          (wo["total_target"], plan["planned_quantity"], maint["total_downtime_minutes"],
           op["good_count"], ex["low_stock_items"]) == (0, 0, 0, 0, 0),
          str((wo["total_target"], plan["planned_quantity"], maint["total_downtime_minutes"],
               op["good_count"], ex["low_stock_items"])))

    print()
    print("=" * 74)
    print("3. A REAL DENOMINATOR STILL PRODUCES A REAL RATE")
    print("=" * 74)
    # The other half of the guard: None must mean "nothing to divide by", never
    # "this endpoint stopped working".
    db2 = _session()
    db2.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db2.add(models.Machine(id=2, name="M2", status="Idle", utilization=40))
    db2.add(models.WorkOrder(work_order_no="WO-2", part_number="P-2", batch_number="B-2",
                             status="Completed", target_quantity=100, actual_quantity=90))
    db2.add(models.ProductionPlan(plan_no="PP-2", status="Completed", plan_date=DAY,
                                  shift_name="A", planned_quantity=200, actual_quantity=150))
    db2.add(models.MaintenanceTask(task_no="MT-2", machine_id=1, task_type="Breakdown",
                                   assigned_to="Ada", planned_date=datetime.utcnow(),
                                   status="Completed", downtime_minutes=60))
    db2.add(models.OperatorJobExecution(execution_no="OJ-2", operator_name="Ada", machine_id=1,
                                        job_status="Completed", good_count=90, rejected_count=10))
    db2.commit()
    check("work-order achievement is 90% and says it measured it",
          analytics_routes.get_work_order_analytics(db=db2, current_user={})["achievement"] == 90)
    check("plan achievement is 75%",
          analytics_routes.get_production_plan_analytics(db=db2, current_user={})["achievement"] == 75)
    m2 = analytics_routes.get_maintenance_analytics(db=db2, current_user={})
    check("average repair is 60 minutes over the one completed task",
          m2["avg_repair_minutes"] == 60 and m2["avg_repair_measured"] is True, str(m2["avg_repair_minutes"]))
    check("operator quality is 90%",
          analytics_routes.get_operator_terminal_analytics(db=db2, current_user={})["quality_rate"] == 90)
    s2 = analytics_routes.analytics_summary(db=db2, current_user={})
    check("average utilization is 60% across both reporting machines",
          s2["avg_utilization"] == 60 and s2["utilization_machines"] == 2, str(s2["avg_utilization"]))
    db2.close()

    print()
    print("=" * 74)
    print("4. THE AUTONOMY TILE'S RATE IS THE WEEK ITS CAPTION NAMES")
    print("=" * 74)
    db3 = _session()
    at = datetime(2026, 9, 21, 14, 30)
    window = oee_contract.OeeWindow(impact.WINDOW_DAYS, now=at)

    def action(no, status, decided_by, when):
        db3.add(models.AgentAction(
            agent="reorder", action_type="propose", summary=f"A{no}", status=status,
            decided_by=decided_by, ref_kind="purchase_order", severity="low",
            created_at=when))

    # LAST YEAR the fleet decided four things and auto-approved all four.
    for i in range(4):
        action(i, "Approved", "auto-policy", at - timedelta(days=200 + i))
    # THIS WEEK a human decided the only one there was.
    action(9, "Approved", "ashwin", at - timedelta(days=1))
    db3.commit()

    imp3 = impact.build_impact(db3, "DEFAULT", now=at)
    check("the lifetime rate is 80% (4 of 5 decisions auto-approved)",
          imp3["auto_rate"] == 80, str(imp3["auto_rate"]))
    check("...and says it is all time", imp3["window"] == "all time", imp3["window"])
    check("the week's rate is 0% — a REAL zero: one decision, made by a human",
          imp3["last_7_days"]["auto_rate"] == 0 and imp3["last_7_days"]["measured"] is True,
          str(imp3["last_7_days"]))
    check("...over the week's one decision, not the lifetime five",
          imp3["last_7_days"]["decided"] == 1, str(imp3["last_7_days"]["decided"]))
    check("...and it names the window", imp3["last_7_days"]["window"] == "last 7 days",
          imp3["last_7_days"]["window"])
    # THE TILE ITSELF. The header's caption names a week, so the header's rate
    # has to be that week's — 0%, not the lifetime 80% it used to show under it.
    p3 = pulse.build_pulse(db3, "DEFAULT", now=at)
    check("the command header shows the WEEK's 0%, not the lifetime 80%",
          p3["agents"]["auto_rate"] == 0, str(p3["agents"]["auto_rate"]))
    check("...beside the count of actions it is actually over",
          p3["agents"]["actions_7d"] == 1 and p3["agents"]["auto_decided"] == 1,
          str(p3["agents"]))
    check("...and it names that week on the tile",
          p3["agents"]["auto_window"] == "last 7 days", str(p3["agents"]["auto_window"]))

    # A future-dated row: the old cutoff had NO upper bound, so a skewed gateway
    # clock inflated the very count the tile's caption reports.
    action(20, "Approved", "auto-policy", at + timedelta(days=3))
    db3.commit()
    imp4 = impact.build_impact(db3, "DEFAULT", now=at)
    check("an action dated in the future is outside the week it has not happened in",
          imp4["last_7_days"]["total"] == 1, str(imp4["last_7_days"]["total"]))
    check("...so it cannot move the week's autonomy rate either",
          imp4["last_7_days"]["auto_rate"] == 0, str(imp4["last_7_days"]["auto_rate"]))
    check("the window's start is the contract's, not a private cutoff",
          imp4["last_7_days"]["days"] == oee_contract.DEFAULT_WINDOW_DAYS
          and window.start is not None)
    db3.close()

    print()
    print("=" * 74)
    print("5. THE EXECUTIVE PAGE READS THE ONE QUALITY CONTRACT")
    print("=" * 74)
    db4 = _session()
    now = datetime.utcnow()
    db4.add(models.QualityInspection(inspection_no="Q-NOW", inspector="I", machine_id=1,
                                     inspected_quantity=100, passed_quantity=99,
                                     failed_quantity=1, defect_category="Scratch",
                                     created_at=now - timedelta(days=2)))
    db4.add(models.QualityInspection(inspection_no="Q-OLD", inspector="I", machine_id=1,
                                     inspected_quantity=100, passed_quantity=10,
                                     failed_quantity=90, defect_category="Dent",
                                     created_at=now - timedelta(days=60)))
    db4.commit()
    ex2 = analytics_routes.get_final_executive_summary(db=db4, current_user={})
    qv = analytics_routes.get_quality_analytics(db=db4, current_user={})
    check("the executive page's quality rate is the window's 99%, not the lifetime 54%",
          ex2["quality_rate"] == 99, str(ex2["quality_rate"]))
    check("...which is the SAME figure the Quality view publishes",
          ex2["quality_rate"] == qv["pass_rate"], f"{ex2['quality_rate']} vs {qv['pass_rate']}")
    check("...and it names the window it read",
          ex2["quality_window"] == qv["window"] == "last 7 days", str(ex2.get("quality_window")))
    db4.close()

    print()
    print("=" * 74)
    print("6. THE DAILY SUMMARY SAYS IT, IN WORDS")
    print("=" * 74)
    ADMIN = {"role": "Admin", "tenant": "DEFAULT"}
    report = core_routes.daily_summary_report(db=db, current_user=ADMIN).body.decode("utf-8")
    check("an unreported plant reads 'not measured', never 'Avg Utilization: 0%'",
          "Avg Utilization: not measured" in report and "Avg Utilization: 0%" not in report,
          [ln for ln in report.splitlines() if "Utilization" in ln])
    db.close()

    db5 = _session()
    db5.add(models.Machine(id=1, name="A", status="Running", utilization=90))
    db5.add(models.Machine(id=2, name="B", status="Idle", utilization=0))
    db5.commit()
    db5.execute(text("UPDATE machines SET utilization = NULL WHERE id = 2"))
    db5.commit()
    db5.expire_all()
    partial = core_routes.daily_summary_report(db=db5, current_user=ADMIN).body.decode("utf-8")
    check("a partly-reporting plant states its coverage beside the figure",
          "Avg Utilization: 90% (from 1 of 2 machines)" in partial,
          [ln for ln in partial.splitlines() if "Utilization" in ln])
    db5.close()

    print()
    print("=" * 74)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for f in FAILURES:
            print("   *", f)
    else:
        print("NOT-MEASURED OK: six rates say when they measured nothing, sums and "
              "counts stay 0, and the autonomy tile reads the week it names")
    print("=" * 74)
    return 1 if FAILURES else 0


def test_rates_say_not_measured():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
