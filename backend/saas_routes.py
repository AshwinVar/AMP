"""SaaS / tenant-lifecycle routes (ADR-0008) — the founder's control plane.

The tenant registry and its lifecycle: list, create (with starter-factory
onboarding + plan-driven licence), provision the tenant admin, change plan /
status, and delete (with optional full data purge). Peeled out of main.py,
following the register(app) pattern.

Founder-only actions gate on the RAW JWT claim (not the X-Tenant preview): a
tenant Admin manages their factory, never the registry. The list / analytics
endpoints are registry-scoped — a client workspace sees only its own row.

The route handlers are module-level (not nested in register) because they are
unit-tested directly by name (test_onboarding / test_offboarding call
saas_routes.<handler>); register() just attaches them to the app.
"""
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import offboard_tenant
import onboard_tenant
import plan_gate
import platform_routes
import schemas
import tenancy
from auth import get_current_user, require_roles
from database import SessionLocal
from platform_routes import log_audit
from security import hash_password


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _registry_scope(query, current_user):
    """The tenant registry is founder data. A non-DEFAULT workspace sees only
    its own row — scoped by the raw JWT claim (not the X-Tenant preview), and
    returned as data rather than a 403 so client dashboards' batched fetch
    keeps working."""
    claim = current_user.get("tenant", tenancy.DEFAULT_TENANT)
    if claim != tenancy.DEFAULT_TENANT:
        query = query.filter(models.CompanyTenant.company_code == claim)
    return query


def _require_founder(current_user):
    """Tenant lifecycle is founder-only: the caller's own workspace (the JWT
    claim — deliberately not the X-Tenant preview) must be DEFAULT. A tenant
    Admin manages their factory, not the tenant registry."""
    if current_user.get("tenant", tenancy.DEFAULT_TENANT) != tenancy.DEFAULT_TENANT:
        raise HTTPException(status_code=403, detail="Only the platform workspace can manage tenants")


def get_company_tenants(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    q = _registry_scope(db.query(models.CompanyTenant), current_user)
    return q.order_by(models.CompanyTenant.id.desc()).limit(300).all()


def create_company_tenant(tenant: schemas.CompanyTenantCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    _require_founder(current_user)
    existing = db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == tenant.company_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Company code already exists")
    row = models.CompanyTenant(**tenant.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    # Onboarding: give the new tenant a generic starter factory so its first
    # login lands on a living dashboard, not an empty one (skips if data exists).
    try:
        onboard_tenant.seed_starter_factory(db, row.company_code, row.company_name or "")
    except Exception as e:
        print(f"[ONBOARD] starter seed for {row.company_code} failed: {e}")
    # Licence follows the chosen plan (Starter/Professional/Enterprise tiers).
    try:
        platform_routes.apply_plan_tier(db, row.company_code, row.plan_name)
    except Exception as e:
        print(f"[ONBOARD] plan tier for {row.company_code} failed: {e}")
    return row


def create_tenant_admin(tenant_id: int, db: Session = Depends(_get_db),
                        current_user: dict = Depends(require_roles(["Admin"]))):
    """Founder-only: provision the tenant's Admin login with a generated
    temporary password. The password is returned ONCE in this response and
    stored only as a bcrypt hash — hand it to the customer, who should rotate
    it via /auth/change-password after first login."""
    _require_founder(current_user)
    row = db.query(models.CompanyTenant).filter(models.CompanyTenant.id == tenant_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    username = f"{row.company_code.lower()}_admin"
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(status_code=400, detail=f"Admin login '{username}' already exists for this tenant")
    temp_password = secrets.token_urlsafe(9)
    db.add(models.User(username=username, password=hash_password(temp_password),
                       role="Admin", tenant_code=row.company_code))
    db.commit()
    log_audit(db, current_user.get("sub", "?"), "provision_admin", "user", None,
              f"tenant={row.company_code} username={username}")
    return {
        "username": username,
        "temporary_password": temp_password,
        "company_code": row.company_code,
        "note": "Shown once. Share securely; the customer should change it after first login.",
    }


def update_company_tenant(tenant_id: int, payload: schemas.CompanyTenantUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    _require_founder(current_user)
    row = db.query(models.CompanyTenant).filter(models.CompanyTenant.id == tenant_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    if "plan_name" in changes:
        # Changing the plan re-syncs the licence (which module packs unlock).
        try:
            platform_routes.apply_plan_tier(db, row.company_code, row.plan_name)
        except Exception as e:
            print(f"[SAAS] plan tier for {row.company_code} failed: {e}")
    return row


def delete_company_tenant(tenant_id: int, purge: bool = False, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    """Founder-only. Removes the registry row; with ``?purge=true`` also
    permanently deletes ALL of the tenant's data across every tenant-aware
    table (machines, records, orders, users, licence — everything except the
    immutable event history). The purge is irreversible and audit-logged."""
    _require_founder(current_user)
    row = db.query(models.CompanyTenant).filter(models.CompanyTenant.id == tenant_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    code = row.company_code
    db.delete(row)
    db.commit()
    purged = None
    if purge:
        try:
            purged = offboard_tenant.purge_tenant_data(db, code)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            # Raise a HANDLED error: unhandled exceptions bypass CORS and the
            # browser sees an opaque network failure instead of this message.
            raise HTTPException(status_code=500, detail=f"Data purge failed: {e}")
        plan_gate.invalidate(code)
        log_audit(db, current_user.get("sub", "?"), "purge_tenant", "tenant", None,
                  f"tenant={code} rows={sum(purged.values())} tables={len(purged)}")
    return {"message": "Tenant deleted successfully", "purged": purged}


def get_saas_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    rows = _registry_scope(db.query(models.CompanyTenant), current_user).all()
    return {
        "total_tenants": len(rows),
        "trial": len([r for r in rows if r.subscription_status == "Trial"]),
        "active": len([r for r in rows if r.subscription_status == "Active"]),
        "past_due": len([r for r in rows if r.subscription_status == "Past Due"]),
        "cancelled": len([r for r in rows if r.subscription_status == "Cancelled"]),
        "monthly_recurring_revenue": sum(r.monthly_fee for r in rows if r.subscription_status in ["Trial", "Active"]),
        "total_seats": sum(r.seats for r in rows),
    }


router = APIRouter(tags=["SaaS Admin"])
router.get("/saas/tenants", response_model=List[schemas.CompanyTenantResponse])(get_company_tenants)
router.post("/saas/tenants", response_model=schemas.CompanyTenantResponse)(create_company_tenant)
router.post("/saas/tenants/{tenant_id}/admin")(create_tenant_admin)
router.patch("/saas/tenants/{tenant_id}", response_model=schemas.CompanyTenantResponse)(update_company_tenant)
router.delete("/saas/tenants/{tenant_id}")(delete_company_tenant)
router.get("/analytics/saas")(get_saas_analytics)
