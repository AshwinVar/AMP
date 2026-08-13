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
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import oem_claims
import oem_events
import oem_service
import oem_sharing
from auth import get_current_user, require_roles
from database import SessionLocal
from events import event_bus
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


# ── Claiming a machine ───────────────────────────────────────────────
#
# THE ONLY PLACE IN AMP THAT SETS MachineInstallation.factory_tenant_code.
#
# An OEM can register a machine and offer it (ADR-0019). It cannot attach it to
# anybody. If it could, a row would appear on this screen — presented by AMP as
# your equipment, from a supplier you have never dealt with, beside controls
# inviting you to grant it access. That is the threat this flow exists to close,
# and it is closed by making the factory the only party who can complete it.


class ClaimAcceptance(BaseModel):
    # Claiming the machine and sharing its data are separate decisions taken at
    # the same moment. An empty list is a complete, valid answer: the
    # installation is created and nothing is shared.
    grants: list[str] = []


def _claim_preview(db, claim, inst, _preview_tenant):
    """What a factory is shown BEFORE it decides. The OEM's own record only —
    who made the machine, which model, its serial, and the warranty the OEM
    recorded. Nothing about any other customer, and nothing about the OEM's
    other equipment."""
    model = (db.query(models.MachineModel)
               .filter(models.MachineModel.id == inst.model_id).first())
    org = (db.query(models.OemOrganization)
             .filter(models.OemOrganization.oem_code == claim.oem_code).first())
    return {
        "claim_id": claim.id,
        "manufacturer": claim.oem_code,
        "manufacturer_name": getattr(org, "name", claim.oem_code),
        "support_email": getattr(org, "support_email", None),
        "serial_number": inst.serial_number,
        "model_code": getattr(model, "model_code", None),
        "model_name": getattr(model, "name", None),
        "family": getattr(model, "family", None),
        "firmware_version": inst.firmware_version,
        "warranty": oem_service.warranty_state(inst),
        "expires_at": claim.expires_at.isoformat() if claim.expires_at else None,
        # The vocabulary, so the factory chooses from what exists rather than
        # being told afterwards what it agreed to.
        "available_grants": [{"key": g, "label": oem_sharing.GRANT_LABELS[g]}
                             for g in oem_sharing.ALL_GRANTS],
        # What this factory ALREADY shares with this manufacturer, if anything.
        # Shown because accepting can only widen consent: without it, somebody
        # adding a second machine would think the unticked boxes were about to
        # turn sharing off.
        "already_granted": sorted(oem_sharing.grants_for(db, claim.oem_code,
                                                         _preview_tenant)),
    }


@router.get("/claim/{code}")
def preview_claim(code: str, db: Session = Depends(_get_db),
                  current_user: dict = Depends(require_roles(["Admin"]))):
    """What is this machine? A READ — opening the link claims nothing.

    A URL is not consent. A link forwarded to the wrong person, or a QR scanned
    by somebody walking past the crate, must not attach equipment to a workspace.

    Admin-gated even though it only reads: an unauthenticated preview would be an
    oracle for testing codes at leisure.
    """
    tenant = request_tenant(current_user)
    claim = oem_claims.find_by_code(db, code)
    inst = None
    if claim is not None:
        inst = (db.query(models.MachineInstallation)
                  .filter(models.MachineInstallation.id == claim.installation_id)
                  .first())
    if not oem_claims.usable(claim, tenant, inst):
        # ONE refusal for every reason — mistyped, expired, revoked, spent, or
        # meant for somebody else. Telling them apart would tell a prober which
        # guesses were close.
        raise HTTPException(status_code=404, detail=oem_claims.REFUSAL)
    return _claim_preview(db, claim, inst, tenant)


@router.post("/claim/{code}")
def accept_claim(code: str, payload: ClaimAcceptance,
                 db: Session = Depends(_get_db),
                 current_user: dict = Depends(require_roles(["Admin"]))):
    """Accept the machine into this factory, and choose what its maker may see.

    The two conditional UPDATEs inside oem_claims.accept are what make this safe
    under concurrency: the DATABASE decides which of two simultaneous
    administrators wins, not Python.
    """
    tenant = request_tenant(current_user)
    actor = current_user.get("sub") or current_user.get("username") or "?"

    unknown = [g for g in payload.grants if g not in oem_sharing.ALL_GRANTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sharing grants: {', '.join(sorted(unknown))}")

    claim = oem_claims.find_by_code(db, code)
    inst = None
    if claim is not None:
        inst = (db.query(models.MachineInstallation)
                  .filter(models.MachineInstallation.id == claim.installation_id)
                  .first())
    if not oem_claims.usable(claim, tenant, inst):
        # A failed attempt is audited IN THE TENANT THAT MADE IT, so a factory
        # can see somebody typing bad codes at their workspace. Deliberately not
        # reported to the OEM: an OEM learning that a named factory tried a
        # specific code is a leak in the other direction.
        log_audit(db, actor, "claim_rejected", "machine_claim", None,
                  f"tenant={tenant} refused")
        db.commit()
        raise HTTPException(status_code=404, detail=oem_claims.REFUSAL)

    if not oem_claims.accept(db, claim, inst, tenant, actor):
        # Lost the race, or the machine was taken between the check and the
        # write. Same answer as every other failure.
        log_audit(db, actor, "claim_rejected", "machine_claim", claim.id,
                  f"tenant={tenant} lost the race or machine already assigned")
        db.commit()
        raise HTTPException(status_code=404, detail=oem_claims.REFUSAL)

    # Consent, through the EXISTING policy — same table, same vocabulary, same
    # withdrawal path as the sharing controls above. Default is nothing.
    #
    # ACCEPTING A MACHINE CAN ONLY WIDEN CONSENT, NEVER NARROW IT.
    #
    # The policy is keyed (oem, tenant): it is a RELATIONSHIP-level agreement,
    # not a per-machine one. The first version of this handler overwrote it, and
    # a test caught what that means in practice — a factory that already shares
    # operating hours with a supplier adds a second machine from them, leaves the
    # boxes unticked, and SILENTLY REVOKES the sharing on the first machine.
    # Nobody asked for that and nobody would notice until an engineer was sent
    # somewhere on stale data.
    #
    # So the requested grants are UNIONED with what is already agreed. Ticking a
    # box at claim time adds; nothing at claim time changes nothing. Withdrawal
    # stays where it has always been: the deliberate, audited, Admin-only control
    # under Connected Equipment.
    policy = (db.query(models.OemDataSharingPolicy)
                .filter(models.OemDataSharingPolicy.oem_code == claim.oem_code,
                        models.OemDataSharingPolicy.tenant_code == tenant).first())
    before = policy.grants if policy else "(no policy)"
    existing = oem_sharing.parse_grants(policy.grants) if policy else set()
    if policy is None:
        policy = models.OemDataSharingPolicy(oem_code=claim.oem_code,
                                             tenant_code=tenant)
        db.add(policy)
    policy.grants = ",".join(sorted(existing | set(payload.grants)))
    policy.updated_by = actor

    db.commit()
    db.refresh(inst)

    log_audit(db, actor, "claim_accepted", "machine_installation", inst.id,
              f"oem={claim.oem_code} serial={inst.serial_number} "
              f"hint={claim.code_hint} Manufactured -> Assigned tenant={tenant}")
    log_audit(db, actor, "oem_sharing_changed", "oem_data_sharing_policy",
              policy.id, f"oem={claim.oem_code} before={before!r} "
                         f"after={policy.grants!r} (at claim)")
    db.commit()

    model = (db.query(models.MachineModel)
               .filter(models.MachineModel.id == inst.model_id).first())
    oem_events.publish(event_bus, db, oem_events.MachineClaimed(
        tenant_code=tenant, oem_code=claim.oem_code, installation_id=inst.id,
        serial_number=inst.serial_number,
        model_code=getattr(model, "model_code", None),
        granted=policy.grants or ""))
    db.commit()

    return {
        "installation_id": inst.id,
        "serial_number": inst.serial_number,
        "manufacturer": claim.oem_code,
        "model_code": getattr(model, "model_code", None),
        "lifecycle_status": inst.status,
        "granted": sorted(oem_sharing.parse_grants(policy.grants)),
        "message": "Machine added to your connected equipment.",
    }


class MachineLink(BaseModel):
    """Which machine on this floor the supplied equipment actually is.

    Nullable so a mis-link can be corrected without releasing the machine back to
    the manufacturer — both ends of the pointer belong to the same factory, so
    undoing it crosses no boundary and costs the OEM nothing it was owed.
    """

    machine_id: int | None = None


@router.post("/{installation_id}/link")
def link_installation(installation_id: int, payload: MachineLink,
                      db: Session = Depends(_get_db),
                      current_user: dict = Depends(require_roles(["Admin"]))):
    """Say which machine on the shop floor this piece of equipment is.

    THE FACTORY DECIDES, NOT THE MANUFACTURER. The OEM knows the serial it built;
    only the factory knows which asset on its floor carries it. If the OEM could
    nominate a `machine_id`, it would be choosing a row in somebody else's
    workspace — an existence oracle at best, and at worst an OEM grafting its
    installation onto a machine it never made, inheriting that machine's status
    and utilisation as if it were its own telemetry.

    WHY THIS ROUTE HAD TO EXIST. Commissioning requires the serial to be linked
    to a machine in the factory (ADR-0019); until this endpoint, nothing but a
    direct database write could satisfy that check, so the last step of the
    journey was reachable only by a developer. That is exactly the class of gap
    this campaign exists to close.

    BOTH ROWS MUST BE THIS TENANT'S, and both are filtered on `tenant` in the
    query rather than fetched and then compared, so another factory's machine is
    indistinguishable from one that does not exist. The reply is 404 either way.

    ONE MACHINE, ONE INSTALLATION. Two installations pointing at the same machine
    would make two manufacturers read the same shop-floor status as telemetry
    from their own equipment, and the fleet count double.

    The site is copied from the machine rather than typed again: the factory has
    already recorded where that machine stands, and a second hand-entered copy is
    a second thing to get wrong.
    """
    tenant = request_tenant(current_user)
    actor = current_user.get("sub") or current_user.get("username") or "?"

    inst = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.id == installation_id,
                      models.MachineInstallation.factory_tenant_code == tenant)
              .first())
    if inst is None:
        raise HTTPException(status_code=404, detail="Equipment not found")

    machine = None
    if payload.machine_id is not None:
        machine = (db.query(models.Machine)
                     .filter(models.Machine.id == payload.machine_id,
                             models.Machine.tenant_code == tenant)
                     .first())
        if machine is None:
            raise HTTPException(status_code=404, detail="Machine not found")

        taken = (db.query(models.MachineInstallation)
                   .filter(models.MachineInstallation.machine_id == machine.id,
                           models.MachineInstallation.factory_tenant_code == tenant,
                           models.MachineInstallation.id != inst.id)
                   .first())
        if taken is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"{machine.name} is already linked to serial "
                        f"{taken.serial_number}. Unlink that one first."))

    before = inst.machine_id
    inst.machine_id = payload.machine_id
    if machine is not None:
        inst.site = machine.site or inst.site
    inst.updated_at = datetime.utcnow()

    log_audit(db, actor, "installation_linked", "machine_installation", inst.id,
              f"oem={inst.oem_code} serial={inst.serial_number} "
              f"machine {before} -> {payload.machine_id}")
    db.commit()
    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "machine_id": inst.machine_id,
            "machine_name": getattr(machine, "name", None),
            "site": inst.site,
            "message": ("Linked. Its manufacturer can now commission it."
                        if machine is not None else
                        "Unlinked from the shop-floor machine.")}


@router.post("/{installation_id}/release")
def release_installation(installation_id: int, db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(["Admin"]))):
    """Let a machine go — it is leaving this site.

    THE ONLY PATH BY WHICH A MACHINE MOVES FACTORIES. There is no reassignment
    route: the machine returns to the manufacturer's unassigned stock, the OEM
    issues a fresh invitation, and the next factory accepts it explicitly. A
    machine cannot be moved between customers behind either one's back.

    HISTORY SURVIVES. The installation row carries CURRENT state; what happened
    here is in event_log and audit_log, filed under this tenant (ADR-0002). This
    factory keeps its record of the commissioning and every service performed on
    site, and the next factory's log starts at its own acceptance.

    The sharing policy is deliberately left alone. It is keyed (oem, tenant), it
    now matches no installation here, and so it grants nothing — and if the same
    manufacturer's equipment ever returns, the factory's earlier decision is
    still on the record rather than silently reset to "nothing" or, worse,
    re-applied without being asked.
    """
    tenant = request_tenant(current_user)
    actor = current_user.get("sub") or current_user.get("username") or "?"

    inst = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.id == installation_id,
                      models.MachineInstallation.factory_tenant_code == tenant)
              .first())
    if inst is None:
        raise HTTPException(status_code=404, detail="Equipment not found")

    before = inst.status
    released = (db.query(models.MachineInstallation)
                  .filter(models.MachineInstallation.id == inst.id,
                          models.MachineInstallation.factory_tenant_code == tenant)
                  .update({"factory_tenant_code": None, "machine_id": None,
                           "site": "", "status": "Sold",
                           "decommissioned_at": datetime.utcnow(),
                           "updated_at": datetime.utcnow()},
                          synchronize_session=False))
    if released != 1:
        raise HTTPException(status_code=404, detail="Equipment not found")

    log_audit(db, actor, "installation_released", "machine_installation", inst.id,
              f"oem={inst.oem_code} serial={inst.serial_number} "
              f"{before} -> Sold, tenant={tenant} -> none")
    db.commit()
    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "manufacturer": inst.oem_code, "lifecycle_status": "Sold",
            "message": ("Released. The manufacturer can issue a new installation "
                        "code for its next site; your service history stays here.")}


def register(app):
    app.include_router(router)
