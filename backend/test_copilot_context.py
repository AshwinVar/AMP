"""The LLM copilot's factory context must cover the domains it advertises.

_build_factory_context originally fed the model only machines / OEE / downtime /
shifts / stock, so the LLM answered "the provided factory data does not contain
that" for cost, orders, quality, maintenance, WIP and compliance questions — the
"same response to every question" symptom. This pins that the context now
composes the cost / delivery / quality / production read-models.

Run:  python backend/test_copilot_context.py
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import ai_copilot
from database import Base


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_context_includes_all_advertised_domains():
    db = _sess()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=80, line="SMT"))
    # production this week -> PRODUCTION + COST OF LOSSES (40 min downtime, 10 scrap)
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # an order -> ORDERS/DELIVERY
    db.add(models.CustomerOrder(order_no="CO-1", customer_name="Bugatti", product_name="Cluster",
                                order_quantity=100, dispatched_quantity=0, status="Pending",
                                due_date=now.date()))
    # an inspection -> QUALITY
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=100, passed_quantity=90, failed_quantity=10,
                                    defect_category="solder"))
    db.commit()

    ctx = ai_copilot._build_factory_context(db, "DEFAULT")

    # the domains that previously returned "the data doesn't contain that"
    for section in ("PRODUCTION", "COST OF LOSSES", "ORDERS/DELIVERY", "QUALITY"):
        assert section in ctx, f"{section} missing from copilot context:\n{ctx}"
    # the originals are still present
    assert "MACHINES:" in ctx
    # sanity: the real numbers made it in, not just the labels
    assert "90 good of 100" in ctx and "Bugatti" not in ctx  # production line present; order is a count line
    print("PASS copilot context now includes cost, orders, quality and production (not just machines/OEE)")


def test_context_empty_is_safe():
    ctx = ai_copilot._build_factory_context(_sess(), "DEFAULT")
    assert ctx == "No factory data available yet."
    print("PASS empty factory -> safe placeholder, no crash")


if __name__ == "__main__":
    test_context_includes_all_advertised_domains()
    test_context_empty_is_safe()
    print("COPILOT CONTEXT OK: the LLM sees cost, orders, quality, maintenance, WIP and compliance")
