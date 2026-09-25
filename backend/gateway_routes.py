"""Issuing, listing and revoking the keys a gateway authenticates with (ADR-0041).

Without these, `gateway_credentials` could only be populated with raw SQL, which
makes a security control that nobody will turn on — and a workspace with no
credential is a workspace where the broker's ACLs are the only thing separating
it from every other customer.

THE SECRET IS SHOWN EXACTLY ONCE. It is returned by the POST that creates it and
by nothing else, ever: not the list, not a detail route, not an error. AMP has no
"show me the key again" because the honest answer to a lost key is to issue a new
one and revoke the old, which is two clicks and leaves a trail.

ADMIN ONLY, and for a blunt reason: this issues a credential that can write
production data for a whole site. It is not a supervisor-level action, and it is
the same bar the rest of the write surface uses.

REVOCATION IS A FLAG, NOT A DELETE. Deleting the row would re-open the workspace
to unsigned packets if it were the last one — the fail-open ADR-0041 exists to
avoid — and it would erase the record that the gateway ever existed, which is
exactly what somebody investigating an incident needs.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import gateway_auth
import models
import mqtt_identity
import mqtt_service
from auth import require_roles
from database import SessionLocal
from tenancy import request_tenant

router = APIRouter(tags=["Gateways"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class GatewayCreate(BaseModel):
    gateway_id: str
    site: str = ""
    label: str = ""

    @field_validator("gateway_id")
    @classmethod
    def _id_is_an_identifier(cls, v):
        # The SAME rule the ingest path applies to a claimed id, so an id that
        # can be issued here can always be looked up there. Anything else would
        # let an operator create a credential no gateway could ever use.
        if gateway_auth.claimed_gateway_id({"gateway_id": v}) is None:
            raise ValueError(
                "gateway_id must start with a letter or number and contain only letters, "
                "numbers, dot, dash or underscore (max 64 characters)")
        return v.strip()

    @field_validator("site", mode="before")
    @classmethod
    def _site_is_a_topic_segment(cls, v):
        # "" and "-" both mean "this factory has no site code"; "-" is the wire
        # spelling and mqtt_identity resolves it to "" on the way in, so it is
        # normalised here too and the two can never disagree.
        if v is None or v == "" or v == mqtt_identity.NO_SITE_TOKEN:
            return ""
        if not isinstance(v, str) or not mqtt_identity._IDENTIFIER.match(v.strip()):
            raise ValueError(
                "site must be letters, numbers, dot, dash or underscore — it is part of the "
                "MQTT topic this gateway will publish to")
        return v.strip()


class GatewaySummary(BaseModel):
    """What a gateway looks like from the outside. NO SECRET, deliberately."""

    id: int
    gateway_id: str
    site: str
    label: str | None = None
    is_active: bool
    created_at: datetime | None = None
    last_seen_at: datetime | None = None

    class Config:
        from_attributes = True


class GatewayIssued(GatewaySummary):
    """The create response, and the only thing in AMP that carries the key."""

    secret: str
    topic: str
    shown_once: str = (
        "This key is shown once and AMP does not store a way to show it again. Put it in the "
        "gateway's environment now (AMP_GATEWAY_KEY). If it is lost, issue a new gateway and "
        "revoke this one.")


@router.get("/gateways", response_model=list[GatewaySummary])
def list_gateways(db: Session = Depends(_get_db),
                  current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    return (db.query(models.GatewayCredential)
              .filter(models.GatewayCredential.tenant_code == tenant)
              .order_by(models.GatewayCredential.id.asc())
              .all())


@router.post("/gateways", response_model=GatewayIssued, status_code=201)
def issue_gateway(body: GatewayCreate, db: Session = Depends(_get_db),
                  current_user: dict = Depends(require_roles(["Admin"]))):
    """Issue a gateway id and key for one site of this workspace.

    NOTE WHAT THIS CHANGES BEYOND ITSELF: the first credential a workspace has
    CLOSES that workspace. From then on every MQTT message for it must be signed
    by a registered, active gateway. That is the point, and it is said in the
    response rather than left for somebody to discover when telemetry stops.
    """
    tenant = request_tenant(current_user)
    secret = gateway_auth.issue_secret()
    credential = models.GatewayCredential(
        tenant_code=tenant,
        gateway_id=body.gateway_id,
        site=body.site,
        label=body.label or None,
        secret=secret,
        is_active=True,
    )
    db.add(credential)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # gateway_id is unique across the INSTALLATION, not per tenant — the id
        # arrives in a payload before AMP knows whose it is, so it has to resolve
        # to exactly one credential. The message says that without revealing
        # whether the clash is inside this workspace or another one.
        raise HTTPException(
            status_code=409,
            detail=f"The gateway id {body.gateway_id!r} is already in use. Gateway ids are "
                   f"unique across AMP, so prefix them with your workspace and site "
                   f"(for example gw-{tenant.lower()}-{body.site or 'site'}-01).")
    db.refresh(credential)

    # Built from the summary PLUS the two fields that exist only in this
    # response. Validating the ORM row straight into GatewayIssued cannot work
    # and should not: `secret` and `topic` are not columns, and the day one of
    # them becomes one is the day the key starts leaking from the list.
    issued = GatewayIssued(
        **GatewaySummary.model_validate(credential, from_attributes=True).model_dump(),
        secret=secret,
        topic="")
    # The exact topic to put in the gateway's config. An empty site is written
    # back as the wire token "-", because an empty topic segment ("a//b") is
    # legal MQTT and invisible when reading a log.
    site_token = credential.site or mqtt_identity.NO_SITE_TOKEN
    issued.topic = f"{mqtt_service.TOPIC_PREFIX}/{tenant}/{site_token}/machines"
    return issued


@router.post("/gateways/{credential_id}/revoke", response_model=GatewaySummary)
def revoke_gateway(credential_id: int, db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(["Admin"]))):
    """Stop a gateway, from its very next packet.

    A flag rather than a delete: deleting the last credential would re-open the
    workspace to unsigned traffic, which is the opposite of what revoking means,
    and it would erase the evidence that the gateway ever existed.
    """
    tenant = request_tenant(current_user)
    credential = (db.query(models.GatewayCredential)
                    .filter(models.GatewayCredential.id == credential_id,
                            models.GatewayCredential.tenant_code == tenant)
                    .first())
    if credential is None:
        raise HTTPException(status_code=404, detail="No such gateway in this workspace")
    credential.is_active = False
    db.commit()
    db.refresh(credential)
    return credential


@router.post("/gateways/{credential_id}/reactivate", response_model=GatewaySummary)
def reactivate_gateway(credential_id: int, db: Session = Depends(_get_db),
                       current_user: dict = Depends(require_roles(["Admin"]))):
    """Undo a revocation. The KEY is unchanged — it never left the gateway."""
    tenant = request_tenant(current_user)
    credential = (db.query(models.GatewayCredential)
                    .filter(models.GatewayCredential.id == credential_id,
                            models.GatewayCredential.tenant_code == tenant)
                    .first())
    if credential is None:
        raise HTTPException(status_code=404, detail="No such gateway in this workspace")
    credential.is_active = True
    db.commit()
    db.refresh(credential)
    return credential


def register(app):
    app.include_router(router)
