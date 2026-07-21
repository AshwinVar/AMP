"""Reporting route registration test (ADR-0009).

CSV/text exports + the report-request log live in reports_routes.register(app),
peeled out of main.py. Guards registration + sole ownership. All compute is
imported from the shared engines (analytics_engine, report_generator), so the
module must not reference the main-local intelligence helpers.

/reports/daily-summary.txt deliberately stays in main (it calls the
/analytics/summary endpoint function directly), so it is asserted to remain
owned by main.

Run:  python backend/test_reports_routes.py     (exit 0 = pass)
"""
import inspect

import main

EXPECTED = {
    "/reports/downtime.csv",
    "/reports/shifts.csv",
    "/reports/oee.csv",
    "/reports/intelligence-summary.txt",
    "/reports",
}


def test_reports_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"reports paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"reports_routes"}}
    assert not wrong, f"reports paths not owned solely by reports_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} reports paths owned by reports_routes")


def test_daily_summary_owned_by_core():
    # daily-summary.txt calls analytics_summary (not the reports CSV/record CRUD);
    # it was left in main and is now grouped into core_routes, which imports
    # analytics_summary from analytics_routes.
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/reports/daily-summary.txt") == "core_routes", \
        "daily-summary.txt should be owned by core_routes"
    print("PASS /reports/daily-summary.txt is owned by core_routes")


def test_module_has_no_main_local_coupling():
    import reports_routes
    src = inspect.getsource(reports_routes)
    for helper in ("analytics_summary(", "generate_alerts("):
        assert helper not in src, f"reports_routes must not call main-local {helper}"
    print("PASS reports_routes has no main-local helper coupling")


if __name__ == "__main__":
    test_reports_paths_owned_by_module()
    test_daily_summary_owned_by_core()
    test_module_has_no_main_local_coupling()
    print("ALL REPORTS ROUTE TESTS PASSED")
