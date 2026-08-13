"""The OEM portal API (ADR-0017).

Every route here is authenticated as an OEM principal, which means the request
already carries a SENTINEL factory tenant — so a bug in one of these handlers
cannot reach a customer's operational data even if the handler forgets to
filter. That is the point of the sentinel: this file is not the security
boundary, it is the product on top of one.

What each handler DOES owe:
  * `oem_code` from the PRINCIPAL, never from a path, query or body parameter;
  * a 404 (never a 403) for a row belonging to another manufacturer, so probing
    ids cannot confirm that a competitor's machine exists;
  * the sharing policy applied per field, read fresh (oem_sharing).
"""
import os
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import oem_auth
import oem_claims
import oem_events
import oem_service
import oem_sharing
import oem_telemetry
from auth import create_access_token
from database import SessionLocal
from events import event_bus
from platform_routes import log_audit
from security import hash_password, verify_password

router = APIRouter(prefix="/oem", tags=["OEM"])

MAX_PAGE = 200

# Where a scanned QR sends somebody. Configurable because the claim sticker is
# printed in the physical world and has to point at the deployment the customer
# actually uses — a hard-coded localhost on a label is unrecoverable.
CLAIM_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.marx8.com").rstrip("/")


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _models_by_id(db, oem_code):
    rows = (db.query(models.MachineModel)
              .filter(models.MachineModel.oem_code == oem_code).all())
    return {r.id: r for r in rows}


@router.get("/me")
def whoami(db: Session = Depends(_get_db),
           principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """Who am I, and what does my portal look like.

    The branding is the white-label configuration (ADR-0017): one frontend
    build, per-OEM appearance from configuration, never a per-OEM fork.
    """
    org = (db.query(models.OemOrganization)
             .filter(models.OemOrganization.oem_code == principal["oem"]).first())
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return {
        "oem_code": org.oem_code,
        "username": principal["username"],
        "role": principal["role"],
        "capabilities": sorted(oem_auth.ROLE_CAPABILITIES.get(principal["role"], set())),
        "branding": {
            "name": org.brand_name or org.name,
            "color": org.brand_color,
            "logo_url": org.brand_logo_url,
            "support_email": org.support_email,
            "support_phone": org.support_phone,
        },
    }


@router.get("/models")
def list_models(db: Session = Depends(_get_db),
                principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """This manufacturer's catalogue. Filtered by the principal's oem_code, which
    is why a competitor's model is not merely hidden — it is not selected."""
    rows = (db.query(models.MachineModel)
              .filter(models.MachineModel.oem_code == principal["oem"])
              .order_by(models.MachineModel.model_code.asc()).all())
    return [{
        "id": r.id, "family": r.family, "model_code": r.model_code, "name": r.name,
        "description": r.description, "rated_capacity": r.rated_capacity,
        "rated_capacity_unit": r.rated_capacity_unit,
        "service_interval_hours": r.service_interval_hours,
        "warranty_months": r.warranty_months, "status": r.status,
        "documentation_url": r.documentation_url,
    } for r in rows]


@router.get("/fleet")
def fleet(customer: str = Query(None, description="Filter to one customer"),
          limit: int = Query(100, ge=1, le=MAX_PAGE),
          offset: int = Query(0, ge=0),
          db: Session = Depends(_get_db),
          principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """The installed fleet, as its manufacturer may see it.

    `customer` NARROWS the result; it can never widen it. The oem_code filter is
    applied first and unconditionally, so passing another manufacturer's
    customer simply returns an empty page rather than that manufacturer's fleet.
    """
    rows = oem_sharing.installations_for(db, principal["oem"], tenant_code=customer)
    total = len(rows)
    page = rows[offset:offset + limit]
    catalogue = _models_by_id(db, principal["oem"])

    # Grants are read once per CUSTOMER, not once per machine: a fleet of a
    # thousand machines at four sites is four policy reads, not a thousand.
    grants_cache = {}
    out = []
    for inst in page:
        tenant = inst.factory_tenant_code
        if tenant not in grants_cache:
            grants_cache[tenant] = oem_sharing.grants_for(db, principal["oem"], tenant)
        out.append(oem_sharing.fleet_row(db, inst, grants_cache[tenant],
                                         catalogue.get(inst.model_id)))
    return {"total": total, "limit": limit, "offset": offset, "machines": out}


@router.get("/customers")
def customers(db: Session = Depends(_get_db),
              principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """The customers this manufacturer has machines at — and nothing else about
    them. A customer appears here because the OEM sold them equipment, which is
    the OEM's own record; it does not imply access to that customer's AMP."""
    rows = oem_sharing.installations_for(db, principal["oem"])
    by_customer = {}
    for inst in rows:
        key = (inst.factory_tenant_code or "(unassigned)", inst.site or "")
        entry = by_customer.setdefault(key, {"customer": key[0], "site": key[1],
                                             "machines": 0, "active": 0})
        entry["machines"] += 1
        if inst.status == "Active":
            entry["active"] += 1
    out = sorted(by_customer.values(), key=lambda e: (e["customer"], e["site"]))
    for entry in out:
        entry["shared"] = sorted(
            oem_sharing.grants_for(db, principal["oem"], entry["customer"]))
    return {"customers": out}


@router.get("/machines/{installation_id}")
def machine_detail(installation_id: int, db: Session = Depends(_get_db),
                   principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """One machine's equipment-lifecycle view.

    A 404 for another manufacturer's installation — deliberately NOT a 403. A
    403 confirms the row exists, which lets a competitor enumerate a fleet by
    probing ids and reading the difference between the two answers.
    """
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    catalogue = _models_by_id(db, principal["oem"])
    grants = oem_sharing.grants_for(db, principal["oem"], inst.factory_tenant_code)
    row = oem_sharing.fleet_row(db, inst, grants, catalogue.get(inst.model_id))
    model = catalogue.get(inst.model_id)
    row["identity"] = {
        "manufacturer": principal["oem"],
        "model_code": getattr(model, "model_code", None),
        "family": getattr(model, "family", None),
        "serial_number": inst.serial_number,
        "firmware_version": inst.firmware_version,
    }
    # What the customer has NOT shared is stated, not silently omitted: an
    # engineer who cannot tell "no data" from "not shared" will read a blank as
    # a broken machine and send somebody to site.
    row["not_shared"] = sorted(set(oem_sharing.ALL_GRANTS) - set(grants))
    return row


@router.get("/sharing")
def sharing_summary(db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """What each of this manufacturer's customers has agreed to share.

    Visible to the OEM on purpose: a manufacturer that can see it was granted
    nothing will ask the customer, rather than concluding the product is broken.
    """
    rows = oem_sharing.installations_for(db, principal["oem"])
    tenants = sorted({r.factory_tenant_code for r in rows if r.factory_tenant_code})
    return {"policies": [{
        "customer": t,
        "granted": sorted(oem_sharing.grants_for(db, principal["oem"], t)),
        "available": list(oem_sharing.ALL_GRANTS),
    } for t in tenants],
        "grant_labels": oem_sharing.GRANT_LABELS}


@router.get("/service")
def service_queue(db: Session = Depends(_get_db),
                  principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """The service queue: every recommendation across this manufacturer's fleet.

    Built from the installations `oem_sharing` returns, so the oem_code filter
    lives in ONE place rather than being re-implemented here — a second copy of a
    security filter is a second chance to get it subtly wrong.

    Service position comes from the OEM's OWN records (serial, hours it reported,
    the interval on its own model), so it needs no grant from the factory. What a
    grant controls is the FACTORY's data — status, utilisation — which is why a
    recommendation never quotes them.
    """
    rows = oem_sharing.installations_for(db, principal["oem"])
    catalogue = _models_by_id(db, principal["oem"])
    recs = oem_service.fleet_recommendations(db, principal["oem"], rows, catalogue)
    by_severity = {}
    for r in recs:
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1
    return {"total": len(recs), "by_severity": by_severity,
            "recommendations": recs}


@router.get("/machines/{installation_id}/service")
def machine_service(installation_id: int, db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """Service, warranty and commissioning position for one machine."""
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    model = _models_by_id(db, principal["oem"]).get(inst.model_id)
    try:
        profile = oem_telemetry.parse(getattr(model, "telemetry_profile", None))
        profile_error = None
    except oem_telemetry.ProfileError as e:
        # A broken profile is REPORTED, not swallowed into "no signals": a
        # corrupted configuration otherwise looks like a machine that reports
        # nothing, and somebody spends a day chasing a healthy gateway.
        profile, profile_error = [], str(e)
    return {
        "installation_id": inst.id,
        "serial_number": inst.serial_number,
        "lifecycle_status": inst.status,
        "service": oem_service.service_state(inst, model),
        "warranty": oem_service.warranty_state(inst),
        "commissioning": oem_service.commissioning_report(inst, model, profile),
        "telemetry_signals": [s["name"] for s in profile],
        "telemetry_profile_error": profile_error,
        "recommendations": oem_service.recommendations(inst, model),
    }


@router.get("/models/{model_id}/telemetry")
def model_telemetry(model_id: int, db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """The telemetry profile for one of this manufacturer's models.

    Filtered by oem_code, so a competitor's model id is a 404 for the same reason
    a competitor's installation id is: a 403 would confirm it exists.
    """
    model = (db.query(models.MachineModel)
               .filter(models.MachineModel.oem_code == principal["oem"],
                       models.MachineModel.id == model_id).first())
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    try:
        return {"model_id": model.id, "model_code": model.model_code,
                "signals": oem_telemetry.parse(model.telemetry_profile),
                "error": None}
    except oem_telemetry.ProfileError as e:
        return {"model_id": model.id, "model_code": model.model_code,
                "signals": [], "error": str(e)}


# ── Lifecycle WRITES ─────────────────────────────────────────────────
#
# Everything above this line is a read. These two are the first OEM writes, and
# they are deliberately the only ones: commissioning a machine and recording a
# service are the two things a manufacturer genuinely does to equipment it
# supplied, and each is confined to the manufacturer's OWN columns on the
# installation row.
#
# WHAT THEY CANNOT DO, ON PURPOSE:
#   * assign a machine to a customer. An OEM that could set
#     `factory_tenant_code` could plant an installation at any factory in the
#     system — which would put a row on that factory's Connected Equipment
#     screen and invite them to grant sharing to a supplier they never bought
#     from. Assignment stays a separate, unbuilt operation rather than a
#     parameter somebody forgets to guard.
#   * touch anything the FACTORY owns. No machine status, no utilisation, no
#     production. The installation row is the whole surface.


class TransitionRequest(BaseModel):
    target: str


class ServiceRecord(BaseModel):
    # The hours reading the service was carried out at. Optional: a service desk
    # that does not know it should not be forced to invent one, and NULL keeps
    # meaning "no service recorded" rather than "serviced at zero hours".
    service_hours: float | None = None


@router.post("/machines/{installation_id}/transition")
def transition_machine(installation_id: int, payload: TransitionRequest,
                       db: Session = Depends(_get_db),
                       principal: dict = Depends(
                           oem_auth.require_oem("manage_installations"))):
    """Move one installation along the equipment lifecycle.

    404 for another manufacturer's row, like every read here — a 403 would
    confirm it exists and turn id probing into fleet enumeration.
    """
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    before = inst.status
    try:
        oem_service.transition(inst, payload.target)
    except oem_service.LifecycleError as e:
        # The state machine's own words. "cannot go from Manufactured to Active;
        # allowed: Sold, Decommissioned" tells a service desk what to do next;
        # "invalid transition" does not.
        raise HTTPException(status_code=400, detail=str(e))

    db.commit()
    db.refresh(inst)

    if inst.status == "Installed" and before != "Installed":
        model = _models_by_id(db, principal["oem"]).get(inst.model_id)
        oem_events.publish(event_bus, db, oem_events.MachineInstalled(
            tenant_code=inst.factory_tenant_code or "",
            oem_code=principal["oem"], installation_id=inst.id,
            serial_number=inst.serial_number, site=inst.site or "",
            model_code=getattr(model, "model_code", None)))
        db.commit()

    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "from": before, "to": inst.status,
            "allowed_next": list(oem_service.LIFECYCLE.get(inst.status, ()))}


@router.post("/machines/{installation_id}/commission")
def commission_machine(installation_id: int, db: Session = Depends(_get_db),
                       principal: dict = Depends(
                           oem_auth.require_oem("commission"))):
    """Commission a machine, and record whether the checks actually passed.

    The commissioning report is ADVICE, not a gate (oem_service). AMP does not
    refuse to commission a machine somebody has a reason to put into service
    with a check outstanding — it records which happened, raises the customer's
    notification as a warning when checks failed, and puts `checks_passed` on
    the event so the history cannot later imply it was clean.
    """
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    model = _models_by_id(db, principal["oem"]).get(inst.model_id)
    try:
        profile = oem_telemetry.parse(getattr(model, "telemetry_profile", None))
    except oem_telemetry.ProfileError:
        profile = []
    report = oem_service.commissioning_report(inst, model, profile)

    before = inst.status
    try:
        if inst.status != "Commissioning":
            oem_service.transition(inst, "Commissioning")
        oem_service.transition(inst, "Active")
    except oem_service.LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    db.commit()
    db.refresh(inst)

    oem_events.publish(event_bus, db, oem_events.MachineCommissioned(
        tenant_code=inst.factory_tenant_code or "",
        oem_code=principal["oem"], installation_id=inst.id,
        serial_number=inst.serial_number, site=inst.site or "",
        model_code=getattr(model, "model_code", None),
        checks_passed=bool(report["ready"])))
    db.commit()

    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "from": before, "to": inst.status, "commissioning": report}


@router.post("/machines/{installation_id}/service")
def record_service(installation_id: int, payload: ServiceRecord,
                   db: Session = Depends(_get_db),
                   principal: dict = Depends(
                       oem_auth.require_oem("manage_service"))):
    """Record a completed service, which is what makes `overdue` mean anything.

    Before migration 0007 service position was `operating_hours % interval`,
    which assumes every service happened on schedule and made `overdue`
    unreachable. This endpoint writes the number that assumption was standing in
    for.
    """
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    hours = payload.service_hours
    if hours is None:
        # Fall back to what the machine last reported. Still honest: it is the
        # OEM's own hours counter, not an invented figure.
        hours = inst.operating_hours
    if hours is not None and hours < 0:
        raise HTTPException(status_code=400,
                            detail="service_hours cannot be negative")

    inst.last_service_hours = hours
    inst.last_service_at = datetime.utcnow()
    db.commit()
    db.refresh(inst)

    oem_events.publish(event_bus, db, oem_events.ServiceCompleted(
        tenant_code=inst.factory_tenant_code or "",
        oem_code=principal["oem"], installation_id=inst.id,
        serial_number=inst.serial_number, site=inst.site or "",
        service_hours=hours))
    db.commit()

    model = _models_by_id(db, principal["oem"]).get(inst.model_id)
    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "last_service_hours": inst.last_service_hours,
            "last_service_at": inst.last_service_at.isoformat()
            if inst.last_service_at else None,
            "service": oem_service.service_state(inst, model)}


# ── Registering a machine, and offering it to a customer ─────────────
#
# An OEM may register what it built and OFFER it. It may not attach it to
# anybody (ADR-0019). The acceptance lives on the factory side, in
# connected_equipment_routes, and is the only thing in AMP that sets
# `MachineInstallation.factory_tenant_code`.


class MachineRegistration(BaseModel):
    serial_number: str
    model_id: int
    firmware_version: str | None = None
    warranty_start: date | None = None
    warranty_end: date | None = None
    notes: str | None = None


class ClaimRequest(BaseModel):
    expires_in_days: int | None = None
    # A HINT the OEM may set when it knows who it shipped to. Enforced when
    # present, never a substitute for acceptance, and a claim without one is
    # equally valid (ADR-0019).
    intended_customer: str | None = None


def _claim_dict(claim, installation=None, model=None):
    return {
        "id": claim.id,
        "serial_number": getattr(installation, "serial_number", None),
        "model_code": getattr(model, "model_code", None),
        "status": oem_claims.public_state(claim),
        "code_hint": claim.code_hint,
        "intended_customer": claim.intended_tenant_code,
        "expires_at": claim.expires_at.isoformat() if claim.expires_at else None,
        "created_at": claim.created_at.isoformat() if claim.created_at else None,
        "created_by": claim.created_by,
        "claimed_at": claim.claimed_at.isoformat() if claim.claimed_at else None,
        # WHO accepted it — the OEM's own customer, so this is its record, not a
        # leak. It learns nothing about any factory it has not shipped to.
        "claimed_by_customer": claim.claimed_tenant_code,
    }


@router.post("/machines")
def register_machine(payload: MachineRegistration, db: Session = Depends(_get_db),
                     principal: dict = Depends(
                         oem_auth.require_oem("manage_installations"))):
    """Register a machine this manufacturer has built.

    No new entity: an unassigned `MachineInstallation` IS a manufactured machine
    — `factory_tenant_code` is nullable and the lifecycle starts at
    `Manufactured`. A second "stock" table would duplicate the serial namespace
    and the lifecycle for nothing.

    The serial is unique PER OEM, not globally: a global namespace would let one
    manufacturer discover another's serials by probing for collisions.
    """
    serial = (payload.serial_number or "").strip()
    if not serial:
        raise HTTPException(status_code=400, detail="A serial number is required")

    model = (db.query(models.MachineModel)
               .filter(models.MachineModel.oem_code == principal["oem"],
                       models.MachineModel.id == payload.model_id).first())
    # 404 for a competitor's model id, for the same reason a competitor's
    # machine id is: a 403 confirms it exists.
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    clash = (db.query(models.MachineInstallation)
               .filter(models.MachineInstallation.oem_code == principal["oem"],
                       models.MachineInstallation.serial_number == serial).first())
    if clash is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Serial {serial} is already registered to this manufacturer")

    inst = models.MachineInstallation(
        oem_code=principal["oem"], serial_number=serial, model_id=model.id,
        factory_tenant_code=None, site="", status="Manufactured",
        firmware_version=payload.firmware_version,
        warranty_start=payload.warranty_start, warranty_end=payload.warranty_end,
        notes=payload.notes)
    db.add(inst)
    db.commit()
    db.refresh(inst)
    log_audit(db, principal["username"], "machine_registered", "machine_installation",
              inst.id, f"oem={principal['oem']} serial={serial} "
                       f"model={model.model_code}")
    db.commit()
    return {"installation_id": inst.id, "serial_number": inst.serial_number,
            "model_code": model.model_code, "status": inst.status,
            "factory": None}


@router.post("/machines/{installation_id}/claim")
def create_claim(installation_id: int, payload: ClaimRequest,
                 db: Session = Depends(_get_db),
                 principal: dict = Depends(
                     oem_auth.require_oem("manage_installations"))):
    """Prepare a machine for installation: issue a claim code.

    THIS DOES NOT ATTACH THE MACHINE TO ANYONE. It creates an offer. The code
    travels with the machine — printed, stuck on, or handed over — and a factory
    Admin turns it into an installation by accepting it.

    The raw code is in this response and nowhere else, ever again: only its
    SHA-256 is stored.
    """
    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        claim, code = oem_claims.create(
            db, principal["oem"], inst, principal["username"],
            ttl_days=payload.expires_in_days,
            intended_tenant_code=(payload.intended_customer or "").strip() or None)
    except oem_claims.ClaimError as e:
        raise HTTPException(status_code=400, detail=str(e))

    db.commit()
    db.refresh(claim)
    # The audit records the HINT, never the code. Four characters are enough to
    # match a row to a sticker on a support call and useless as a credential.
    log_audit(db, principal["username"], "claim_created", "machine_claim", claim.id,
              f"oem={principal['oem']} serial={inst.serial_number} "
              f"hint={claim.code_hint} expires={claim.expires_at.isoformat()}")
    db.commit()

    model = _models_by_id(db, principal["oem"]).get(inst.model_id)
    out = _claim_dict(claim, inst, model)
    out["claim_code"] = code
    # The QR carries the claim URL and the code, and nothing else — no tenant, no
    # OEM code, no database id, no machine detail (ADR-0019).
    out["claim_url"] = f"{CLAIM_BASE_URL}/claim/{code}"
    out["note"] = ("Shown once. Print it with the machine or hand it to the "
                   "customer; AMP stores only a hash and cannot show it again.")
    return out


@router.get("/claims")
def list_claims(status: str = Query(None, description="Filter by claim status"),
                limit: int = Query(100, ge=1, le=MAX_PAGE),
                offset: int = Query(0, ge=0),
                db: Session = Depends(_get_db),
                principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """This manufacturer's invitations. Filtered by the principal's oem_code, so
    a competitor's claims are not merely hidden — they are not selected."""
    q = (db.query(models.MachineClaim)
           .filter(models.MachineClaim.oem_code == principal["oem"])
           .order_by(models.MachineClaim.id.desc()))
    rows = q.all()
    # `Pending` becomes `Expired` once the deadline passes, so the filter has to
    # run on the DERIVED state or a stale row would be listed as live.
    if status:
        rows = [c for c in rows if oem_claims.public_state(c) == status]
    total = len(rows)
    page = rows[offset:offset + limit]

    insts = {}
    if page:
        ids = {c.installation_id for c in page}
        insts = {i.id: i for i in db.query(models.MachineInstallation)
                 .filter(models.MachineInstallation.oem_code == principal["oem"],
                         models.MachineInstallation.id.in_(ids)).all()}
    catalogue = _models_by_id(db, principal["oem"])
    return {"total": total, "limit": limit, "offset": offset,
            "claims": [_claim_dict(c, insts.get(c.installation_id),
                                   catalogue.get(getattr(insts.get(c.installation_id),
                                                         "model_id", None)))
                       for c in page]}


@router.post("/claims/{claim_id}/revoke")
def revoke_claim(claim_id: int, db: Session = Depends(_get_db),
                 principal: dict = Depends(
                     oem_auth.require_oem("manage_installations"))):
    """Withdraw an unused invitation — a shipment changed, a sticker went astray.

    Revoking is not deleting. The row stays so the record of what was issued
    survives, and the machine can be offered again with a fresh code.
    """
    claim = (db.query(models.MachineClaim)
               .filter(models.MachineClaim.oem_code == principal["oem"],
                       models.MachineClaim.id == claim_id).first())
    # 404 for a competitor's claim id, indistinguishable from one that does not
    # exist.
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    if not oem_claims.revoke(db, claim, principal["username"]):
        raise HTTPException(
            status_code=400,
            detail=f"That invitation is already {oem_claims.public_state(claim).lower()} "
                   "and cannot be withdrawn.")
    db.commit()
    db.refresh(claim)
    log_audit(db, principal["username"], "claim_revoked", "machine_claim", claim.id,
              f"oem={principal['oem']} hint={claim.code_hint} Pending -> Revoked")
    db.commit()
    return _claim_dict(claim)


@router.get("/notifications")
def oem_notifications(db: Session = Depends(_get_db),
                      principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """What has happened to this manufacturer's fleet that it should know about.

    The EXISTING notification table, scoped by the EXISTING mechanism: a row
    stamped with the OEM sentinel `OEM:<code>` is visible to this manufacturer's
    sessions and to no factory, because Notification is in SCOPED_MODELS and an
    OEM request binds that sentinel.

    This corrects a claim in oem_subscribers.py that an OEM-side notification
    store did not exist. It did — the sentinel made it work all along.
    """
    rows = (db.query(models.Notification)
              .order_by(models.Notification.id.desc()).limit(100).all())
    return {"notifications": [{
        "id": n.id, "type": n.notification_type, "severity": n.severity,
        "title": n.title, "message": n.message, "status": n.status,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n in rows]}


# ── Signing in, and who may sign in ──────────────────────────────────
#
# Until this block existed, an OEM could not authenticate AT ALL. `OemUser` rows
# were only ever READ, by oem_auth.resolve; every test minted its own JWT by
# hand. The security spine was complete and the front door was missing.
#
# `/oem/login` is the ONE route in this file with no `require_oem` dependency,
# which is exactly the kind of exception that gets copied by accident — so
# test_oem_provisioning asserts that every other /oem route has one.


class OemLogin(BaseModel):
    username: str
    password: str


class OemUserCreate(BaseModel):
    username: str
    role: str


class OemUserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class OemPasswordChange(BaseModel):
    current_password: str
    new_password: str


def _oem_user_dict(user):
    return {"id": user.id, "username": user.username, "role": user.role,
            "is_active": user.is_active,
            "capabilities": sorted(oem_auth.ROLE_CAPABILITIES.get(user.role, set())),
            "created_at": user.created_at.isoformat() if user.created_at else None}


@router.post("/login")
def oem_login(payload: OemLogin, db: Session = Depends(_get_db)):
    """Sign in as a manufacturer.

    Separate from `/login` because the principals live in separate tables and
    must stay that way (ADR-0017): this route cannot return a factory session
    however it is called, and `/login` cannot return an OEM one.

    THE REFUSALS ARE DELIBERATELY UNEVEN. Bad credentials give one
    indistinguishable answer, because telling an attacker which half was wrong
    hands them a username oracle. Everything AFTER a correct password names the
    real reason — a suspended engineer who is told "invalid credentials" will
    spend the afternoon resetting a password that was never the problem.
    """
    username = (payload.username or "").strip()
    user = (db.query(models.OemUser)
              .filter(models.OemUser.username == username).first())
    if user is None or not verify_password(payload.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been disabled")
    if user.role not in oem_auth.OEM_ROLES:
        raise HTTPException(status_code=403, detail="This account has no OEM role")

    org = (db.query(models.OemOrganization)
             .filter(models.OemOrganization.oem_code == user.oem_code).first())
    if org is None:
        raise HTTPException(status_code=403, detail="This organisation no longer exists")
    if not org.is_active:
        raise HTTPException(status_code=403, detail="This organisation has been suspended")

    # NO `tenant` CLAIM. tenancy.effective_tenant has a terminal OEM branch that
    # binds the sentinel and ignores any tenant claim, so one here would be inert
    # — but an inert claim that looks authoritative is how somebody later "fixes"
    # the branch to honour it.
    token = create_access_token({"sub": user.username, "role": user.role,
                                 "principal": "oem", "oem": user.oem_code})
    # Audited without a bound tenant, so the row's tenant_code stays NULL and is
    # invisible to every tenant's read (models.AuditLog) — a platform event, not
    # a customer's.
    log_audit(db, user.username, "oem_login", "oem_user", user.id,
              f"oem={user.oem_code}")
    db.commit()
    return {"access_token": token, "token_type": "bearer", "role": user.role,
            "oem": user.oem_code,
            "branding": {"name": org.brand_name or org.name,
                         "color": org.brand_color, "logo_url": org.brand_logo_url}}


@router.post("/change-password")
def oem_change_password(payload: OemPasswordChange, db: Session = Depends(_get_db),
                        principal: dict = Depends(oem_auth.require_oem("read_fleet"))):
    """Rotate your own password. Every OEM role may; nobody may rotate another's.

    The account comes from the PRINCIPAL, never from the body, so there is no
    parameter through which one user could reset another's — which is the shape
    this kind of route usually goes wrong in.
    """
    user = (db.query(models.OemUser)
              .filter(models.OemUser.username == principal["username"]).first())
    if user is None or not verify_password(payload.current_password, user.password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(payload.new_password or "") < 8:
        raise HTTPException(status_code=400,
                            detail="New password must be at least 8 characters")
    user.password = hash_password(payload.new_password)
    log_audit(db, user.username, "oem_change_password", "oem_user", user.id,
              "self-service")
    db.commit()
    return {"message": "Password changed"}


@router.get("/users")
def list_oem_users(db: Session = Depends(_get_db),
                   principal: dict = Depends(oem_auth.require_oem("manage_users"))):
    """The manufacturer's own people. Filtered by the principal's oem_code, so a
    competitor's roster is not merely hidden — it is not selected."""
    rows = (db.query(models.OemUser)
              .filter(models.OemUser.oem_code == principal["oem"])
              .order_by(models.OemUser.id.asc()).all())
    return {"oem_code": principal["oem"], "users": [_oem_user_dict(u) for u in rows]}


@router.post("/users")
def create_oem_user(payload: OemUserCreate, db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem("manage_users"))):
    """Add somebody to THIS manufacturer, with a one-time password.

    `oem_code` comes from the principal and appears nowhere in the request body.
    There is therefore no input through which an administrator at one
    manufacturer could create an account inside another — the usual way a
    multi-tenant user-creation route leaks.

    The role must be one of the four OEM roles. Not merely "not Admin": an
    unrecognised role would produce an account with an empty capability set that
    authenticates and can do nothing, which is a support ticket rather than a
    refusal.
    """
    if payload.role not in oem_auth.OEM_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown role. Choose one of: {', '.join(oem_auth.OEM_ROLES)}")

    username = (payload.username or "").strip()
    try:
        oem_auth.assert_username_available(db, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    temp_password = secrets.token_urlsafe(9)
    user = models.OemUser(oem_code=principal["oem"], username=username,
                          password=hash_password(temp_password),
                          role=payload.role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    log_audit(db, principal["username"], "oem_user_created", "oem_user", user.id,
              f"oem={principal['oem']} username={username} role={payload.role}")
    db.commit()
    out = _oem_user_dict(user)
    out["temporary_password"] = temp_password
    out["note"] = ("Shown once. Share securely; they should change it at "
                   "POST /oem/change-password after first sign-in.")
    return out


@router.patch("/users/{user_id}")
def update_oem_user(user_id: int, payload: OemUserUpdate,
                    db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem("manage_users"))):
    """Change somebody's role, or switch their account off.

    ONE LOCKOUT GUARD, STATING THE ACTUAL REQUIREMENT: an organisation must
    never be left without an active administrator, because the only way back is
    a founder-side bootstrap that deliberately refuses to run once an admin
    exists.

    The first version of this had TWO rules — "you cannot demote yourself" (the
    factory side's #390 rule) and "the last admin cannot be demoted". Writing the
    test showed the second could never fire. Reaching it needs a caller with
    `manage_users`, which only an active OEM_ADMIN has; if the caller is not the
    target then the caller IS a remaining admin, and if the caller is the target
    the self-rule fired first. It was dead code wearing the uniform of a security
    control — worse than nothing, because somebody could later relax the
    self-rule believing this one still covered the case.

    So the self-rule is gone and the requirement is stated once. It is strictly
    more permissive in exactly one situation, deliberately: an administrator may
    step down once somebody else can cover, which is a normal handover and was
    previously refused for no reason anybody could act on.
    """
    user = (db.query(models.OemUser)
              .filter(models.OemUser.oem_code == principal["oem"],
                      models.OemUser.id == user_id).first())
    # 404 for another manufacturer's user, never 403 — a 403 confirms the row
    # exists, which turns id probing into a roster of a competitor's staff.
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    losing_admin = (
        (payload.role is not None and payload.role != oem_auth.OEM_ADMIN)
        or payload.is_active is False)

    if losing_admin and user.role == oem_auth.OEM_ADMIN and user.is_active:
        remaining = (db.query(models.OemUser)
                       .filter(models.OemUser.oem_code == principal["oem"],
                               models.OemUser.role == oem_auth.OEM_ADMIN,
                               models.OemUser.is_active.is_(True),
                               models.OemUser.id != user.id).count())
        if remaining == 0:
            raise HTTPException(
                status_code=400,
                detail="That is the last active administrator. Promote somebody "
                       "else first, or the organisation locks itself out.")

    if payload.role is not None:
        if payload.role not in oem_auth.OEM_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role. Choose one of: {', '.join(oem_auth.OEM_ROLES)}")
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active

    log_audit(db, principal["username"], "oem_user_updated", "oem_user", user.id,
              f"oem={principal['oem']} username={user.username} "
              f"role={user.role} active={user.is_active}")
    db.commit()
    db.refresh(user)
    return _oem_user_dict(user)


def register(app):
    app.include_router(router)
