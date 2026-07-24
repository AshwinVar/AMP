"""User-management route registration test (ADR-0009).

Admin CRUD over workspace users (add / list / change role / delete / password
reset) lives in users_routes.register(app), peeled out of main.py. Guards
registration + sole ownership, and that role validation still uses VALID_ROLES.

Run:  python backend/test_users_routes.py     (exit 0 = pass)
"""
import main

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import schemas
import users_routes
from database import Base

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


# --- Behavioural tests that actually EXECUTE the handlers. The registration
# tests above never called a handler, which is exactly how a NameError
# (_same_tenant_or_403 referenced but never defined) shipped: update-role /
# delete / password-reset raised it -> HTTP 500 on every call. ---

def _two_tenant_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.User(id=1, username="acme_op", password="x", role="Operator", tenant_code="ACME"))
    db.add(models.User(id=2, username="globex_op", password="x", role="Operator", tenant_code="GLOBEX"))
    db.commit()
    return db


# request_tenant reads the "tenant" JWT claim (falling back to DEFAULT).
ACME_ADMIN = {"sub": "acme_admin", "role": "Admin", "tenant": "ACME"}


def _user(db, uid):
    return db.query(models.User).filter(models.User.id == uid).first()


def test_role_change_works_in_tenant_and_403s_across_tenants():
    db = _two_tenant_db()
    # same tenant: succeeds (regression — this used to raise NameError -> 500)
    out = users_routes.update_user_role(1, schemas.UserRoleUpdate(role="Supervisor"),
                                        db=db, current_user=ACME_ADMIN)
    assert out.role == "Supervisor"
    # another tenant's user: 403, not a NameError and not a silent cross-tenant edit
    try:
        users_routes.update_user_role(2, schemas.UserRoleUpdate(role="Admin"),
                                      db=db, current_user=ACME_ADMIN)
        assert False, "cross-tenant role change must be forbidden"
    except HTTPException as e:
        assert e.status_code == 403
    assert _user(db, 2).role == "Operator"        # GLOBEX user untouched
    print("PASS role change: in-tenant works, cross-tenant 403 (no NameError)")


def test_delete_and_password_reset_enforce_the_same_tenant_boundary():
    db = _two_tenant_db()
    # cross-tenant delete -> 403 (the guard runs before the delete), user survives
    try:
        users_routes.delete_user(2, db=db, current_user=ACME_ADMIN)
        assert False, "cross-tenant delete must be forbidden"
    except HTTPException as e:
        assert e.status_code == 403
    assert _user(db, 2) is not None
    # cross-tenant password reset -> 403
    try:
        users_routes.reset_user_password(2, {"password": "newpass123"}, db=db, current_user=ACME_ADMIN)
        assert False, "cross-tenant password reset must be forbidden"
    except HTTPException as e:
        assert e.status_code == 403
    # in-tenant password reset succeeds (no NameError)
    r = users_routes.reset_user_password(1, {"password": "newpass123"}, db=db, current_user=ACME_ADMIN)
    assert "successfully" in r["message"].lower()
    print("PASS delete + password reset enforce the same-tenant boundary (no NameError)")


def test_missing_user_is_404_not_500():
    db = _two_tenant_db()
    try:
        users_routes.update_user_role(999, schemas.UserRoleUpdate(role="Admin"),
                                      db=db, current_user=ACME_ADMIN)
        assert False, "unknown user should 404"
    except HTTPException as e:
        assert e.status_code == 404
    print("PASS unknown user id -> 404 (guard runs after the existence check)")


if __name__ == "__main__":
    test_users_paths_owned_by_module()
    test_valid_roles_moved_with_module()
    test_every_users_route_is_admin_gated_at_router_level()
    test_admin_gate_rejects_non_admin()
    test_role_change_works_in_tenant_and_403s_across_tenants()
    test_delete_and_password_reset_enforce_the_same_tenant_boundary()
    test_missing_user_is_404_not_500()
    print("ALL USERS ROUTE TESTS PASSED")
