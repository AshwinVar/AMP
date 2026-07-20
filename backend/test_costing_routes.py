"""Costing-routes registration test (ADR-0009).

The costing endpoints live in costing_routes.register(app), peeled out of
main.py. Guards that every expected costing path is registered and owned by
costing_routes.

Run:  python backend/test_costing_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {"/cost-records", "/cost-records/{cost_id}", "/analytics/costing"}


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


if __name__ == "__main__":
    test_costing_paths_owned_by_costing_routes()
    print("ALL COSTING ROUTE TESTS PASSED")
