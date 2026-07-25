"""Analytics/alerts route registration test (ADR-0009).

The dashboard read-model / intelligence surface (OEE + alerts summary, per-page
command centres, executive rollups, predictive maintenance, machine health)
lives in analytics_routes.register(app), peeled out of main.py. Guards
registration + sole ownership of the 27 moved paths.

analytics_summary is module-level (main's /reports/daily-summary.txt imports it
directly), so it's asserted importable both from analytics_routes and re-exported
on main. Compute is imported from the shared engines — the module must not carry
its own copy of the relocated helpers.

Run:  python backend/test_analytics_routes.py     (exit 0 = pass)
"""
import inspect
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import core_routes
import analytics_routes
import models
from analytics_engine import pooled_oee
from database import Base

EXPECTED = {
    "/oee/summary",
    "/alerts", "/alerts/smart",
    "/machine-health/{machine_id}",
    "/analytics/summary",
    "/analytics/machine-timeline", "/analytics/machine-state-summary",
    "/analytics/oee-trends", "/analytics/shift-kpis", "/analytics/management",
    "/analytics/predictive-maintenance",
    "/analytics/work-orders", "/analytics/production-plans",
    "/analytics/escalations", "/analytics/inventory", "/analytics/quality",
    "/analytics/executive-oee", "/analytics/factory-command-center",
    "/analytics/documents", "/analytics/maintenance",
    "/analytics/production-schedules", "/analytics/iot-command",
    "/analytics/ai-insights", "/analytics/operator-terminal",
    "/analytics/system-health", "/analytics/final-executive-summary",
    "/analytics/industrial-gateway",
}


def test_analytics_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"analytics paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"analytics_routes"}}
    assert not wrong, f"analytics paths not owned solely by analytics_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} analytics paths owned by analytics_routes")


def test_analytics_summary_is_module_level_and_shared():
    import analytics_routes
    import core_routes
    assert callable(getattr(analytics_routes, "analytics_summary", None)), \
        "analytics_summary must be a module-level function (core_routes' daily-summary.txt imports it)"
    # core_routes owns /reports/daily-summary.txt and imports the same function.
    assert getattr(core_routes, "analytics_summary", None) is analytics_routes.analytics_summary, \
        "core_routes must import the same analytics_summary it delegates daily-summary.txt to"
    dsr = inspect.getsource(core_routes.daily_summary_report)
    assert "analytics_summary(" in dsr, "daily-summary.txt should still call analytics_summary"
    print("PASS analytics_summary is module-level, shared with main's daily-summary.txt")


def test_module_has_no_relocated_helper_copies():
    import analytics_routes
    src = inspect.getsource(analytics_routes)
    for helper in ("def generate_alerts", "def calculate_fallback_oee",
                   "def parse_duration_to_minutes", "def calculate_oee_from_record"):
        assert helper not in src, f"{helper} must be imported from analytics_engine, not redefined"
    print("PASS analytics_routes imports its compute from analytics_engine (no local copies)")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_executive_oee_plant_rollup_is_pooled_not_a_per_machine_mean():
    # A high-volume machine running poorly + a tiny perfect run. Averaging the two
    # machines' OEE (mean of ratios) over-weights the tiny perfect run; pooling
    # weights by volume — and it must match /oee-summary's pooled definition.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Running", utilization=80))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    db.commit()

    out = analytics_routes.get_executive_oee(db=db, current_user={})
    expected = pooled_oee(db.query(models.ProductionRecord).all())
    assert out["plant_oee"] == expected["oee"]
    assert out["plant_availability"] == expected["availability"]
    assert out["plant_performance"] == expected["performance"]
    assert out["plant_quality"] == expected["quality"]

    # and it is emphatically NOT the mean of the per-machine ranking (Big 20 +
    # Tiny 100) / 2 = 60 — the number the endpoint used to report.
    ranking = out["machine_ranking"]
    mean_oee = round(sum(r["oee"] for r in ranking) / len(ranking))
    assert out["plant_oee"] != mean_oee and out["plant_oee"] == expected["oee"] < mean_oee, (out["plant_oee"], mean_oee)
    print(f"PASS executive-oee plant rollup is pooled ({out['plant_oee']}%), not the per-machine mean ({mean_oee}%)")


def test_executive_oee_no_production_is_zero_not_fabricated():
    # No production records -> pooled has no data -> plant OEE is 0, not a number
    # invented from per-machine fallback constants.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db.commit()
    out = analytics_routes.get_executive_oee(db=db, current_user={})
    assert out["plant_oee"] == 0 and out["plant_availability"] == 0
    print("PASS executive-oee reports 0 plant OEE on no production (no fabricated number)")


def _work_order(**kw):
    kw.setdefault("part_number", "PN")
    kw.setdefault("batch_number", "BN")
    return models.WorkOrder(**kw)


def test_work_order_analytics_null_actual_and_reconciled_totals():
    # actual_quantity is Column(Integer, default=0) without nullable=False, so a
    # NULL slips in via raw SQL / a cleared update. The old sum(...) did int + None
    # and 500'd; COALESCE(SUM,0) must treat the NULL as 0. Numbers are derived by
    # hand, not read back from the endpoint.
    db = _fresh_session()
    db.add(_work_order(id=1, work_order_no="W1", status="Running", target_quantity=100, actual_quantity=50))
    db.add(_work_order(id=2, work_order_no="W2", status="Planned", target_quantity=200, actual_quantity=None))
    db.add(_work_order(id=3, work_order_no="W3", status="Completed", target_quantity=300, actual_quantity=300))
    db.add(_work_order(id=4, work_order_no="W4", status="Delayed", target_quantity=50, actual_quantity=10))
    db.commit()

    out = analytics_routes.get_work_order_analytics(db=db, current_user={})
    assert out["total_work_orders"] == 4, out
    assert out["planned"] == 1 and out["running"] == 1
    assert out["completed"] == 1 and out["delayed"] == 1
    # target: 100+200+300+50 = 650 (target_quantity is NOT NULL, all counted)
    assert out["total_target"] == 650, out
    # actual: 50 + 0(NULL->0) + 300 + 10 = 360 — NOT a crash, NOT dropping the row
    assert out["total_actual"] == 360, out
    # achievement = round(360/650*100) = round(55.38) = 55
    assert out["achievement"] == 55, out
    # the four status buckets reconcile with the count of the four named statuses
    assert out["planned"] + out["running"] + out["completed"] + out["delayed"] == 4
    print("PASS work-order analytics: NULL actual -> 0, totals reconcile (650 target / 360 actual / 55%)")


def test_work_order_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_work_order_analytics(db=db, current_user={})
    assert out["total_work_orders"] == 0
    assert out["total_target"] == 0 and out["total_actual"] == 0
    # zero denominator -> 0, not a ZeroDivisionError
    assert out["achievement"] == 0
    print("PASS work-order analytics: empty table -> zeros, no divide-by-zero")


def test_production_plan_analytics_null_actual_and_reconciled_totals():
    db = _fresh_session()
    db.add(models.ProductionPlan(id=1, plan_no="P1", status="Running", planned_quantity=100,
                                 actual_quantity=80, plan_date=date(2026, 1, 1), shift_name="A"))
    db.add(models.ProductionPlan(id=2, plan_no="P2", status="Behind", planned_quantity=200,
                                 actual_quantity=None, plan_date=date(2026, 1, 1), shift_name="B"))
    db.add(models.ProductionPlan(id=3, plan_no="P3", status="Completed", planned_quantity=100,
                                 actual_quantity=100, plan_date=date(2026, 1, 1), shift_name="C"))
    db.commit()

    out = analytics_routes.get_production_plan_analytics(db=db, current_user={})
    assert out["total_plans"] == 3, out
    # planned: 100+200+100 = 400 (planned_quantity is NOT NULL)
    assert out["planned_quantity"] == 400, out
    # actual: 80 + 0(NULL->0) + 100 = 180
    assert out["actual_quantity"] == 180, out
    # achievement = round(180/400*100) = 45
    assert out["achievement"] == 45, out
    assert out["running"] == 1 and out["completed"] == 1 and out["behind"] == 1
    assert out["planned"] == 0
    print("PASS production-plan analytics: NULL actual -> 0, totals reconcile (400 planned / 180 actual / 45%)")


def test_production_plan_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_production_plan_analytics(db=db, current_user={})
    assert out["total_plans"] == 0
    assert out["planned_quantity"] == 0 and out["actual_quantity"] == 0
    assert out["achievement"] == 0
    print("PASS production-plan analytics: empty table -> zeros, no divide-by-zero")


if __name__ == "__main__":
    test_analytics_paths_owned_by_module()
    test_analytics_summary_is_module_level_and_shared()
    test_module_has_no_relocated_helper_copies()
    test_executive_oee_plant_rollup_is_pooled_not_a_per_machine_mean()
    test_executive_oee_no_production_is_zero_not_fabricated()
    test_work_order_analytics_null_actual_and_reconciled_totals()
    test_work_order_analytics_empty_table_is_zero_not_a_crash()
    test_production_plan_analytics_null_actual_and_reconciled_totals()
    test_production_plan_analytics_empty_table_is_zero_not_a_crash()
    print("ALL ANALYTICS ROUTE TESTS PASSED")
