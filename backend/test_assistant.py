"""Rule-first copilot tests (ADR-0003).

Routes plant questions to the right read-model and phrases the answer, no API key
required. Run:  python backend/test_assistant.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import assistant


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # a prior-week run (8 days ago) so the scorecard has a period to compare against
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=480, runtime_minutes=400,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=80,
                                   rejected_count=20, created_at=now - timedelta(days=8)))
    db.add(models.InventoryItem(item_code="CLB-PCB", item_name="Cluster PCB", category="PCB",
                                current_stock=0, reorder_level=50, unit="pcs", supplier="Acme"))
    db.add(models.CustomerOrder(order_no="BUG-1", customer_name="Bugatti", product_name="P",
                                order_quantity=100, dispatched_quantity=0, status="Pending",
                                due_date=(now.date() - timedelta(days=1))))
    db.commit()


def test_copilot_routes_questions_to_the_right_pillar():
    db = _fresh_session()
    _seed(db)

    def ask(q):
        return assistant.answer(db, "DEFAULT", q)

    assert ask("Why is my OEE low?")["matched"] == "oee"
    assert ask("Which machines are in breakdown?")["matched"] == "machines"
    assert ask("What should I reorder first?")["matched"] == "inventory"
    assert ask("Are any orders late?")["matched"] == "delivery"
    assert ask("How much are losses costing us?")["matched"] == "cost"
    assert ask("What's our quality fail rate?")["matched"] == "quality"
    assert ask("Any overdue maintenance?")["matched"] == "maintenance"
    assert ask("Why is downtime high?")["matched"] == "downtime"
    assert ask("Summarise today's production")["matched"] == "production"
    # temporal questions -> the trend intent (week-on-week from the scorecard)
    assert ask("How are we doing vs last week?")["matched"] == "trend"
    assert ask("Is OEE improving?")["matched"] == "trend"
    tr = ask("Are we better or worse than last week?")
    assert tr["view"] == "executive" and tr["answer"]
    # unknown -> defaults to the attention briefing
    assert ask("hello")["matched"] == "briefing"

    # answers carry a sentence and a drill-in view
    a = ask("Which machines are in breakdown?")
    assert "SMT-Reflow-01" in a["answer"] and a["view"] == "machines"
    r = ask("What should I reorder first?")
    assert "Cluster PCB" in r["answer"] and r["view"] == "inventory"

    # empty-safe on a fresh plant
    blank = assistant.answer(_fresh_session(), "DEFAULT", "how's it going?")
    assert "answer" in blank and blank["answer"]


if __name__ == "__main__":
    test_copilot_routes_questions_to_the_right_pillar()
    print("ASSISTANT OK: rule-first copilot routes questions to the right pillar read-model and "
          "phrases an answer + drill-in view; no API key; empty-safe")
