"""Costing-routes registration test (ADR-0009).

The costing endpoints live in costing_routes.register(app), peeled out of
main.py. Guards that every expected costing path is registered and owned by
costing_routes.

Run:  python backend/test_costing_routes.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
import costing_routes
import models
from database import Base

EXPECTED = {"/cost-records", "/cost-records/{cost_id}", "/analytics/costing"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_costing_paths_owned_by_costing_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"costing paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"costing_routes"}}
    assert not wrong, f"costing paths not owned solely by costing_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} costing paths owned by costing_routes")


def test_cost_per_good_unit_keeps_pence_precision():
    # £500 of logged cost over 1000 good units is £0.50/unit — round() to whole
    # pounds reported £0, fabricating a free product. Keep pence precision.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", description="x", amount=500))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=1000, good_count=1000, rejected_count=0))
    db.commit()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["cost_per_good_unit"] == 0.5      # not round(0.5) == 0
    print("PASS cost_per_good_unit keeps pence precision (£0.50, not £0)")


def test_cost_per_good_unit_is_none_when_no_production():
    # Real costs but zero good units -> per-unit cost is undefined, reported as
    # None ("—" in the UI), never a misleading £0 while costs exist.
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", description="x", amount=500))
    db.commit()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["cost_per_good_unit"] is None and out["manual_cost_total"] == 500
    print("PASS cost_per_good_unit is None (undefined), not £0, when there is no production")


def test_costing_survives_null_amount_and_received_quantity():
    # amount / received_quantity are Column(Integer, default=0) with NO
    # nullable=False — the ORM default only fills a value the inserter omitted, so
    # a raw-SQL / migration / cleared-field row can carry NULL. A bare Python
    # sum() over that None TypeError-500'd the whole /analytics/costing rollup.
    # Independently-derived expectations from THESE numbers:
    #   amounts (NULL->0): 300 + 0(NULL) + 200 = 500
    #   received_quantity (NULL->0): 0(NULL) + 50 = 50
    #   good_count: 400 + 600 = 1000
    #   cost_per_good_unit = 500 / 1000 = 0.5
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", department="Assembly",
                             description="x", amount=300))
    db.add(models.CostRecord(cost_no="C-2", cost_type="Material", department=None,
                             description="y", amount=0))    # will be NULLed below
    db.add(models.CostRecord(cost_no="C-3", cost_type="Labour", department="Assembly",
                             description="z", amount=200))
    due = date(2026, 1, 1)
    db.add(models.PurchaseOrder(po_no="PO-1", item_name="Steel", order_quantity=100,
                                unit="kg", expected_delivery_date=due,
                                received_quantity=0))        # will be NULLed below
    db.add(models.PurchaseOrder(po_no="PO-2", item_name="Steel", order_quantity=100,
                                unit="kg", expected_delivery_date=due,
                                received_quantity=50))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=400, good_count=400, rejected_count=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=600, good_count=600, rejected_count=0))
    db.commit()
    # Simulate the legacy / migration rows the ORM default never touched.
    db.execute(text("UPDATE cost_records SET amount=NULL WHERE cost_no='C-2'"))
    db.execute(text("UPDATE purchase_orders SET received_quantity=NULL WHERE po_no='PO-1'"))
    db.commit()

    out = costing_routes.get_costing_analytics(db=db, current_user={})

    assert out["total_cost_records"] == 3, out
    assert out["manual_cost_total"] == 500, out               # 300 + NULL->0 + 200
    assert out["supplier_receipt_units"] == 50, out           # NULL->0 + 50
    assert out["production_units"] == 1000, out
    assert out["cost_per_good_unit"] == 0.5, out

    # Headline reconciles with BOTH breakdowns (parts sum to the whole).
    assert sum(out["by_type"].values()) == out["manual_cost_total"], out
    assert sum(out["by_department"].values()) == out["manual_cost_total"], out
    # The NULLed row (C-2, Material, no department) still lands in its buckets at 0.
    assert out["by_type"] == {"Labour": 500, "Material": 0}, out
    assert out["by_department"] == {"Assembly": 500, "Unassigned": 0}, out
    print("PASS costing rollup survives NULL amount/received_quantity and reconciles")


def test_costing_empty_tables_all_zero():
    # No cost records, no POs, no production -> every aggregate is 0 and the
    # undefined per-unit cost is None, not a crash and not a fabricated £0.
    db = _fresh_session()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["total_cost_records"] == 0
    assert out["manual_cost_total"] == 0
    assert out["supplier_receipt_units"] == 0
    assert out["production_units"] == 0
    assert out["cost_per_good_unit"] is None
    assert out["by_type"] == {} and out["by_department"] == {}
    print("PASS costing rollup survives empty tables (all zeros, per-unit None)")


if __name__ == "__main__":
    test_costing_paths_owned_by_costing_routes()
    test_cost_per_good_unit_keeps_pence_precision()
    test_cost_per_good_unit_is_none_when_no_production()
    test_costing_survives_null_amount_and_received_quantity()
    test_costing_empty_tables_all_zero()
    print("ALL COSTING ROUTE TESTS PASSED")
