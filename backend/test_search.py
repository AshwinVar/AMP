"""Global entity search tests.

One query across the core entities, each hit carrying the view that opens it.
Run:  python backend/test_search.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import search


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Running", utilization=80, line="SMT"))
    db.add(models.WorkOrder(work_order_no="WO-CLB-01", part_number="CLB-PCB", batch_number="B1",
                            machine_id=1, target_quantity=100, actual_quantity=10, status="In Progress"))
    db.add(models.CustomerOrder(order_no="CO-5001", customer_name="Bugatti", product_name="CLB-PCB",
                                order_quantity=100, dispatched_quantity=0, status="Pending",
                                due_date=datetime.utcnow().date()))
    db.add(models.InventoryItem(item_code="CLB-PCB", item_name="Cluster PCB", category="PCB",
                                current_stock=40, reorder_level=50, unit="pcs", supplier="Acme"))
    db.add(models.MaintenanceTask(task_no="AUTO-MAINT-1-99", machine_id=1, task_type="Predictive (auto)",
                                  priority="High", assigned_to="Maintenance team",
                                  planned_date=datetime.utcnow().date(), status="Open"))
    db.add(models.Escalation(tenant_code="DEFAULT", machine_id=1, title="Reflow oven overheating",
                             severity="High", owner="Lead", department="Maintenance",
                             status="Open", source="Manual"))
    db.add(models.ComplianceDocument(document_no="SOP-7", title="Reflow profile SOP", document_type="SOP",
                                     department="Quality", version="1.0", owner="QA",
                                     approval_status="Approved", review_due_date=datetime.utcnow().date()))
    db.commit()


def test_search_finds_entities_across_types():
    db = _fresh_session()
    _seed(db)

    # "reflow" hits the machine, the escalation and the document — typed, with views
    r = search.build_search(db, "DEFAULT", "reflow")
    types = {h["type"] for h in r["results"]}
    assert {"machine", "escalation", "document"} <= types
    m = next(h for h in r["results"] if h["type"] == "machine")
    assert m["label"] == "SMT-Reflow-01" and m["view"] == "machines"

    # an order number jumps straight to orders; case-insensitive
    r = search.build_search(db, "DEFAULT", "co-5001")
    assert any(h["type"] == "customer order" and h["view"] == "orders" for h in r["results"])

    # a part number spans work orders + customer orders + inventory
    r = search.build_search(db, "DEFAULT", "CLB-PCB")
    assert {"work order", "customer order", "inventory"} <= {h["type"] for h in r["results"]}

    # a maintenance task number
    r = search.build_search(db, "DEFAULT", "AUTO-MAINT")
    assert any(h["type"] == "maintenance" and h["view"] == "cmms" for h in r["results"])

    # too-short and no-hit queries are empty, not errors
    assert search.build_search(db, "DEFAULT", "x")["results"] == []
    assert search.build_search(db, "DEFAULT", "zzzznope")["results"] == []


def test_per_type_results_are_bounded_and_newest_first():
    """A broad match must not hydrate the whole (growing) table: each type is
    capped in SQL at LIMIT_PER_TYPE, and the capped page is the NEWEST matches
    (deterministic id-desc order), not an arbitrary DB-order subset."""
    db = _fresh_session()
    # 12 customer orders all matching "WIDGET" — more than LIMIT_PER_TYPE (4).
    for n in range(1, 13):
        db.add(models.CustomerOrder(order_no=f"CO-{n:04d}", customer_name="Acme",
                                    product_name="WIDGET-X", order_quantity=10,
                                    dispatched_quantity=0, status="Pending",
                                    due_date=datetime.utcnow().date()))
    db.commit()

    r = search.build_search(db, "DEFAULT", "WIDGET")
    orders = [h for h in r["results"] if h["type"] == "customer order"]
    # Bounded per type, and (with 10 total across types) not more than LIMIT_TOTAL.
    assert len(orders) == search.LIMIT_PER_TYPE
    assert len(r["results"]) <= search.LIMIT_TOTAL
    # Newest first: the four highest ids (the last four inserted), in id-desc order.
    ids = [h["id"] for h in orders]
    assert ids == sorted(ids, reverse=True)
    all_ids = [o.id for o in db.query(models.CustomerOrder).all()]
    assert set(ids) == set(sorted(all_ids)[-search.LIMIT_PER_TYPE:])


if __name__ == "__main__":
    test_search_finds_entities_across_types()
    test_per_type_results_are_bounded_and_newest_first()
    print("SEARCH OK: one query across machines/work orders/customer orders/inventory/maintenance/"
          "escalations/documents, typed hits with views; case-insensitive; empty-safe; "
          "each type bounded in SQL and newest-first")
