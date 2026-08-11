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
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import oem_auth
import oem_events
import oem_service
import oem_sharing
import oem_telemetry
from database import SessionLocal
from events import event_bus

router = APIRouter(prefix="/oem", tags=["OEM"])

MAX_PAGE = 200


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


def register(app):
    app.include_router(router)
