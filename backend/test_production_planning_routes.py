"""Production-planning route registration test (ADR-0009).

Production plans + schedules (plain CRUD) live in
production_planning_routes.register(app), peeled out of main.py. Guards
registration + sole ownership by the module.

Run:  python backend/test_production_planning_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/production-plans",
    "/production-plans/{plan_id}",
    "/production-schedules",
    "/production-schedules/{schedule_id}",
}


def test_planning_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"planning paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"production_planning_routes"}}
    assert not wrong, f"planning paths not owned solely by production_planning_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} planning paths owned by production_planning_routes")


if __name__ == "__main__":
    test_planning_paths_owned_by_module()
    print("ALL PRODUCTION-PLANNING ROUTE TESTS PASSED")
