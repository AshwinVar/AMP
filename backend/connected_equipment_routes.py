"""The FACTORY's view of its connected equipment (ADR-0017).

The OEM portal shows a manufacturer its fleet. This is the other side: what a
factory administrator sees about the manufacturers who have equipment on their
shop floor, and — the part that matters — what those manufacturers can see back.

A consent control nobody can read is not consent. So this surface answers three
questions a factory administrator is entitled to ask:

    which machines here came from an OEM, and who made them
    what is that OEM allowed to see about them
    how do I change or withdraw that

Every change is audited, because "who agreed to share our operating hours, and
when" is a question that gets asked after something goes wrong, not before.

These routes are FACTORY routes: they authenticate with `get_current_user` like
every other factory endpoint, and an OEM token is rejected there (ADR-0017). A
manufacturer cannot read — let alone widen — its own permissions.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import oem_service
import oem_sharing
from auth import get_current_user, require_roles
from database import SessionLocal
from platform_routes import log_audit
from tenancy import request_tenant

router = APIRouter(prefix="/connected-equipment", tags=["Connected Equipment"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SharingUpdate(BaseModel):
    oem_code: str
    grants: list[str] = []


@router.get("")
def connected_equipment(db: Session = Depends(_get_db),
                        current_user: dict = Depends(get_current_user)):
    """Machines in THIS factory that an OEM supplied, and what each OEM can see.

    Filtered by `factory_tenant_code` explicitly. MachineInstallation is outside
    SCOPED_MODELS by design (the OEM sentinel would otherwise hide an OEM's whole
    fleet), so this filter IS the boundary rather than a convenience.
    """
    tenant = request_tenant(current_user)
    rows = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.factory_tenant_code == tenant)
              .order_by(models.MachineInstallation.id.asc()).all())

    model_ids = {r.model_id for r in rows}
    catalogue = {m.id: m for m in db.query(models.MachineModel)
                 .filter(models.MachineModel.id.in_(model_ids)).all()} if model_ids else {}
    oem_codes = sorted({r.oem_code for r in rows})
    orgs = {o.oem_code: o for o in db.query(models.OemOrganization)
            .filter(models.OemOrganization.oem_code.in_(oem_codes)).all()} if oem_codes else {}

    equipment = []
    for r in rows:
        model = catalogue.get(r.model_id)
        org = orgs.get(r.oem_code)
        equipment.append({
            "installation_id": r.id,
            "serial_number": r.serial_number,
            "manufacturer": r.oem_code,
            "manufacturer_name": getattr(org, "name", r.oem_code),
            "support_email": getattr(org, "support_email", None),
            "model_code": getattr(model, "model_code", None),
            "model_name": getattr(model, "name", None),
            "site": r.site or "",
            "machine_id": r.machine_id,
            "lifecycle_status": r.status,
            "commissioned_at": r.commissioned_at.isoformat() if r.commissioned_at else None,
            "warranty": oem_service.warranty_state(r),
            "service": oem_service.service_state(r, model),
            # What THIS machine's manufacturer can currently see.
            "shared_with_manufacturer": sorted(
                oem_sharing.grants_for(db, r.oem_code, tenant)),
        })

    return {
        "equipment": equipment,
        "manufacturers": [{
            "oem_code": code,
            "name": getattr(orgs.get(code), "name", code),
            "machines": sum(1 for r in rows if r.oem_code == code),
            "granted": sorted(oem_sharing.grants_for(db, code, tenant)),
        } for code in oem_codes],
        # The full vocabulary, with plain-English labels, so the factory can see
        # what it is NOT sharing as clearly as what it is.
        "available_grants": [{"key": g, "label": oem_sharing.GRANT_LABELS[g]}
                             for g in oem_sharing.ALL_GRANTS],
    }


@router.put("/sharing")
def update_sharing(payload: SharingUpdate, db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    """Grant or withdraw what one manufacturer may see. Admin only, and audited.

    THE FACTORY DECIDES. This endpoint lives on the factory side and takes the
    tenant from the FACTORY's own token; there is no OEM-side equivalent,
    because a manufacturer that could edit its own permissions has permissions
    in name only.

    Unknown grant keys are REFUSED rather than dropped: silently ignoring a key
    would let an administrator believe they had shared something they had not,
    or — worse on the next release — something they had.
    """
    tenant = request_tenant(current_user)
    oem_code = (payload.oem_code or "").strip()
    if not oem_code:
        raise HTTPException(status_code=400, detail="oem_code is required")

    unknown = [g for g in payload.grants if g not in oem_sharing.ALL_GRANTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sharing grants: {', '.join(sorted(unknown))}")

    # The OEM must actually have equipment here. Granting to a manufacturer with
    # no machines on site is either a typo or an attempt to hand data to a third
    # party who was never on the shop floor.
    has_equipment = (db.query(models.MachineInstallation)
                       .filter(models.MachineInstallation.oem_code == oem_code,
                               models.MachineInstallation.factory_tenant_code == tenant)
                       .first())
    if has_equipment is None:
        raise HTTPException(
            status_code=404,
            detail="That manufacturer has no equipment installed here")

    row = (db.query(models.OemDataSharingPolicy)
             .filter(models.OemDataSharingPolicy.oem_code == oem_code,
                     models.OemDataSharingPolicy.tenant_code == tenant).first())
    before = row.grants if row else "(no policy)"
    if row is None:
        row = models.OemDataSharingPolicy(oem_code=oem_code, tenant_code=tenant)
        db.add(row)
    row.grants = ",".join(sorted(set(payload.grants)))
    row.updated_by = current_user.get("sub") or current_user.get("username")

    log_audit(db, row.updated_by, "oem_sharing_changed", "oem_data_sharing_policy",
              row.id, f"oem={oem_code} before={before!r} after={row.grants!r}")
    db.commit()
    db.refresh(row)
    return {"oem_code": oem_code, "granted": sorted(oem_sharing.parse_grants(row.grants)),
            "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def register(app):
    app.include_router(router)
