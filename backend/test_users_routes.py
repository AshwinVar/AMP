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


def test_every_users_route_is_admin_gated_at_router_level():
    # The Admin gate is hoisted onto the APIRouter (dependencies=[...]), so every
    # /users route — current and future — must carry the require_roles checker.
    users = [r for r in main.app.routes if getattr(r, "path", "").startswith("/users")]
    assert users, "no /users routes registered"
    for r in users:
        names = [getattr(d.call, "__qualname__", "") for d in r.dependant.dependencies]
        assert any("role_checker" in n for n in names), \
            f"{r.path} {r.methods} lost the router-level Admin gate"
    print(f"PASS all {len(users)} /users routes carry the router-level Admin gate")


def test_admin_gate_rejects_non_admin():
    import auth
    from fastapi import HTTPException
    checker = auth.require_roles(["Admin"])
    try:
        checker(current_user={"sub": "o", "role": "Operator"})
        assert False, "non-Admin should be rejected by the users router gate"
    except HTTPException as e:
        assert e.status_code == 403
    assert checker(current_user={"sub": "a", "role": "Admin"})["role"] == "Admin"
    print("PASS the Admin gate rejects non-Admin (403) and passes Admin")


if __name__ == "__main__":
    test_users_paths_owned_by_module()
    test_valid_roles_moved_with_module()
    test_every_users_route_is_admin_gated_at_router_level()
    test_admin_gate_rejects_non_admin()
    print("ALL USERS ROUTE TESTS PASSED")
