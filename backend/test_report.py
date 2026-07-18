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
    db.commit()

    r = report.build_weekly_report(db, "DEFAULT")
    assert r["has_data"] is True
    md = r["markdown"]
    # the report has the expected sections
    for section in ["# Weekly Plant Report", "## Scorecard", "## Cost of losses",
                    "## Delivery", "## Needs attention", "## Wins"]:
        assert section in md
    # and pulls real figures through
    assert "Plant OEE" in md and "Bugatti" in md and "machine down" in md
    assert "$" in md   # cost of losses rendered as money

    # empty plant -> still a valid (mostly empty) report, no crash
    empty = report.build_weekly_report(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and "# Weekly Plant Report" in empty["markdown"]


if __name__ == "__main__":
    test_weekly_report_composes_a_markdown_page()
    print("REPORT OK: weekly Markdown report composes scorecard/cost/delivery/attention/wins; empty-safe")
