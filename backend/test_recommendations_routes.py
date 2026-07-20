"""AI-recommendation route registration test (ADR-0009).

The copilot's suggestion queue (list / update / regenerate) lives in
recommendations_routes.register(app), peeled out of main.py. Guards registration
+ sole ownership by the module.

Run:  python backend/test_recommendations_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/ai/recommendations",
    "/ai/recommendations/{recommendation_id}",
    "/ai/generate-recommendations",
}


def test_recommendations_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"recommendation paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"recommendations_routes"}}
    assert not wrong, f"recommendation paths not owned solely by recommendations_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} recommendation paths owned by recommendations_routes")


if __name__ == "__main__":
    test_recommendations_paths_owned_by_module()
    print("ALL RECOMMENDATION ROUTE TESTS PASSED")
