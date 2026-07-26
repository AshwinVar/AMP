"""Weekly plant report read-model tests (ADR-0007).

Composes scorecard + cost + delivery + briefing into one Markdown report.
Run:  python backend/test_report.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import report


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_weekly_report_composes_a_markdown_page():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    db.add(models.CustomerOrder(order_no="BUG-1", customer_name="Bugatti", product_name="CLB-PCB",
                                order_quantity=100, dispatched_quantity=50, status="Pending",
                                due_date=(now.date() + timedelta(days=10))))
    db.add(models.ComplianceDocument(document_no="SOP-1", title="Reflow SOP", document_type="SOP",
                                     department="Quality", version="1.0", owner="QA Lead",
                                     approval_status="Approved",
                                     review_due_date=(now.date() - timedelta(days=3))))
    # Maintenance: one overdue open task and one completed-on-plan task, so the
    # report's Maintenance section shows both the backlog and a PM-compliance rate.
    db.add(models.MaintenanceTask(task_no="PM-1", machine_id=1, task_type="Preventive",
                                  priority="High", assigned_to="tech",
                                  planned_date=(now.date() - timedelta(days=2)), status="Open"))
    db.add(models.MaintenanceTask(task_no="PM-2", machine_id=1, task_type="Preventive",
                                  priority="Medium", assigned_to="tech",
                                  planned_date=(now.date() - timedelta(days=5)),
                                  completed_date=(now.date() - timedelta(days=6)), status="Completed"))
    db.commit()

    r = report.build_weekly_report(db, "DEFAULT")
    assert r["has_data"] is True
    md = r["markdown"]
    # the report has the expected sections
    for section in ["# Weekly Plant Report", "## Scorecard", "## Cost of losses",
                    "## Delivery", "## Maintenance", "## Compliance", "## Needs attention", "## Wins"]:
        assert section in md
    assert "1 overdue for review" in md
    # maintenance: the overdue backlog and the PM-compliance rate both render
    assert "1 overdue, 0 scheduled over the next 14 days" in md
    assert "PM compliance (30d): 100% completed on plan" in md
    # and pulls real figures through
    assert "Plant OEE" in md and "Bugatti" in md and "machine down" in md
    assert "$" in md   # cost of losses rendered as money

    # empty plant -> still a valid (mostly empty) report, no crash
    empty = report.build_weekly_report(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and "# Weekly Plant Report" in empty["markdown"]


if __name__ == "__main__":
    test_weekly_report_composes_a_markdown_page()
    print("REPORT OK: weekly Markdown report composes scorecard/cost/delivery/attention/wins; empty-safe")
