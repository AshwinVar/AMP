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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
import schema_guard
import schemas
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
        "unit_value_gbp": c.unit_value_gbp,
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


router = APIRouter(tags=["Platform"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── Health (public — for uptime monitors) ─────────────────────


@router.get("/health")
def health():
    """LIVENESS: is this process alive, and can it reach its database?

    Return the health in the HTTP STATUS, not just the body: an uptime
    monitor checks the status code. 200 when the DB answers, 503 when it
    doesn't — so a dead database is actually detectable instead of hiding
    behind a 200 with "down" text.

    IT IS NOT READINESS, AND #513 IS WHY THAT DISTINCTION IS NOW WRITTEN DOWN.
    Through a day-long total authentication outage this endpoint answered 200
    with `"status": "ok"`, because a schema mismatch is invisible to `SELECT 1`.
    It now carries the schema verdict and downgrades its own status to
    "degraded" when the schema is wrong, so it can never again describe a
    product nobody can log into as healthy.

    The STATUS CODE deliberately stays about liveness. Railway's restart policy
    and any uptime monitor key off it, and a 503 here would put the container in
    a restart loop that cannot possibly fix a schema problem — while paging
    somebody for an incident whose fix is a migration, not a reboot. The probe
    that gates traffic is /readiness (ADR-0018).
    """
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    schema = schema_guard.evaluate(engine) if db_ok else None
    schema_ok = bool(schema and schema["ok"])

    body = {
        "status": "ok" if (db_ok and schema_ok) else "degraded",
        "database": "ok" if db_ok else "down",
        "schema": (schema["state"] if schema else "unknown"),
        "time": datetime.utcnow().isoformat(),
        "version": BUILD_SHA,   # short git sha of the running build, or null
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


@router.get("/readiness")
def readiness():
    """READINESS: should this instance be given traffic?

    200 only when the database answers AND its schema is at the revision this
    build was written against. 503 otherwise, with the two revisions in the body
    so the answer to "why is the deploy not cutting over" is the response itself
    rather than a log dig.

    railway.toml points its healthcheck HERE, which is what makes the invariant
    hold at the platform level: a build whose migrations have not been applied
    never passes its healthcheck, so Railway never cuts traffic to it and the
    previous deployment goes on serving. Failing to deploy is a good day; #513
    was the alternative.

    Public and unauthenticated, like /health — it exposes a migration revision
    and a state word, which is operational metadata, not a secret. Deliberately
    NOT the database URL, the driver error text, or anything else that would
    describe how to reach the database.
    """
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    if not db_ok:
        return JSONResponse({
            "ready": False, "database": "down",
            "reason": "the database is not reachable",
            "time": datetime.utcnow().isoformat(), "version": BUILD_SHA,
        }, status_code=503)

    # Re-evaluated while unhealthy (see schema_guard.evaluate): an operator who
    # runs `python migrate.py` against a refusing instance sees it become ready
    # on the next probe, without a restart and without a redeploy.
    state = schema_guard.evaluate(engine)
    return JSONResponse({
        "ready": state["ok"],
        "database": "ok",
        "schema": {
            "state": state["state"],
            "expected_revision": state["expected"],
            "current_revision": state["current"],
            "reason": state["reason"],
        },
        "time": datetime.utcnow().isoformat(),
        "version": BUILD_SHA,
    }, status_code=200 if state["ok"] else 503)

# ── Tenant config: licensing / feature flags / branding ───────


@router.get("/tenant-config")
def tenant_config(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """The current workspace's company config — used by the frontend for
    branding and to decide which module packs to show. Follows the founder's
    company switcher (effective tenant), so previewing a client shows that
    client's licence and branding, not the founder's."""
    import tenancy
    tenant = tenancy.current_tenant() or current_user.get("tenant", "DEFAULT")
    return _config_dict(get_or_create_config(db, tenant))


@router.get("/modules")
def list_modules(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """The module manifest (modules.json) annotated for the caller's tenant: every
    pack with its views and an ``enabled`` flag set from the tenant's subscription
    (TenantConfig.enabled_modules). The frontend renders its nav from this, so a
    module appears in a tenant's AMP only when its pack is in their plan — the
    single, editable source of truth for the plug-and-play plugin system. Follows
    the founder's company switcher (effective tenant), like /tenant-config."""
    import module_manifest
    import tenancy
    tenant = tenancy.current_tenant() or current_user.get("tenant", "DEFAULT")
    cfg = get_or_create_config(db, tenant)
    enabled_ids = [m for m in (cfg.enabled_modules or "").split(",") if m]
    return {
        "tenant": tenant,
        "plan": cfg.plan,
        "enabled_modules": enabled_ids,
        "packs": module_manifest.packs_for_tenant(enabled_ids),
        "plan_bundles": module_manifest.plan_bundles(),
    }


@router.patch("/tenant-config")
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
    # £ per good unit — a tenant Admin sets their own margin so the recovery
    # read-model can value the OEE gap. null/"" clears it (back to units-only).
    if "unit_value_gbp" in payload:
        raw = payload["unit_value_gbp"]
        if raw is None or raw == "":
            c.unit_value_gbp = None
        else:
            try:
                val = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="unit_value_gbp must be a number")
            if val < 0:
                raise HTTPException(status_code=400, detail="unit_value_gbp must be >= 0")
            c.unit_value_gbp = val
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


@router.get("/tenant-configs")
def all_tenant_configs(db: Session = Depends(get_db),
                       current_user: dict = Depends(require_roles(["Admin"]))):
    if current_user.get("tenant", "DEFAULT") != "DEFAULT":
        raise HTTPException(status_code=403, detail="Platform owner only")
    return [_config_dict(c) for c in db.query(models.TenantConfig).order_by(models.TenantConfig.id).all()]


@router.patch("/tenant-configs/{tenant_code}")
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
    # A licence change must take effect at once — the plan-gate caches each
    # tenant's packs for ~60s, so drop the stale entry (the self-service
    # update_tenant_config already does this; this cross-tenant path didn't).
    import plan_gate
    plan_gate.invalidate(tenant_code)
    log_audit(db, current_user.get("sub"), "update_tenant_license", "tenant", None, tenant_code)
    return _config_dict(c)


@router.post("/tenant-configs/{tenant_code}/apply-plan")
def apply_plan(tenant_code: str, payload: dict, db: Session = Depends(get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    """Platform owner assigns a tenant a subscription plan, setting its module
    bundle from the manifest (modules.json) in one call — so the tenant's AMP
    immediately shows exactly that plan's modules. Validates the plan against the
    manifest, invalidates the plan-gate cache so it takes effect at once, and
    audits it. Founder (DEFAULT) only: it licenses another company."""
    if current_user.get("tenant", "DEFAULT") != "DEFAULT":
        raise HTTPException(status_code=403, detail="Platform owner only")
    import module_manifest
    import plan_gate
    plan = (payload.get("plan") or "").strip().lower()
    bundles = module_manifest.plan_bundles()
    if plan not in bundles:
        raise HTTPException(status_code=400,
                            detail=f"Unknown plan '{plan}'. Choose one of: {', '.join(sorted(bundles))}")
    c = get_or_create_config(db, tenant_code)
    c.plan = plan
    c.enabled_modules = ",".join(bundles[plan])
    db.commit()
    plan_gate.invalidate(tenant_code)
    log_audit(db, current_user.get("sub"), "apply_plan", "tenant", None, f"{tenant_code}:{plan}")
    return _config_dict(c)

# ── Audit log ─────────────────────────────────────────────────


@router.get("/audit-logs")
def audit_logs(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    rows = db.query(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(200).all()
    return [
        {"id": r.id, "actor": r.actor, "action": r.action, "entity_type": r.entity_type,
         "entity_id": r.entity_id, "details": r.details, "created_at": r.created_at}
        for r in rows
    ]


@router.post("/audit-logs", response_model=schemas.AuditLogResponse)
def create_audit_log(payload: schemas.AuditLogCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    """Append an audit record, attributed to the AUTHENTICATED caller.

    `actor` used to come straight out of the request body
    (`models.AuditLog(**payload.model_dump())`), so the trail had no binding to
    the identity that wrote it. A Supervisor — who passed the old
    require_roles(["Admin","Supervisor"]) write gate but gets 403 on the GET —
    could post {"actor": "alice_admin", "action": "delete_user", ...} and produce
    a row indistinguishable from the ones log_audit() writes with
    current_user["sub"]. Same table, same shape, no provenance flag. Measured:

        POST (write)  Supervisor -> allowed
        GET  (read)   Supervisor -> HTTP 403
        trail shows   actor=alice_admin   (the caller was sup_mallory)

    And not only via curl: the dashboard's report form posted
    `actor: reportForm.requested_by`, a free-text input, so client-typed
    attribution was the NORMAL path, not an attack.

    Stamping from the token is the fix; `actor` is gone from AuditLogCreate so a
    caller cannot believe it still works. The write is also narrowed to Admin, so
    nobody writes into a trail they cannot read — verified safe: the only caller
    is the report form, which renders inside the "enterprise" view and that view
    is in the frontend's ADMIN_ONLY_VIEWS, and every internal caller uses
    log_audit() directly rather than this route.

    The `or "system"` is belt-and-braces, NOT load-bearing: actor is
    Column(String, default="system"), a PYTHON-side default that SQLAlchemy
    applies when the attribute is None at flush, so a token with no `sub` claim
    records "system" either way (mutation testing established this — removing
    only the `or` changes nothing observable; removing it AND the column default
    writes NULL and 500s on the response_model). Kept because it mirrors
    log_audit's own `actor or "system"` two hundred lines up.
    """
    row = models.AuditLog(**payload.model_dump(),
                          actor=current_user.get("sub") or "system")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
