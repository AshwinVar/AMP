"""Core routes — the irreducible endpoints that never fit a domain module.

Auth & bootstrap (/, /me, /register, /login, /auth/refresh,
/auth/change-password), the AI-platform self-report (/platform/status, which
reads the simulator heartbeat from sim_state), the BOM view (/bom), and the
intelligence stragglers that lean on the shared engines — /briefing/escalate,
/escalations/from-smart-alerts, /reports/daily-summary.txt, /ops-trends.

These were the last endpoints left directly on `app` in main.py; grouping them
behind one router (tag "Core") leaves main.py to assemble the app and own only
the lifecycle bits (the sim loop, startup, the live websocket) — no @app routes.
Per ADR-0009; imports only lower-level/shared modules, never main.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import ai
import ai.agents
import ai.platform_status
import ai.trends
import models
import schemas
import sim_state
import tenancy
from analytics_engine import generate_alerts
from analytics_routes import analytics_summary
from auth import create_access_token, get_current_user, require_roles
from bom import PART_BOM
from database import SessionLocal
from platform_routes import log_audit
from security import hash_password, needs_rehash, verify_password
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(tags=["Core"])


@router.get("/")
def root():
    return {"message": "AMP Backend Running"}


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return current_user


@router.post("/register", response_model=schemas.UserResponse)
def register_user(user: schemas.UserCreate, db: Session = Depends(_get_db)):
    """Bootstrap only: creates the very first Admin when the system has no users.
    Once any user exists, self-registration is disabled — an Admin must add employees."""
    if db.query(models.User).count() > 0:
        raise HTTPException(status_code=403, detail="Self-registration is disabled. Ask your Admin to add you.")

    try:
        new_user = models.User(
            username=user.username,
            password=hash_password(user.password),
            role="Admin",            # the first account is always the Admin
            tenant_code="DEFAULT",
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return new_user
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Database integrity error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Register failed: {str(e)}")


@router.post("/login")
def login(user: schemas.UserLogin, db: Session = Depends(_get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()

    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid username")

    if not verify_password(user.password, db_user.password):
        raise HTTPException(status_code=401, detail="Invalid password")

    # Transparently upgrade legacy SHA-256 hashes to bcrypt on successful login.
    if needs_rehash(db_user.password):
        try:
            db_user.password = hash_password(user.password)
            db.commit()
        except Exception:
            db.rollback()

    tenant = getattr(db_user, "tenant_code", None) or tenancy.CLIENT_TENANTS.get(db_user.username.lower(), "DEFAULT")

    # Subscription enforcement: a cancelled company can no longer sign in.
    # Only applies when the tenant has a registry row that says Cancelled —
    # tenants outside the registry (legacy) and Trial/Active/Past Due all pass.
    if tenant != tenancy.DEFAULT_TENANT:
        reg = db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == tenant).first()
        if reg and reg.subscription_status == "Cancelled":
            log_audit(db, db_user.username, "login_blocked", "user", db_user.id, f"tenant={tenant} cancelled")
            raise HTTPException(status_code=403, detail="Subscription inactive — contact your provider")
        if reg and reg.trial_expired:
            log_audit(db, db_user.username, "login_blocked", "user", db_user.id, f"tenant={tenant} trial expired")
            raise HTTPException(status_code=403, detail="Trial expired — contact your provider to activate your subscription")

    log_audit(db, db_user.username, "login", "user", db_user.id, f"tenant={tenant}")
    token = create_access_token(data={"sub": db_user.username, "role": db_user.role, "tenant": tenant})

    return {
        "access_token": token,
        "token_type": "bearer",
        "role": db_user.role,
        "tenant": tenant,
    }


@router.post("/briefing/escalate")
def escalate_briefing(db: Session = Depends(_get_db),
                      current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    # Proactive briefing (ADR-0005): the Escalation agent turns the briefing's most
    # urgent (high-severity) alert into a proposed escalation in the approval queue.
    result = ai.agents.escalate_from_briefing(db, request_tenant(current_user))
    db.commit()
    return result


@router.post("/auth/refresh")
def refresh_token(current_user: dict = Depends(get_current_user)):
    # Sliding session: a valid (not-yet-expired) token can be exchanged for a
    # fresh one carrying the same identity claims. The frontend calls this when
    # the token nears expiry, so an active user is never logged out mid-shift —
    # while idle sessions still expire naturally.
    token = create_access_token(data={
        "sub": current_user.get("sub"),
        "role": current_user.get("role"),
        "tenant": current_user.get("tenant", "DEFAULT"),
    })
    return {"access_token": token, "token_type": "bearer"}


@router.post("/auth/change-password")
def change_password(payload: schemas.ChangePasswordRequest, db: Session = Depends(_get_db),
                    current_user: dict = Depends(get_current_user)):
    """Any signed-in user can rotate their own password (used after receiving a
    provisioned temporary password, or routinely)."""
    db_user = db.query(models.User).filter(models.User.username == current_user.get("sub")).first()
    if not db_user or not verify_password(payload.current_password, db_user.password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    db_user.password = hash_password(payload.new_password)
    db.commit()
    log_audit(db, db_user.username, "change_password", "user", db_user.id, "self-service")
    return {"message": "Password changed"}


# NOTE: /health is owned by platform_routes.register() (registered first, so it
# wins routing) and returns a truthful status code — 200 healthy / 503 DB down.
# A second /health used to be defined here; it was dead (shadowed) and always
# returned 200, so removing it changes nothing served while eliminating a
# duplicate that could silently disable DB monitoring if registration order
# ever changed. See platform_routes.py.


@router.get("/platform/status")
def platform_status(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # AI platform self-report (ADR-0003): registered read-models, the agent roster,
    # copilot connectivity, and the tenant's logged agent actions.
    result = ai.platform_status.build_platform_status(db, request_tenant(current_user))
    # Sim-loop diagnostics are founder-only: the allowlist names other tenants,
    # which a client workspace must not see.
    if current_user.get("tenant", tenancy.DEFAULT_TENANT) == tenancy.DEFAULT_TENANT:
        result["sim"] = {
            "tenants": sim_state.tenants,
            "last_tick_utc": sim_state.last_tick.isoformat() if sim_state.last_tick else None,
            "tick_count": sim_state.tick_count,
        }
    return result


@router.get("/bom")
def get_bom(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    rows = []
    for part_code, bom in PART_BOM.items():
        raw_item = None
        fg_item = None
        if bom["raw"]:
            raw_item = db.query(models.InventoryItem).filter(
                models.InventoryItem.item_code == bom["raw"]
            ).first()
        if bom["fg"]:
            fg_item = db.query(models.InventoryItem).filter(
                models.InventoryItem.item_code == bom["fg"]
            ).first()
        rows.append({
            "part_number": part_code,
            "raw_material_code": bom["raw"] or "—",
            "raw_material_name": raw_item.item_name if raw_item else "—",
            "consume_per_unit": bom["consume_per_unit"],
            "raw_unit": raw_item.unit if raw_item else "—",
            "finished_goods_code": bom["fg"] or "—",
            "finished_goods_name": fg_item.item_name if fg_item else "—",
            "raw_current_stock": raw_item.current_stock if raw_item else None,
            "raw_reorder_level": raw_item.reorder_level if raw_item else None,
        })
    return rows


@router.get("/reports/daily-summary.txt")
def daily_summary_report(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    summary = analytics_summary(db, current_user)
    report = f"""
AMP Daily Factory Summary
Generated: {datetime.utcnow().isoformat()} UTC

Machines: {summary["machines"]}
Running: {summary["running"]}
Breakdowns: {summary["breakdown"]}
Avg Utilization: {summary["avg_utilization"]}%
Avg OEE: {summary["avg_oee"]}%
Avg Availability: {summary["avg_availability"]}%
Avg Performance: {summary["avg_performance"]}%
Avg Quality: {summary["avg_quality"]}%
Downtime Events: {summary["downtime_events"]}
Total Downtime: {summary["total_downtime_minutes"]} minutes
Shift Efficiency: {summary["avg_shift_efficiency"]}%
Top Downtime Reason: {summary["top_reason"]}
Top Downtime Machine: {summary["top_machine"]}

Alerts:
{chr(10).join([f'- [{a["severity"]}] {a["message"]}' for a in summary["alerts"]]) or "No active alerts"}
"""
    return Response(content=report, media_type="text/plain", headers={"Content-Disposition": "attachment; filename=daily_summary_report.txt"})


@router.post("/escalations/from-smart-alerts")
def create_escalations_from_smart_alerts(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    alerts = generate_alerts(db)
    created = 0

    for alert in alerts:
        title = f'{alert.get("type", "Alert")} - {alert.get("machine", "Factory")}'

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        machine = (
            db.query(models.Machine)
            .filter(models.Machine.name == alert.get("machine"))
            .first()
        )

        escalation = models.Escalation(
            machine_id=machine.id if machine else None,
            title=title,
            severity=alert.get("severity", "Medium"),
            owner="Unassigned",
            department="Maintenance",
            status="Open",
            source="Smart Alert",
            notes=alert.get("message"),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}


@router.get("/ops-trends")
def get_ops_trends(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Ops trends (ADR-0007): last-7-days daily series across the four pillars —
    # production, downtime, quality, and agent activity — tenant-scoped.
    return ai.trends.build_ops_trends(db, request_tenant(current_user))

