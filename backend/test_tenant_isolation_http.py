"""Route-level tenant-isolation tests — audit trail + enterprise inventory.

Complements the repository-level tests in test_tenancy.py by driving the REAL
route handlers through the exact chain a request takes:

    JWT  ->  tenant_from_token / effective_tenant  (TenantScopeMiddleware's logic)
         ->  set_current_tenant                    (bind the request's tenant)
         ->  handler(db, current_user)             (auto-scoped ORM query)

Starlette's TestClient needs httpx, which is not a project dependency, so we
reproduce the middleware's tenant-derivation directly rather than spinning an
ASGI transport — the scoping path exercised is identical. get_current_user only
decodes the JWT (no DB load), so no seeded user is needed.

Run:  python backend/test_tenant_isolation_http.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import auth
import models
import platform_routes
import enterprise_inventory_routes
import tenancy as T
from database import Base
from tenancy import tenant_from_token, effective_tenant


def _fresh_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    def as_tenant(tenant, objs):
        tok = T.set_current_tenant(tenant)
        for o in objs:
            db.add(o)
        db.commit()
        T.reset_current_tenant(tok)

    as_tenant("TA", [
        models.AuditLog(actor="a_admin", action="login"),
        models.Remnant(tag_no="REM-A", item_id=1, original_qty=1, remaining_qty=1, unit="pc"),
    ])
    as_tenant("TB", [
        models.AuditLog(actor="b_admin", action="login"),
        models.Remnant(tag_no="REM-B", item_id=2, original_qty=1, remaining_qty=1, unit="pc"),
    ])
    db.add(models.AuditLog(actor="system", action="boot"))   # NULL tenant -> hidden
    db.commit()


def _request_as(tenant, handler, db):
    """Invoke a route handler exactly as a real request for `tenant` would: mint a
    JWT, derive+bind the tenant the way TenantScopeMiddleware does, decode the
    principal the way get_current_user does, then call the handler."""
    token = auth.create_access_token({"sub": f"{tenant}_admin", "role": "Admin", "tenant": tenant})
    bound = T.set_current_tenant(effective_tenant(tenant_from_token(token), None))
    try:
        current_user = auth.verify_token(token)          # == get_current_user's return
        return handler(db=db, current_user=current_user)
    finally:
        T.reset_current_tenant(bound)


def test_audit_logs_route_is_tenant_isolated():
    db = _fresh_session()
    _seed(db)
    a = _request_as("TA", platform_routes.audit_logs, db)
    b = _request_as("TB", platform_routes.audit_logs, db)
    assert {r["actor"] for r in a} == {"a_admin"}, a     # not b_admin, not system
    assert {r["actor"] for r in b} == {"b_admin"}, b
    print("PASS /audit-logs handler is tenant-isolated via the JWT->tenant chain (NULL hidden)")


def test_remnants_route_is_tenant_isolated():
    db = _fresh_session()
    _seed(db)
    a = _request_as("TA", enterprise_inventory_routes.get_remnants, db)
    b = _request_as("TB", enterprise_inventory_routes.get_remnants, db)
    assert {r["tag_no"] for r in a} == {"REM-A"}, a
    assert {r["tag_no"] for r in b} == {"REM-B"}, b
    print("PASS /remnants handler is tenant-isolated via the JWT->tenant chain")


if __name__ == "__main__":
    test_audit_logs_route_is_tenant_isolated()
    test_remnants_route_is_tenant_isolated()
    print("ROUTE TENANT-ISOLATION OK: audit trail + enterprise inventory isolated through the request chain")
