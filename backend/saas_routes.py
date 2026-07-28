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
from sqlalchemy import func, or_
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
    # seats/monthly_fee are required numeric columns (Integer, Python-side default
    # only — NOT nullable=False). A partial update omits a field entirely; an
    # explicit `null` is not "leave unchanged", it would BLANK the column to NULL.
    # A NULL then TypeError-500s /analytics/saas (sum over seats/fees) and fails
    # the non-optional CompanyTenantResponse on the very next list. Reject the null
    # here so the invariant "these are never NULL" holds at the write boundary.
    for field in ("seats", "monthly_fee"):
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=400, detail=f"{field} must be a number")
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
    # Aggregate the registry in SQL rather than hydrating every CompanyTenant row
    # into Python just to count/sum it (rule-4: bound a growing, polled table).
    # company_tenants grows with every trial signup and churned/cancelled customer,
    # and this founder panel is polled by the SaaS dashboard — the old
    # db.query(CompanyTenant).all() streamed the whole registry back on each poll to
    # do work SQL does in one pass. Same GROUP-BY / COALESCE(SUM(..),0) fix already
    # applied across the /analytics/* endpoints (#328/#331/#317/#315). _registry_scope
    # still filters to the caller's own row for a client workspace (founder sees all),
    # applied to every aggregate so the scoping is identical to the old .all() scan.
    def scoped(q):
        return _registry_scope(q, current_user)

    total_tenants = scoped(db.query(func.count(models.CompanyTenant.id))).scalar() or 0

    # One row per distinct status — a small, naturally-bounded result set. A NULL
    # subscription_status (String default only) simply lands in a key we never read,
    # so it's counted in total_tenants but in none of the named buckets — exactly as
    # the old `status == "Trial"` comparisons treated it (None matches no bucket).
    status_counts = dict(
        scoped(db.query(models.CompanyTenant.subscription_status, func.count()))
        .group_by(models.CompanyTenant.subscription_status)
        .all()
    )

    # NULL-safe, same basis as before: the columns carry a Python-side default only
    # (NOT nullable=False), so a legacy / raw-SQL / migration row can hold a NULL fee
    # or seat, and a bare SUM over that None used to TypeError-500 the whole rollup.
    # COALESCE(SUM(..), 0) counts a NULL fee/seat as 0 — never fabricating a figure
    # the data doesn't hold, a real recorded 0 already contributes 0 — and yields a
    # clean 0 on an empty registry too. MRR keeps its Trial+Active filter; a NULL
    # status is outside `IN ('Trial','Active')` exactly as it was outside the old
    # Python `in [...]` membership test, so both agree on which rows count.
    mrr = scoped(
        db.query(func.coalesce(func.sum(models.CompanyTenant.monthly_fee), 0))
        .filter(models.CompanyTenant.subscription_status.in_(["Trial", "Active"]))
    ).scalar() or 0
    total_seats = scoped(
        db.query(func.coalesce(func.sum(models.CompanyTenant.seats), 0))
    ).scalar() or 0

    return {
        "total_tenants": int(total_tenants),
        "trial": status_counts.get("Trial", 0),
        "active": status_counts.get("Active", 0),
        "past_due": status_counts.get("Past Due", 0),
        "cancelled": status_counts.get("Cancelled", 0),
        "monthly_recurring_revenue": int(mrr),
        "total_seats": int(total_seats),
    }


def get_tenant_activity(db: Session = Depends(_get_db),
                        current_user: dict = Depends(require_roles(["Admin"]))):
    """Founder-only adoption panel: is each client actually USING AMP? Per tenant
    (the registry rows + the founder's own DEFAULT workspace): user and machine
    counts, production records and customer orders booked in the last 7 days,
    the open escalation load, when production was last recorded, and an
    ``active`` flag (any production or order activity in the window).

    Cross-tenant reads go through the sanctioned mechanism — set_current_tenant
    per tenant so the ADR-0002 auto-scoping applies to every count (the same
    pattern the starter-factory seeder uses) — never by disabling scoping. Each
    tenant costs a handful of SQL COUNT/MAX queries (bounded by the 7-day
    window), so the panel stays cheap as the registry grows."""
    from datetime import datetime, timedelta
    _require_founder(current_user)
    cutoff = datetime.utcnow() - timedelta(days=7)

    codes = [tenancy.DEFAULT_TENANT] + [
        t.company_code for t in db.query(models.CompanyTenant)
        .order_by(models.CompanyTenant.id).all()
        if t.company_code != tenancy.DEFAULT_TENANT
    ]
    rows = []
    for code in codes:
        token = tenancy.set_current_tenant(code)
        try:
            # User is NOT auto-scoped (ADR-0002 scopes the factory tables; the
            # /users endpoints filter explicitly) — so filter explicitly here too,
            # or every tenant's row would count the whole platform's logins.
            users = db.query(models.User).filter(models.User.tenant_code == code).count()
            machines = db.query(models.Machine).count()
            production_7d = (db.query(models.ProductionRecord)
                             .filter(models.ProductionRecord.created_at >= cutoff).count())
            orders_7d = (db.query(models.CustomerOrder)
                         .filter(models.CustomerOrder.created_at >= cutoff).count())
            # "Open" = not in a terminal state. status is Column(String,
            # default="Open") WITHOUT nullable=False, so a raw-SQL / migration /
            # cleared-field row can hold a NULL — and SQL's `status NOT IN (...)`
            # evaluates to NULL (not TRUE) for it, silently DROPPING an open
            # NULL-status escalation from this count and undercounting the panel.
            # OR the NULL back in (a NULL status is not a terminal state, i.e.
            # still open), matching the reconciled open-escalation convention the
            # dashboard headline and the maintenance/late-order counts already use
            # (analytics /analytics/escalations #295, system-notifications #403).
            open_escalations = (db.query(models.Escalation)
                                .filter(or_(
                                    models.Escalation.status.is_(None),
                                    ~models.Escalation.status.in_(
                                        ("Resolved", "Cancelled", "Closed")),
                                )).count())
            last_production = (db.query(models.ProductionRecord.created_at)
                               .order_by(models.ProductionRecord.created_at.desc())
                               .limit(1).scalar())
        finally:
            tenancy.reset_current_tenant(token)
        rows.append({
            "tenant_code": code,
            "users": users,
            "machines": machines,
            "production_7d": production_7d,
            "orders_7d": orders_7d,
            "open_escalations": open_escalations,
            "last_production_at": last_production.isoformat() if last_production else None,
            "active": bool(production_7d or orders_7d),
        })
    # Quietest tenants first — the churn risks the founder should chase.
    rows.sort(key=lambda r: (r["active"], r["production_7d"] + r["orders_7d"]))
    return {
        "days": 7,
        "tenants": rows,
        "active_count": sum(1 for r in rows if r["active"]),
        "quiet_count": sum(1 for r in rows if not r["active"]),
    }


router = APIRouter(tags=["SaaS Admin"])
router.get("/saas/tenant-activity")(get_tenant_activity)
router.get("/saas/tenants", response_model=List[schemas.CompanyTenantResponse])(get_company_tenants)
router.post("/saas/tenants", response_model=schemas.CompanyTenantResponse)(create_company_tenant)
router.post("/saas/tenants/{tenant_id}/admin")(create_tenant_admin)
router.patch("/saas/tenants/{tenant_id}", response_model=schemas.CompanyTenantResponse)(update_company_tenant)
router.delete("/saas/tenants/{tenant_id}")(delete_company_tenant)
router.get("/analytics/saas")(get_saas_analytics)
