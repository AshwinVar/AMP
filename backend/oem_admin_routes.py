"""Founder-side OEM onboarding: creating a manufacturer and its first login.

WHY THIS IS SEPARATE FROM oem_routes.py
---------------------------------------
Everything in `oem_routes.py` is done BY a manufacturer, authenticated as an OEM
principal. Everything here is done TO one, by the platform operator, and an OEM
principal must never reach it: an organisation that could create itself, rename
itself, or reactivate itself after suspension is not an organisation the platform
controls.

So these routes authenticate with the FACTORY authenticator and then demand the
founder workspace — the same gate `/saas/tenants` uses, because onboarding a
manufacturer is the same class of act as onboarding a customer.

THE ORDER THAT MATTERS
----------------------
    1. create the organisation          POST /saas/oems
    2. provision its first admin        POST /saas/oems/{id}/admin   -> temp password
    3. hand the credentials over        (out of band, once)
    4. the OEM signs in and rotates     POST /oem/login, /oem/change-password
    5. the OEM creates its own users    POST /oem/users

Step 5 is deliberately the OEM's own job. A platform operator who provisions
every one of a manufacturer's engineers becomes a help desk, and holds passwords
it has no reason to hold.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import oem_auth
from auth import require_roles
from database import SessionLocal
from platform_routes import log_audit
from security import hash_password
from tenancy import DEFAULT_TENANT

router = APIRouter(prefix="/saas/oems", tags=["OEM Onboarding"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_founder(current_user: dict):
    """The founder workspace, and nothing else.

    Checked against the token's OWN tenant claim rather than the effective
    tenant, so a founder previewing a customer (the X-Tenant switcher) is still
    the founder — and, more importantly, a customer Admin can never become one.
    """
    if (current_user or {}).get("tenant") != DEFAULT_TENANT:
        raise HTTPException(status_code=403,
                            detail="Only the platform operator can onboard a manufacturer")


class OemOrganizationCreate(BaseModel):
    oem_code: str
    name: str
    brand_name: str | None = None
    brand_color: str | None = None
    brand_logo_url: str | None = None
    support_email: str | None = None
    support_phone: str | None = None


class OemOrganizationUpdate(BaseModel):
    name: str | None = None
    brand_name: str | None = None
    brand_color: str | None = None
    brand_logo_url: str | None = None
    support_email: str | None = None
    support_phone: str | None = None
    is_active: bool | None = None


def _as_dict(org):
    return {
        "id": org.id, "oem_code": org.oem_code, "name": org.name,
        "brand_name": org.brand_name, "brand_color": org.brand_color,
        "brand_logo_url": org.brand_logo_url,
        "support_email": org.support_email, "support_phone": org.support_phone,
        "is_active": org.is_active,
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


@router.get("")
def list_oems(db: Session = Depends(_get_db),
              current_user: dict = Depends(require_roles(["Admin"]))):
    """Every manufacturer on the platform. Founder-only, and deliberately NOT
    data-shaped-by-scope like the customer registry: a manufacturer has no
    business reading the list of manufacturers, so it is refused outright."""
    _require_founder(current_user)
    rows = db.query(models.OemOrganization).order_by(
        models.OemOrganization.id.desc()).limit(300).all()
    out = []
    for org in rows:
        entry = _as_dict(org)
        entry["users"] = (db.query(models.OemUser)
                            .filter(models.OemUser.oem_code == org.oem_code).count())
        entry["installations"] = (db.query(models.MachineInstallation)
                                    .filter(models.MachineInstallation.oem_code
                                            == org.oem_code).count())
        out.append(entry)
    return out


@router.post("")
def create_oem(payload: OemOrganizationCreate, db: Session = Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    """Register a manufacturer.

    `oem_code` is validated as a plain identifier, which is load-bearing rather
    than cosmetic: it becomes the suffix of the sentinel tenant `OEM:<code>`
    that every one of this manufacturer's requests binds. A code carrying a
    colon or whitespace would produce a sentinel whose shape nobody reasoned
    about, and the whole isolation rests on that string being unmatchable.
    """
    _require_founder(current_user)
    code = (payload.oem_code or "").strip().upper()
    try:
        oem_auth.validate_oem_code(code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if db.query(models.OemOrganization).filter(
            models.OemOrganization.oem_code == code).first():
        raise HTTPException(status_code=400, detail="That manufacturer code already exists")

    org = models.OemOrganization(
        oem_code=code, name=(payload.name or "").strip() or code,
        brand_name=payload.brand_name, brand_color=payload.brand_color,
        brand_logo_url=payload.brand_logo_url,
        support_email=payload.support_email, support_phone=payload.support_phone)
    db.add(org)
    db.commit()
    db.refresh(org)
    log_audit(db, current_user.get("sub", "?"), "oem_created", "oem_organization",
              org.id, f"oem={code}")
    db.commit()
    return _as_dict(org)


@router.patch("/{oem_id}")
def update_oem(oem_id: int, payload: OemOrganizationUpdate,
               db: Session = Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    """Branding, contact details, and the suspension switch.

    `oem_code` is absent from the update model on purpose. It is the identity
    every installation, sharing policy and audit row already points at, and
    renaming it would silently orphan all of them.
    """
    _require_founder(current_user)
    org = db.query(models.OemOrganization).filter(
        models.OemOrganization.id == oem_id).first()
    if org is None:
        raise HTTPException(status_code=404, detail="Manufacturer not found")

    before_active = org.is_active
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value)
    db.commit()
    db.refresh(org)

    if payload.is_active is not None and payload.is_active != before_active:
        # Suspension is the one change with an immediate security effect —
        # oem_auth.resolve re-reads the organisation on every request, so a
        # suspended manufacturer stops working on its next call rather than at
        # token expiry. Worth its own audit line.
        log_audit(db, current_user.get("sub", "?"),
                  "oem_reactivated" if org.is_active else "oem_suspended",
                  "oem_organization", org.id, f"oem={org.oem_code}")
    else:
        log_audit(db, current_user.get("sub", "?"), "oem_updated",
                  "oem_organization", org.id, f"oem={org.oem_code}")
    db.commit()
    return _as_dict(org)


@router.post("/{oem_id}/admin")
def provision_oem_admin(oem_id: int, db: Session = Depends(_get_db),
                        current_user: dict = Depends(require_roles(["Admin"]))):
    """Provision the manufacturer's FIRST administrator.

    The password is generated here, returned exactly once, and stored only as a
    bcrypt hash — the same contract as `/saas/tenants/{id}/admin`. Nobody types a
    password into this system on somebody else's behalf.

    Deliberately refuses if an admin already exists. This is a bootstrap, not a
    user-management route: once the manufacturer has an administrator, further
    accounts are ITS job (`POST /oem/users`), and a platform operator quietly
    minting extra logins into a customer's supplier is exactly the kind of
    access nobody audits until it matters.
    """
    _require_founder(current_user)
    org = db.query(models.OemOrganization).filter(
        models.OemOrganization.id == oem_id).first()
    if org is None:
        raise HTTPException(status_code=404, detail="Manufacturer not found")

    existing = (db.query(models.OemUser)
                  .filter(models.OemUser.oem_code == org.oem_code,
                          models.OemUser.role == oem_auth.OEM_ADMIN).first())
    if existing is not None:
        raise HTTPException(
            status_code=400,
            detail=f"{org.oem_code} already has an administrator ({existing.username}). "
                   "Further accounts are created from the manufacturer's own portal.")

    username = f"{org.oem_code.lower()}_admin"
    try:
        oem_auth.assert_username_available(db, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    temp_password = secrets.token_urlsafe(9)
    user = models.OemUser(oem_code=org.oem_code, username=username,
                          password=hash_password(temp_password),
                          role=oem_auth.OEM_ADMIN, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    log_audit(db, current_user.get("sub", "?"), "oem_admin_provisioned", "oem_user",
              user.id, f"oem={org.oem_code} username={username}")
    db.commit()
    return {
        "username": username,
        "temporary_password": temp_password,
        "oem_code": org.oem_code,
        "role": oem_auth.OEM_ADMIN,
        "note": "Shown once. Share securely; the manufacturer should change it "
                "at POST /oem/change-password after first sign-in.",
    }


def register(app):
    app.include_router(router)
