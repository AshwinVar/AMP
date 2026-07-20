"""User-management route registration test (ADR-0009).

Admin CRUD over workspace users (add / list / change role / delete / password
reset) lives in users_routes.register(app), peeled out of main.py. Guards
registration + sole ownership, and that role validation still uses VALID_ROLES.

Run:  python backend/test_users_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/users",
    "/users/{user_id}/role",
    "/users/{user_id}",
    "/users/{user_id}/password",
}


def test_users_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"users paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"users_routes"}}
    assert not wrong, f"users paths not owned solely by users_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} users paths owned by users_routes")


def test_valid_roles_moved_with_module():
    import users_routes
    assert users_routes.VALID_ROLES == ["Admin", "Supervisor", "Operator"], \
        "VALID_ROLES must live with the module that validates it"
    # And it must no longer be a name on main (dead constant removed).
    assert not hasattr(main, "VALID_ROLES"), "stale VALID_ROLES left on main"
    print("PASS VALID_ROLES moved to users_routes and removed from main")


if __name__ == "__main__":
    test_users_paths_owned_by_module()
    test_valid_roles_moved_with_module()
    print("ALL USERS ROUTE TESTS PASSED")
