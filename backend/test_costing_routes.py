"""Costing-routes registration test (ADR-0009).

The costing endpoints live in costing_routes.register(app), peeled out of
main.py. Guards that every expected costing path is registered and owned by
costing_routes.

Run:  python backend/test_costing_routes.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
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


if __name__ == "__main__":
    test_costing_paths_owned_by_costing_routes()
    test_cost_per_good_unit_keeps_pence_precision()
    test_cost_per_good_unit_is_none_when_no_production()
    print("ALL COSTING ROUTE TESTS PASSED")
