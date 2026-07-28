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
    # Week ahead: one job booked tomorrow (the schedule-load section).
    db.add(models.ProductionSchedule(schedule_no="SCH-1", machine_id=1, shift_name="Shift A",
                                     scheduled_date=(now.date() + timedelta(days=1)),
                                     planned_quantity=100, estimated_minutes=480, status="Scheduled"))
    # Supply: one overdue PO from a named supplier (the supply section).
    db.add(models.Supplier(id=9, supplier_code="SUP-9", supplier_name="Acme Metals"))
    db.add(models.PurchaseOrder(po_no="PO-9", supplier_id=9, item_name="Solder wire", unit="kg",
                                order_quantity=10, received_quantity=0,
                                expected_delivery_date=(now.date() - timedelta(days=3)), status="Open"))
    # Stock integrity: stock with no movement in the window -> dead.
    db.add(models.InventoryItem(item_code="IT-9", item_name="Bracket", category="Parts",
                                unit="pcs", current_stock=50, reorder_level=0))
    # Work in progress: one open WO past its planned end, 4 days old.
    wo = models.WorkOrder(work_order_no="WO-R1", part_number="P-9", batch_number="B-9",
                          target_quantity=100, actual_quantity=20, status="Planned",
                          material_state="RAW",
                          planned_end=now - timedelta(days=1))
    wo.created_at = now - timedelta(days=4)
    db.add(wo)
    # Crew: one operator, two jobs (one completed), 90% pooled quality.
    for i, (status, good, rej) in enumerate([("Completed", 45, 5), ("Started", 45, 5)]):
        ex = models.OperatorJobExecution(execution_no=f"EX-{i}", operator_name="lee",
                                         machine_id=1, job_status=status,
                                         good_count=good, rejected_count=rej)
        ex.started_at = now - timedelta(days=1)
        db.add(ex)
    # Escalations: one open High + one resolved in 24h (the SLA line).
    db.add(models.Escalation(title="Reflow oven tripping", severity="High", owner="Ops",
                             department="Maintenance", status="Open",
                             created_at=now - timedelta(days=2)))
    db.add(models.Escalation(title="Sensor drift", severity="Medium", owner="Ops",
                             department="Quality", status="Resolved",
                             created_at=now - timedelta(days=3),
                             resolved_at=now - timedelta(days=2)))
    db.commit()

    r = report.build_weekly_report(db, "DEFAULT")
    assert r["has_data"] is True
    md = r["markdown"]
    # the report has the expected sections
    for section in ["# Weekly Plant Report", "## Scorecard", "## Cost of losses",
                    "## Delivery", "## Maintenance", "## Week ahead", "## Supply",
                    "## Work in progress", "## Crew",
                    "## Stock integrity", "## Escalations", "## Compliance",
                    "## Needs attention", "## Wins"]:
        assert section in md
    # WIP: the open late WO with its age
    assert "1 open work order, 1 past planned end, oldest open 4 days." in md
    # Crew: pooled quality 90/100 across the two jobs
    assert "1 operator ran 2 jobs (1 completed) at 90% quality." in md
    assert "1 overdue for review" in md
    # maintenance: the overdue backlog and the PM-compliance rate both render
    assert "1 overdue, 0 scheduled over the next 14 days" in md
    assert "PM compliance (30d): 100% completed on plan" in md
    # week ahead: the booked job and its minutes
    assert "1 job (480 min) booked over the next 7 days" in md
    # supply: the overdue PO with the supplier blamed
    assert "1 PO overdue — worst: Acme Metals (1)." in md
    # stock integrity: the dead item and its idle units
    assert "1 dead item (50 units idle)" in md
    # escalations: the open queue and the measured SLA
    assert "1 open (1 high-priority), oldest open 2 days." in md
    assert "2 raised, 1 resolved (50%), avg 24.0h to close." in md
    # and pulls real figures through
    assert "Plant OEE" in md and "Bugatti" in md and "machine down" in md
    assert "$" in md   # cost of losses rendered as money

    # empty plant -> still a valid (mostly empty) report, no crash
    empty = report.build_weekly_report(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and "# Weekly Plant Report" in empty["markdown"]


if __name__ == "__main__":
    test_weekly_report_composes_a_markdown_page()
    print("REPORT OK: weekly Markdown report composes scorecard/cost/delivery/attention/wins; empty-safe")
