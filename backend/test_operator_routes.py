"""Operator route registration test (ADR-0009).

Operator job executions (list / create / update / delete) live in
operator_routes.register(app), peeled out of main.py. Guards registration +
sole ownership by the module.

Run:  python backend/test_operator_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {"/operator/executions", "/operator/executions/{execution_id}"}


def test_operator_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"operator paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"operator_routes"}}
    assert not wrong, f"operator paths not owned solely by operator_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} operator paths owned by operator_routes")


if __name__ == "__main__":
    test_operator_paths_owned_by_module()
    print("ALL OPERATOR ROUTE TESTS PASSED")
