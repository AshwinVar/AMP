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

import main

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
    assert callable(getattr(analytics_routes, "analytics_summary", None)), \
        "analytics_summary must be a module-level function (main's daily-summary.txt imports it)"
    # main re-exports it and /reports/daily-summary.txt still calls it.
    assert getattr(main, "analytics_summary", None) is analytics_routes.analytics_summary, \
        "main must import the same analytics_summary it delegates daily-summary.txt to"
    dsr = inspect.getsource(main.daily_summary_report)
    assert "analytics_summary(" in dsr, "daily-summary.txt should still call analytics_summary"
    print("PASS analytics_summary is module-level, shared with main's daily-summary.txt")


def test_module_has_no_relocated_helper_copies():
    import analytics_routes
    src = inspect.getsource(analytics_routes)
    for helper in ("def generate_alerts", "def calculate_fallback_oee",
                   "def parse_duration_to_minutes", "def calculate_oee_from_record"):
        assert helper not in src, f"{helper} must be imported from analytics_engine, not redefined"
    print("PASS analytics_routes imports its compute from analytics_engine (no local copies)")


if __name__ == "__main__":
    test_analytics_paths_owned_by_module()
    test_analytics_summary_is_module_level_and_shared()
    test_module_has_no_relocated_helper_copies()
    print("ALL ANALYTICS ROUTE TESTS PASSED")
