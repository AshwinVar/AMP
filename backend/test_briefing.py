"""Morning-briefing read-model tests (ADR-0007).

The briefing composes the pillar read-models (OEE, losses, downtime, quality,
flow, inventory) plus a machine-status glance into one prioritized digest:
headline OEE + trend, ranked alerts (most urgent first), and a few wins.

Run:  python backend/test_briefing.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import briefing


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_briefing_ranks_alerts_and_surfaces_wins():
    db = _fresh_session()
    now = datetime.utcnow()
    # A down machine (high-urgency) and a healthy one.
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=95, line="IC"))
    # Production so OEE has data (low performance -> a real OEE loss).
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # Out-of-stock part (high-urgency supply alert).
    db.add(models.InventoryItem(item_code="CLB-PCB", item_name="Cluster PCB", category="PCB",
                                current_stock=0, reorder_level=50, unit="pcs", supplier="Acme"))
    # A downtime event and a failing inspection.
    db.add(models.DowntimeLog(machine_id=1, reason="Breakdown", duration="40 min", created_at=now))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=100, passed_quantity=90, failed_quantity=10,
                                    defect_category="solder"))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    assert b["has_data"] is True
    assert b["oee"] > 0 and b["oee_trend"] in {"up", "down", "flat"}
    keys = [a["key"] for a in b["alerts"]]
    # every pillar signal is represented
    assert {"machines_down", "out_of_stock", "oee_loss", "quality", "downtime"} <= set(keys)
    # ranked: the two high-severity alerts lead the feed
    assert {b["alerts"][0]["key"], b["alerts"][1]["key"]} == {"machines_down", "out_of_stock"}
    assert all(a["severity"] in {"high", "medium", "low"} for a in b["alerts"])
    # the down-machine alert names the machine
    md = next(a for a in b["alerts"] if a["key"] == "machines_down")
    assert "SMT-Reflow-01" in md["detail"]
    # headline mentions OEE and the attention count
    assert "OEE" in b["headline"] and "attention" in b["headline"]


def test_briefing_empty_is_safe():
    b = briefing.build_briefing(_fresh_session(), "DEFAULT")
    assert b["has_data"] is False and b["alerts"] == [] and b["wins"] == []
    assert b["oee"] == 0 and b["oee_trend"] == "flat"


def test_clean_plant_has_no_alerts_but_reports_wins():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="IC-Test-01", status="Running", utilization=98, line="IC"))
    # High-OEE run over several days so the plant reads world-class and trend has data.
    for i in range(6):
        db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=478,
                                       ideal_cycle_time_seconds=60, total_count=478, good_count=478,
                                       rejected_count=0, created_at=now - timedelta(days=i)))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=100, passed_quantity=100, failed_quantity=0))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    assert b["has_data"] is True
    # nothing broken, nothing short on stock, quality clean -> no alerts
    assert b["alerts"] == []
    # but the wins surface the world-class OEE and clean yield
    win_titles = " ".join(w["title"] for w in b["wins"])
    assert "world-class" in win_titles and "First-pass yield" in win_titles


def test_briefing_marks_alerts_the_agent_has_escalated():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # an open escalation the agent already raised, tagged with the briefing marker
    esc = models.Escalation(tenant_code="DEFAULT", machine_id=1, title="Briefing: 1 machine down",
                            severity="High", owner="Plant Manager", department="Maintenance",
                            status="Proposed", source="Escalation agent",
                            notes="[briefing:machines_down] raised from the briefing")
    db.add(esc)
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    by = {a["key"]: a for a in b["alerts"]}
    # the escalated signal is marked and links to the escalation id
    assert by["machines_down"]["escalated"] is True
    assert by["machines_down"]["escalation_id"] == esc.id
    # a different alert with no escalation is not marked and has no id
    assert "oee_loss" in by and by["oee_loss"]["escalated"] is False
    assert by["oee_loss"]["escalation_id"] is None


def test_briefing_flags_late_orders():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=95,
                                   rejected_count=5, created_at=now))
    # a customer order past its due date and not shipped -> a high-severity delivery alert
    db.add(models.CustomerOrder(order_no="BUG-9", customer_name="Bugatti", product_name="CLB-PCB",
                                order_quantity=100, dispatched_quantity=10, status="Pending",
                                due_date=(now.date() - timedelta(days=3))))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    by = {a["key"]: a for a in b["alerts"]}
    assert "delivery" in by
    assert by["delivery"]["severity"] == "high" and by["delivery"]["module"] == "orders"
    assert "BUG-9" in by["delivery"]["detail"] and "overdue" in by["delivery"]["detail"]


def test_briefing_flags_overdue_maintenance():
    from datetime import date
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=90, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=460,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=98,
                                   rejected_count=2, created_at=now))
    # an overdue maintenance task -> a high-severity maintenance alert
    db.add(models.MaintenanceTask(task_no="M-1", machine_id=1, task_type="Predictive (auto)",
                                  priority="Critical", assigned_to="Maintenance team",
                                  planned_date=(date.today() - timedelta(days=2)), status="Open"))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    by = {a["key"]: a for a in b["alerts"]}
    assert "maintenance" in by
    assert by["maintenance"]["severity"] == "high" and by["maintenance"]["module"] == "cmms"
    assert "overdue" in by["maintenance"]["title"] and "SMT-Reflow-01" in by["maintenance"]["detail"]


def test_briefing_flags_overdue_document_reviews():
    from datetime import date
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=470,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=98,
                                   rejected_count=2, created_at=now))
    # a controlled document overdue for review -> a medium compliance alert
    db.add(models.ComplianceDocument(document_no="SOP-1", title="Solder Reflow SOP", document_type="SOP",
                                     department="Quality", version="1.0", owner="QA Lead",
                                     approval_status="Approved", review_due_date=(date.today() - timedelta(days=5))))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    by = {a["key"]: a for a in b["alerts"]}
    assert "compliance" in by
    assert by["compliance"]["severity"] == "medium" and by["compliance"]["module"] == "documents"
    assert "overdue" in by["compliance"]["title"] and "Solder Reflow SOP" in by["compliance"]["detail"]


if __name__ == "__main__":
    test_briefing_ranks_alerts_and_surfaces_wins()
    test_briefing_empty_is_safe()
    test_clean_plant_has_no_alerts_but_reports_wins()
    test_briefing_marks_alerts_the_agent_has_escalated()
    test_briefing_flags_late_orders()
    test_briefing_flags_overdue_maintenance()
    test_briefing_flags_overdue_document_reviews()
    print("BRIEFING OK: composes pillar read-models into a ranked alert feed (high-first) + wins; "
          "headline OEE + trend; empty-safe; clean plant -> no alerts, only wins; "
          "marks alerts the Escalation agent has proactively raised")
