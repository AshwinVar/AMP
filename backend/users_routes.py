"""User-management routes — admin CRUD over workspace employees.

Admin-only management of the users in a tenant workspace: add employee, list,
change role, delete, and admin password reset. All endpoints are gated by
require_roles(["Admin"]); new users are stamped with the request's tenant
(request_tenant) and mutations are audit-logged (log_audit). Peeled out of
main.py per ADR-0009.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
from auth import require_roles
from database import SessionLocal
from platform_routes import log_audit
from security import hash_password
from tenancy import request_tenant

# Roles a workspace user may hold (owned here — only user management validates it).
VALID_ROLES = ["Admin", "Supervisor", "Operator"]


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter()


@router.post("/users", response_model=schemas.UserResponse)
def create_employee(
    user: schemas.UserCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Admin adds an employee into the current workspace's tenant. For a tenant
    Admin that's always their own company; for the founder it follows the
    company switcher — switch to a tenant, then add that tenant's users."""
    if user.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")

    tenant = request_tenant(current_user)
    try:
        new_user = models.User(
            username=user.username,
            password=hash_password(user.password),
            role=user.role,
            tenant_code=tenant,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        log_audit(db, current_user.get("sub"), "create_employee", "user", new_user.id, f"{user.username} ({user.role}) in {tenant}")
        return new_user
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Database integrity error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Create employee failed: {str(e)}")


@router.get("/users", response_model=List[schemas.UserResponse])
def list_users(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    q = db.query(models.User)
    if tenant == "DEFAULT":
        q = q.filter((models.User.tenant_code == "DEFAULT") | (models.User.tenant_code.is_(None)))
    else:
        q = q.filter(models.User.tenant_code == tenant)
    return q.order_by(models.User.id.asc()).all()


@router.patch("/users/{user_id}/role", response_model=schemas.UserResponse)
def update_user_role(
    user_id: int,
    payload: schemas.UserRoleUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    user.role = payload.role
    db.commit()
    db.refresh(user)

    return user


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    if user.username == current_user.get("sub"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    deleted_name = user.username
    db.delete(user)
    db.commit()
    log_audit(db, current_user.get("sub"), "delete_user", "user", user_id, deleted_name)

    return {"message": "User deleted successfully"}


@router.patch("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    payload: dict,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Admin resets an employee's password (within their own company)."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    _same_tenant_or_403(user, current_user)

    new_password = (payload.get("password") or "").strip()
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    user.password = hash_password(new_password)
    db.commit()
    return {"message": "Password reset successfully"}
