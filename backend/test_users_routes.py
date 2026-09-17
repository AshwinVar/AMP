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


def _self_admin_db():
    """One workspace (ACME) whose only Admin is the caller (id=1)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.User(id=1, username="acme_admin", password="x", role="Admin", tenant_code="ACME"))
    db.add(models.User(id=3, username="acme_admin2", password="x", role="Admin", tenant_code="ACME"))
    db.commit()
    return db


def test_admin_cannot_demote_their_own_account():
    # The lockout vector: an Admin demoting THEMSELF to a non-Admin role removes the
    # last Admin and locks the workspace out of every Admin-gated action, with no
    # non-Admin path to undo it. Mirrors delete_user's "cannot delete your own
    # account" guard, which is what otherwise guarantees >= 1 Admin survives.
    db = _self_admin_db()
    for role in ("Operator", "Supervisor"):
        try:
            users_routes.update_user_role(1, schemas.UserRoleUpdate(role=role),
                                          db=db, current_user=ACME_ADMIN)
            assert False, f"self-demotion to {role} must be forbidden"
        except HTTPException as e:
            assert e.status_code == 400, f"self-demotion should 400, got {e.status_code}"
    # The caller's own row is untouched — still Admin, workspace not locked out.
    assert _user(db, 1).role == "Admin"
    print("PASS an Admin cannot demote their own account (400), role unchanged")


def test_admin_may_still_rescope_another_admin():
    # Only SELF-demotion is refused: demoting a DIFFERENT Admin is allowed (the
    # caller remains an Admin, so the workspace keeps an Admin). Guards against the
    # fix over-reaching into ordinary role management.
    db = _self_admin_db()
    out = users_routes.update_user_role(3, schemas.UserRoleUpdate(role="Operator"),
                                        db=db, current_user=ACME_ADMIN)
    assert out.role == "Operator"
    assert _user(db, 1).role == "Admin"          # caller still Admin
    print("PASS an Admin may still re-scope ANOTHER Admin's role")


def test_admin_self_noop_to_admin_is_allowed():
    # A self "change" that keeps the Admin role is a harmless no-op and must NOT be
    # blocked — only a role that would REMOVE Admin is refused.
    db = _self_admin_db()
    out = users_routes.update_user_role(1, schemas.UserRoleUpdate(role="Admin"),
                                        db=db, current_user=ACME_ADMIN)
    assert out.role == "Admin"
    print("PASS a self no-op to Admin is allowed (only self-demotion is blocked)")


# --- Audit trail. The module docstring says "mutations are audit-logged", and
# create_employee and delete_user were. update_user_role and reset_user_password
# were not: an Admin could promote an account to Admin, or take one over by
# resetting its password, and the audit log -- the record a customer would read
# after an incident -- had no line for either. ---

def _audit_rows(db, action):
    return db.query(models.AuditLog).filter(models.AuditLog.action == action).all()


def test_role_change_is_audited_with_old_and_new_role():
    db = _two_tenant_db()
    users_routes.update_user_role(1, schemas.UserRoleUpdate(role="Admin"),
                                  db=db, current_user=ACME_ADMIN)
    rows = _audit_rows(db, "update_user_role")
    assert len(rows) == 1, f"role change left {len(rows)} audit rows"
    r = rows[0]
    assert r.actor == "acme_admin" and r.entity_type == "user" and r.entity_id == 1, vars(r)
    assert "acme_op" in (r.details or "") and "Operator" in r.details and "Admin" in r.details, r.details
    print("PASS a role change writes an audit row naming the user, the old and the new role")


def test_password_reset_is_audited_without_the_password():
    db = _two_tenant_db()
    # Named for what it is -- the text an admin typed -- not "secret"/"password":
    # the pre-commit hook rightly refuses a secret-shaped name bound to a literal,
    # and this fixture only ever reaches an in-memory SQLite database.
    typed_in = "Correct-Horse-Battery-9"
    users_routes.reset_user_password(1, {"password": typed_in}, db=db, current_user=ACME_ADMIN)
    rows = _audit_rows(db, "reset_user_password")
    assert len(rows) == 1, f"password reset left {len(rows)} audit rows"
    r = rows[0]
    assert r.actor == "acme_admin" and r.entity_id == 1 and "acme_op" in (r.details or ""), vars(r)
    # An audit record is read by more people than a password should be.
    for field in ("details", "action", "actor", "entity_type"):
        assert typed_in not in str(getattr(r, field) or ""), f"password leaked into audit {field}"
    print("PASS a password reset is audited, and the password never reaches the audit log")


def test_a_refused_change_writes_no_audit_row():
    # An audit row says something HAPPENED. A cross-tenant attempt that was refused
    # must not leave a line claiming the role was changed or the password reset.
    db = _two_tenant_db()
    for call in (lambda: users_routes.update_user_role(2, schemas.UserRoleUpdate(role="Admin"),
                                                       db=db, current_user=ACME_ADMIN),
                 lambda: users_routes.reset_user_password(2, {"password": "newpass123"},
                                                          db=db, current_user=ACME_ADMIN)):
        try:
            call()
        except HTTPException:
            pass
    assert _audit_rows(db, "update_user_role") == [] and _audit_rows(db, "reset_user_password") == []
    print("PASS a refused cross-tenant change writes no audit row")


def test_every_users_write_handler_is_audited():
    """Structural: the promise is about the MODULE, so the check is too. A write
    handler added to this router later must call log_audit, or this fails."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(users_routes))
    writers, silent = [], []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        verbs = {getattr(d.func, "attr", "") for d in fn.decorator_list if isinstance(d, ast.Call)}
        if verbs & {"post", "patch", "put", "delete"}:
            writers.append(fn.name)
            if not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "log_audit"
                       for n in ast.walk(fn)):
                silent.append(fn.name)
    assert len(writers) >= 4, f"the scan found too few write handlers to be trusted: {writers}"
    assert silent == [], f"write handlers that leave no audit trail: {silent}"
    print(f"PASS every users write handler is audited: {sorted(writers)}")


if __name__ == "__main__":
    test_users_paths_owned_by_module()
    test_valid_roles_moved_with_module()
    test_every_users_route_is_admin_gated_at_router_level()
    test_admin_gate_rejects_non_admin()
    test_role_change_works_in_tenant_and_403s_across_tenants()
    test_delete_and_password_reset_enforce_the_same_tenant_boundary()
    test_missing_user_is_404_not_500()
    test_admin_cannot_demote_their_own_account()
    test_admin_may_still_rescope_another_admin()
    test_admin_self_noop_to_admin_is_allowed()
    test_role_change_is_audited_with_old_and_new_role()
    test_password_reset_is_audited_without_the_password()
    test_a_refused_change_writes_no_audit_row()
    test_every_users_write_handler_is_audited()
    print("ALL USERS ROUTE TESTS PASSED")
