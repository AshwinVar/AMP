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


if __name__ == "__main__":
    test_analytics_paths_owned_by_module()
    test_analytics_summary_is_module_level_and_shared()
    test_module_has_no_relocated_helper_copies()
    test_executive_oee_plant_rollup_is_pooled_not_a_per_machine_mean()
    test_executive_oee_no_production_is_zero_not_fabricated()
    print("ALL ANALYTICS ROUTE TESTS PASSED")
