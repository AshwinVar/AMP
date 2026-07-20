"""
AMP platform layer — the SaaS plumbing that sits under every module.

This one module delivers several "enterprise platform" capabilities:
  * Licensing / feature flags  -> TenantConfig.enabled_modules + plan
  * White-label branding       -> TenantConfig.brand_name / brand_color / brand_logo_url
  * Subscription / trial state -> TenantConfig.subscription_status + trial_ends_at
  * Audit logging              -> log_audit() + GET /audit-logs
  * Health check               -> GET /health (public, for uptime monitors)

Everything is keyed by `tenant_code` — the same tenant identity used across
users and the GMATS inventory — so a company's licence and branding follow it
everywhere. Registered from main.py at import time via register(app).
"""
import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from auth import get_current_user, require_roles
from database import SessionLocal, engine

# The running build's git commit, if the platform exposes it (Railway sets
# RAILWAY_GIT_COMMIT_SHA automatically). Short, public, resolved once at import.
# Lets ops confirm which build is live — and confirms a deploy actually cut over.
BUILD_SHA = (os.environ.get("RAILWAY_GIT_COMMIT_SHA")
             or os.environ.get("GIT_COMMIT_SHA") or "")[:7] or None


# Defaults applied the first time we see a tenant. DEFAULT is the founder/demo
# workspace (everything on); GMATS is the first client (growth plan, own brand).
_TENANT_DEFAULTS = {
    "DEFAULT": dict(plan="demo",   enabled_modules="core,operations,factory,intelligence,admin",
                    brand_name="AMP",            brand_color="#6366f1"),
    "GMATS":   dict(plan="growth", enabled_modules="core,operations,factory",
                    brand_name="GMATS Compressors",  brand_color="#e11d2a"),
}


# SaaS plan (CompanyTenant.plan_name, what the founder picks in SaaS Admin) →
# licence tier (TenantConfig.plan + enabled_modules, what the frontend obeys).
# "admin" stays in every tier — the frontend force-enables core+admin anyway so
# no tenant is locked out of account management.
PLAN_MODULE_TIERS = {
    "starter": ("starter", "core"),
    "growth": ("growth", "core,operations,factory"),
    "professional": ("growth", "core,operations,factory"),
    "enterprise": ("enterprise", "core,operations,factory,intelligence,admin"),
}


def apply_plan_tier(db, tenant_code, plan_name):
    """Sync a tenant's licence to its SaaS plan. Called when the founder creates
    a tenant or changes its plan; unknown plan names fail open to enterprise."""
    tier, modules = PLAN_MODULE_TIERS.get((plan_name or "").strip().lower(),
                                          PLAN_MODULE_TIERS["enterprise"])
    c = get_or_create_config(db, tenant_code)
    c.plan = tier
    c.enabled_modules = modules
    db.commit()
    # The API gate caches licences briefly — a plan change applies immediately.
    import plan_gate
    plan_gate.invalidate(tenant_code)
    return c


def log_audit(db, actor, action, entity_type=None, entity_id=None, details=None):
    """Append an audit record. Safe to call anywhere — never raises."""
    try:
        db.add(models.AuditLog(
            actor=actor or "system", action=action,
            entity_type=entity_type, entity_id=entity_id, details=details,
        ))
        db.commit()
    except Exception:
        db.rollback()


def _config_dict(c):
    return {
        "tenant_code": c.tenant_code,
        "plan": c.plan,
        "enabled_modules": [m for m in (c.enabled_modules or "").split(",") if m],
        "brand_name": c.brand_name,
        "brand_color": c.brand_color,
        "brand_logo_url": c.brand_logo_url,
        "subscription_status": c.subscription_status,
        "trial_ends_at": c.trial_ends_at,
    }


def get_or_create_config(db, tenant_code):
    """Return a tenant's config, creating it from defaults (30-day trial) on first sight."""
    c = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == tenant_code).first()
    if not c:
        d = _TENANT_DEFAULTS.get(tenant_code, dict(
            plan="enterprise",
            enabled_modules="core,operations,factory,intelligence,admin",
            brand_name="AMP", brand_color="#6366f1",
        ))
        c = models.TenantConfig(
            tenant_code=tenant_code, subscription_status="trial",
            trial_ends_at=datetime.utcnow() + timedelta(days=30), **d,
        )
        db.add(c); db.commit(); db.refresh(c)
    return c


def seed_tenant_configs(db):
    for code in _TENANT_DEFAULTS:
        get_or_create_config(db, code)


def register(app):
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # ── Health (public — for uptime monitors) ─────────────────────
    @app.get("/health")
    def health():
        # Return the health in the HTTP STATUS, not just the body: an uptime
        # monitor (and Railway's probe, if pointed here) checks the status code.
        # 200 when the DB answers, 503 when it doesn't — so a dead database is
        # actually detectable instead of hiding behind a 200 with "down" text.
        db_ok = True
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
        body = {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "down",
            "time": datetime.utcnow().isoformat(),
            "version": BUILD_SHA,   # short git sha of the running build, or null
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    # ── Tenant config: licensing / feature flags / branding ───────
    @app.get("/tenant-config")
    def tenant_config(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        """The current workspace's company config — used by the frontend for
        branding and to decide which module packs to show. Follows the founder's
        company switcher (effective tenant), so previewing a client shows that
        client's licence and branding, not the founder's."""
        import tenancy
        tenant = tenancy.current_tenant() or current_user.get("tenant", "DEFAULT")
        return _config_dict(get_or_create_config(db, tenant))

    @app.patch("/tenant-config")
    def update_tenant_config(payload: dict, db: Session = Depends(get_db),
                             current_user: dict = Depends(require_roles(["Admin"]))):
        """A client Admin may re-brand their own workspace (the founder, while
        switched, edits the previewed tenant's branding). Plan/licensing edits
        stay gated on the raw claim — platform owner only."""
        import tenancy
        tenant = tenancy.current_tenant() or current_user.get("tenant", "DEFAULT")
        is_platform_owner = current_user.get("tenant", "DEFAULT") == "DEFAULT"
        c = get_or_create_config(db, tenant)
        for f in ("brand_name", "brand_color", "brand_logo_url"):
            if f in payload:
                setattr(c, f, payload[f])
        if is_platform_owner:
            for f in ("plan", "subscription_status"):
                if f in payload:
                    setattr(c, f, payload[f])
            if "enabled_modules" in payload:
                mods = payload["enabled_modules"]
                c.enabled_modules = ",".join(mods) if isinstance(mods, list) else mods
        db.commit()
        log_audit(db, current_user.get("sub"), "update_tenant_config", "tenant", None, tenant)
        return _config_dict(c)

    # Platform-owner (DEFAULT tenant) view: license/brand ANY client.
    @app.get("/tenant-configs")
    def all_tenant_configs(db: Session = Depends(get_db),
                           current_user: dict = Depends(require_roles(["Admin"]))):
        if current_user.get("tenant", "DEFAULT") != "DEFAULT":
            raise HTTPException(status_code=403, detail="Platform owner only")
        return [_config_dict(c) for c in db.query(models.TenantConfig).order_by(models.TenantConfig.id).all()]

    @app.patch("/tenant-configs/{tenant_code}")
    def update_any_tenant(tenant_code: str, payload: dict, db: Session = Depends(get_db),
                          current_user: dict = Depends(require_roles(["Admin"]))):
        if current_user.get("tenant", "DEFAULT") != "DEFAULT":
            raise HTTPException(status_code=403, detail="Platform owner only")
        c = get_or_create_config(db, tenant_code)
        for f in ("plan", "brand_name", "brand_color", "brand_logo_url", "subscription_status"):
            if f in payload:
                setattr(c, f, payload[f])
        if "enabled_modules" in payload:
            mods = payload["enabled_modules"]
            c.enabled_modules = ",".join(mods) if isinstance(mods, list) else mods
        db.commit()
        log_audit(db, current_user.get("sub"), "update_tenant_license", "tenant", None, tenant_code)
        return _config_dict(c)

    # ── Audit log ─────────────────────────────────────────────────
    @app.get("/audit-logs")
    def audit_logs(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
        rows = db.query(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(200).all()
        return [
            {"id": r.id, "actor": r.actor, "action": r.action, "entity_type": r.entity_type,
             "entity_id": r.entity_id, "details": r.details, "created_at": r.created_at}
            for r in rows
        ]
