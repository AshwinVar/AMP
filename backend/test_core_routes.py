"""Core route registration test (ADR-0009).

The irreducible endpoints that never fit a domain module — auth/bootstrap, the
AI-platform self-report, the BOM view, and the intelligence stragglers — now
live behind core_routes.register... (an APIRouter tagged "Core"), the last group
peeled off `app` in main.py. Guards registration + sole ownership, and the
invariant that main.py no longer defines ANY HTTP route itself (only the
lifecycle websocket remains).

Run:  python backend/test_core_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/", "/me", "/register", "/login", "/auth/refresh", "/auth/change-password",
    "/platform/status", "/bom", "/briefing/escalate",
    "/escalations/from-smart-alerts", "/reports/daily-summary.txt", "/ops-trends",
}


def test_core_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"core paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"core_routes"}}
    assert not wrong, f"core paths not owned solely by core_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} core paths owned by core_routes")


def test_main_defines_no_http_routes():
    # The whole point: after this peel-off, main.py assembles the app and owns
    # only lifecycle bits (sim loop, startup, the /ws/live websocket). No HTTP
    # route may be defined directly on `app` in main anymore.
    http_in_main = sorted({
        r.path for r in main.app.routes
        if hasattr(r, "methods") and getattr(r, "endpoint", None)
        and r.endpoint.__module__ == "main"
    })
    assert not http_in_main, f"main.py still defines HTTP routes: {http_in_main}"
    print("PASS main.py defines no HTTP routes (only the lifecycle websocket remains)")


if __name__ == "__main__":
    test_core_paths_owned_by_module()
    test_main_defines_no_http_routes()
    print("ALL CORE ROUTE TESTS PASSED")
