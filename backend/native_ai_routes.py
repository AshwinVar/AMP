"""AMP-native AI routes (ADR-0020): failure risk, the anomaly check, model cards, AI consent.

THE CHAIN, IN THE ORDER EVERY HANDLER HERE WALKS IT
---------------------------------------------------
USER -> AUTHENTICATION -> RBAC -> TENANT / CONSENT -> AMP TOOL -> DATA -> MODEL.

  * AUTHENTICATION  `get_current_user` (directly or through `require_roles`),
                    which also refuses OEM principals (ADR-0017).
  * RBAC            failure risk, the anomaly check and reading consent:
                    Admin + Supervisor. Changing consent: Admin only. Model
                    cards: any signed-in factory user (they hold no tenant data).
  * TENANT          `request_tenant(current_user)`, the same effective tenant
                    every other factory route uses. The models never supply one.
  * CONSENT         only the anomaly check learns from a tenant's own data, so
                    only it consults a gate here - ALWAYS `DbConsentGate()`,
                    which reads the tenant's stored decision on every call
                    (test_amp_ai_integration_structural.py pins the call site).
                    The consent page below also carries the Copilot's
                    `external_model` decision (ADR-0037); THAT gate sits where
                    the Copilot builds a request's model, ai_copilot._copilot_llm.
  * TOOL / DATA     amp_ai.failure_risk.db_history and
                    amp_ai.telemetry_anomaly.db_telemetry: explicit tenant
                    filters, both time bounds, column-only reads.
  * MODEL           receives what the data layer returned and nothing else.

PLAN GATE AND THROTTLE
----------------------
/ai/native/* and /ai/models sit under the Intelligence Pack's "/ai" prefix in
modules.json. /ai-consent deliberately does NOT ("/ai-consent" is not under
"/ai/"): a tenant that downgrades must still be able to see and WITHDRAW a
consent it gave. The two scoring endpoints and the cards are in
http_security.RATE_LIMITS.

WHY FOUNDER PREVIEW CANNOT CONSENT, OR LEARN
--------------------------------------------
A founder Admin may PREVIEW a customer (X-Tenant) and read that customer's
consent page. Consent to learn from a company's data, or to send it outside
AMP, is that company's decision; a platform operator switching into their
workspace is not an Admin OF that company, so the PUT refuses whenever the
effective tenant differs from the token's own tenant claim. The founder's own
DEFAULT workspace is its own company. The same test refuses the anomaly check
(the one learning step) from a preview, and the Copilot's hosted model
(ai_copilot.external_model_allowed): what an Admin consents to is the company's
OWN people using it. Failure risk learns nothing, so it answers in a preview
like any factory read.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.orm import Session

import ai_copilot
import logging_config
from amp_ai import consent, registry
from amp_ai.core.contracts import CAPABILITY_EXTERNAL_MODEL, SCOPED_CAPABILITIES, ConsentRequired
from amp_ai.failure_risk import baseline_rule, db_history, predict
from amp_ai.failure_risk.history import LOOKBACK_DAYS, in_breakdown_at
from amp_ai.telemetry_anomaly import service
from auth import get_current_user, require_roles
from database import SessionLocal
import tenancy
from tenancy import request_tenant

log = logging_config.get_logger(__name__)

ANALYST_ROLES = ["Admin", "Supervisor"]
CONSENT_EDITOR_ROLES = ["Admin"]

# What the rule number beside the model is, so nobody compares it with the
# Machine Health screen's number and concludes one of them is wrong.
RULE_BASIS = ("AMP's rule-based risk scorer (predictive_engine), computed at the same moment from the "
              "30 days before it: breakdowns and downtime from machine events and downtime logs, and the "
              "machine's last recorded status. It uses no work orders, so it can differ from the number on "
              "Machine Health. It is the baseline the model was evaluated against.")


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(tags=["AMP-native AI"])


# --- failure risk ------------------------------------------------------------------
def _rule_only(histories, as_of):
    """The rule score for every machine, for when the model artifact is unavailable.

    The same adapter predict() uses (baseline_rule.rule_row), so the rule column
    does not change when the model column disappears.
    """
    out = []
    for h in histories:
        row = baseline_rule.rule_row(h, as_of)
        out.append({
            "machine_id": h.machine_id, "name": h.name, "probability": None, "band": None,
            "top_contributions": [], "rule_score": float(row["risk_score"]), "rule_level": row["risk_level"],
            "model_version": None, "adopted": None,
            "excluded_reason": "already_in_breakdown" if in_breakdown_at(h, as_of) else None,
            "data_basis": {"lookback_days": LOOKBACK_DAYS, "learned_from": False},
        })
    return out


@router.get("/ai/native/failure-risk")
def native_failure_risk(db: Session = Depends(_get_db),
                        current_user: dict = Depends(require_roles(ANALYST_ROLES))):
    """Each machine's model probability of STARTING a breakdown in the next 7 days, beside the rule."""
    tenant = request_tenant(current_user)
    as_of = datetime.utcnow()          # naive UTC, as every AMP timestamp column stores it
    histories = db_history.load_histories(db, tenant, as_of)
    result = predict.predict(histories, as_of)
    if result.get("status") != "ok":
        log.info("[amp-native] failure-risk model unavailable: %s", result.get("reason"))
        result = {
            "status": result.get("status"), "reason": result.get("reason"), "caveat": predict.CAVEAT,
            "model_name": predict.MODEL_NAME, "model_version": None, "artifact_sha256": None, "adopted": None,
            "as_of": as_of.isoformat(), "machines": _rule_only(histories, as_of),
        }
    result["rule_basis"] = RULE_BASIS
    result["evaluation_scope"] = registry.failure_risk_evaluation_scope()
    return result


# --- telemetry anomaly -----------------------------------------------------------------
@router.get("/ai/native/anomaly/machines/{machine_id}")
def native_anomaly(machine_id: int, db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(ANALYST_ROLES))):
    """The last hour of one machine's telemetry against a baseline fitted from its own previous 14 days.

    403 {code: learning_not_from_preview, reason} from a founder preview, before anything is read.
    404 for a machine this tenant does not own, BEFORE consent is looked at.
    403 {code: learning_consent_required, capability, reason} without consent.
    200 with a null score for insufficient history or an unavailable evaluation.
    """
    tenant = request_tenant(current_user)
    if tenancy.is_preview(current_user):
        # The consent covers the company's OWN Admins and Supervisors opening the
        # check (consent.CAPABILITY_INFO "reads"). A platform operator previewing
        # the company is neither, so the learning step does not run for them -
        # the same rule that stops a preview from giving the consent.
        reason = (f"You are previewing {tenant} from the platform workspace. The anomaly check learns from "
                  f"{tenant}'s own telemetry, and {tenant}'s consent covers only its own Admins and "
                  "Supervisors, so it does not run from a preview.")
        return JSONResponse(status_code=403, content={"code": "learning_not_from_preview", "reason": reason,
                                                      "detail": reason})
    try:
        return service.score_machine(db, tenant, machine_id, gate=consent.DbConsentGate())
    except service.MachineNotFound:
        raise HTTPException(status_code=404, detail="Machine not found") from None
    except ConsentRequired as exc:
        d = exc.decision
        return JSONResponse(status_code=403, content={
            "code": "learning_consent_required", "capability": d.capability,
            "reason": d.reason, "detail": d.reason,
        })


@router.get("/ai/native/anomaly/sweep")
def native_anomaly_sweep(db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(ANALYST_ROLES))):
    """The same check as above, over every machine, with a reason for each one (ADR-0032).

    Consent is checked once for the fleet, not once per machine, and a refusal
    is the whole sweep's answer rather than an empty list: an empty list reads
    as "nothing unusual", which is a claim AMP has not made.

    The model is EXPERIMENTAL and was not adopted, so every scored row carries
    MODEL NOT VALIDATED and nothing here is worded as an alarm.
    """
    from ai.anomaly_sweep import build_anomaly_sweep
    return build_anomaly_sweep(
        db, request_tenant(current_user), scorer=service.score_machine,
        gate=consent.DbConsentGate(), previewing=tenancy.is_preview(current_user))


# --- model cards ---------------------------------------------------------------------------
@router.get("/ai/models")
def native_model_cards(current_user: dict = Depends(get_current_user)):
    """Every registered model's public card: provenance, held-out metrics beside the baseline, verdict."""
    return {"models": registry.cards(), "caveat": registry.CAVEAT}


@router.get("/ai/models/{name}")
def native_model_card(name: str, current_user: dict = Depends(get_current_user)):
    """One card. `name` is looked up in the fixed registry; it is never used to build a path."""
    card = registry.card(name)
    if card is None:
        raise HTTPException(status_code=404, detail="Unknown model")
    return card


# --- AI consent: learning from the company's data, and sending it to a hosted model ----------
class ConsentUpdate(BaseModel):
    """`granted` must be a JSON true/false: "yes", 1 or a missing field is a 422, never coerced."""

    model_config = ConfigDict(extra="forbid")
    granted: StrictBool


def _consent_page(db, tenant, current_user):
    previewing = tenancy.is_preview(current_user)
    is_admin = current_user.get("role") in CONSENT_EDITOR_ROLES
    if previewing:
        reason = (f"You are previewing {tenant} from the platform workspace. Only an Admin of {tenant} can "
                  "give or withdraw its consent, never a preview.")
    elif not is_admin:
        reason = "Only an Admin of this company can change its AI consent."
    else:
        reason = None
    return {
        "tenant": tenant,
        "can_edit": reason is None,
        "read_only_reason": reason,
        "capabilities": consent.consent_view(db, tenant, configured=_configured_scopes()),
    }


def _configured_scopes():
    """What each SCOPED capability would be used for right now (ADR-0038): for
    external_model, the hosted provider configured, or None when there is none."""
    hosted = ai_copilot.hosted_provider()
    return {CAPABILITY_EXTERNAL_MODEL: hosted.name if hosted is not None else None}


@router.get("/ai-consent")
def get_learning_consent(db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(ANALYST_ROLES))):
    """What AMP may learn from this company's own data, and whether its Copilot questions may go
    to a hosted AI model: each decision, who made it, and when."""
    return _consent_page(db, request_tenant(current_user), current_user)


@router.put("/ai-consent/{capability}")
def put_learning_consent(capability: str, payload: ConsentUpdate, db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(CONSENT_EDITOR_ROLES))):
    """Grant or withdraw one consent capability for THIS company. Admin only; never from a preview.

    The consent row and its AuditLog record are committed together
    (amp_ai.consent.set_consent): if the audit cannot be written, nothing changes.
    """
    tenant = request_tenant(current_user)
    if tenancy.is_preview(current_user):
        raise HTTPException(status_code=403, detail=(
            "A company's AI consent can only be given or withdrawn by that company's own Admin, not from a "
            "platform preview."))
    actor = current_user.get("sub") or current_user.get("username")
    # ADR-0038: a scoped consent is given FOR the hosted provider configured at
    # this moment, and for nothing when none is -- there is nothing to consent
    # to, so the grant is refused rather than stored in advance (ADR-0017's rule).
    scope = _configured_scopes().get(capability) if capability in SCOPED_CAPABILITIES else None
    if payload.granted and capability in SCOPED_CAPABILITIES and not scope:
        raise HTTPException(status_code=400, detail=(
            "No hosted AI provider is configured, so there is nothing to consent to. The switch applies "
            "once one is."))
    try:
        # set_consent is the one place a capability is validated: an unknown one
        # raises ValueError before anything is written, and that is a 400.
        consent.set_consent(db, tenant, capability, payload.granted, actor, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _consent_page(db, tenant, current_user)
