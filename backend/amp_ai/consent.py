"""Per-tenant learning consent for AMP-native AI: the database-backed ConsentGate (ADR-0020).

WHAT "LEARNING" MEANS HERE
--------------------------
AMP's models are trained on synthetic data and ship as pinned JSON artifacts.
SCORING a tenant's records with one of them reads that tenant's data but learns
nothing from it, and needs no consent beyond the tenant using AMP. FITTING
anything to a tenant's own records - a mean, a spread, a threshold - is learning
from that tenant's data. The only capability that does so today is
``telemetry_baseline``: the anomaly check fits one machine's normal range from its
own last 14 days of telemetry, for the duration of one request.

The rule: no learning capability runs for a tenant unless an ADMIN OF THAT TENANT
has granted it, and it stops on the next request after they revoke it.

HOW THAT IS ENFORCED
--------------------
* ``DbConsentGate.check`` reads the tenant's row on EVERY call, filtered by
  ``tenant_code`` explicitly (the table is outside the ADR-0002 hook, like
  agent_policies). No row, a revoked row, an unknown capability or a blank tenant
  is a refusal with a reason a person can read. There is no cache, so revocation
  bites on the next request.
* ``set_consent`` is the only writer of a decision. (``remove_for_company``
  only deletes a company's rows when the company leaves the registry, with an
  audit record per row.) ``set_consent`` validates the capability against
  ``LEARNING_CAPABILITIES``, updates the row and adds the AuditLog record, then
  COMMITS ONCE. If the audit row cannot be written the whole change is rolled
  back: consent never changes without a record of who changed it. (It builds the
  audit row with ``platform_routes.build_audit_row``, the same factory
  ``log_audit`` uses; ``log_audit`` itself commits separately and swallows
  errors, which is exactly what must not happen here.)
* WHO may call it - Admin only, never from a founder preview - is decided by the
  route (native_ai_routes.py), before this module is reached. A model never
  decides it.
"""
import json
from datetime import datetime

import models
import platform_routes

from .core.contracts import CAPABILITY_TELEMETRY_BASELINE, LEARNING_CAPABILITIES, ConsentDecision

__all__ = ["AUDIT_ACTION_PREFIX", "AUDIT_ENTITY", "AUDIT_GRANTED", "AUDIT_REMOVED_WITH_COMPANY", "AUDIT_REVOKED",
           "CAPABILITY_INFO", "DbConsentGate", "set_consent", "remove_for_company", "is_consent_audit_record",
           "consent_view"]

AUDIT_ENTITY = "ai_learning_consent"
AUDIT_GRANTED = "ai.learning_consent.granted"
AUDIT_REVOKED = "ai.learning_consent.revoked"
AUDIT_REMOVED_WITH_COMPANY = "ai.learning_consent.removed_with_company"
# Every consent audit record's action starts with this, and its entity_type is
# AUDIT_ENTITY. POST /audit-logs refuses both (platform_routes.create_audit_log),
# so only this module writes the consent history.
AUDIT_ACTION_PREFIX = "ai.learning_consent."

# What an Admin is agreeing to, in the words the consent card shows. Kept next to
# the gate so the promise and the enforcement are reviewed together.
CAPABILITY_INFO = {
    CAPABILITY_TELEMETRY_BASELINE: {
        "title": "Learn each machine's normal telemetry",
        "reads": ("When one of this company's own Admins or Supervisors opens the anomaly check for one "
                  "machine, AMP reads that machine's own telemetry from the last 14 days (AMP keeps no older "
                  "telemetry) and fits that machine's normal range from it. It never runs for AMP staff "
                  "previewing the company from the platform workspace."),
        "stored": ("Nothing. The fitted baseline exists only while that one request is answered. It is not "
                   "saved, cached, pooled across machines or companies, or used to train any model."),
        "used_by": "The anomaly check (GET /ai/native/anomaly/machines/{id}).",
        "without": "The anomaly check refuses and says why. Nothing else in AMP changes.",
    },
}

if set(CAPABILITY_INFO) != set(LEARNING_CAPABILITIES):  # pragma: no cover - import-time consistency check
    raise RuntimeError("CAPABILITY_INFO must describe exactly the LEARNING_CAPABILITIES")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _refused(capability, reason):
    return ConsentDecision(granted=False, capability=capability, reason=reason, granted_by=None, granted_at=None)


class DbConsentGate:
    """The ConsentGate backed by ``ai_learning_consents``. Stateless; reads the row on every check."""

    def check(self, db, tenant, capability) -> ConsentDecision:
        named = capability if isinstance(capability, str) and capability else "unknown"
        if not isinstance(tenant, str) or not tenant.strip():
            return _refused(named, "No company was given, so there is no consent to find.")
        if capability not in LEARNING_CAPABILITIES:
            return _refused(named, f"'{named}' is not a learning capability AMP offers, so it cannot "
                                   "have been granted.")
        # Spelled out, not aliased: test_unscoped_model_reads' sweep and the
        # integration structural test find consent reads by this exact name.
        row = (db.query(models.AiLearningConsent)
               .filter(models.AiLearningConsent.tenant_code == tenant,
                       models.AiLearningConsent.capability == capability).first())
        title = CAPABILITY_INFO[capability]["title"]
        if row is None:
            return _refused(capability, f"No Admin of this company has turned on '{title}'. An Admin can "
                                        "turn it on under Agent Activity, AI learning consent.")
        if row.granted is not True:
            if row.revoked_at is not None:
                return _refused(capability, f"'{title}' was revoked by {row.revoked_by or 'an Admin'} on "
                                            f"{_iso(row.revoked_at)}.")
            return _refused(capability, f"'{title}' is turned off for this company.")
        return ConsentDecision(granted=True, capability=capability,
                               reason=f"Granted by {row.granted_by or 'an Admin'} on {_iso(row.granted_at)}.",
                               granted_by=row.granted_by, granted_at=row.granted_at)


def set_consent(db, tenant, capability, granted, actor, *, now=None):
    """Grant or revoke one capability for one tenant, with its audit record, in ONE commit.

    Raises ValueError for a blank tenant or an unknown capability and TypeError
    for a non-boolean decision, before anything is written. Any failure while
    writing (including the audit row) rolls the whole change back and re-raises.
    """
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("a consent belongs to exactly one company; tenant is required")
    if capability not in LEARNING_CAPABILITIES:
        raise ValueError(f"unknown learning capability {capability!r}")
    if type(granted) is not bool:
        raise TypeError("granted must be True or False")
    actor = actor or "unknown"
    now = now or datetime.utcnow()
    try:
        row = (db.query(models.AiLearningConsent)
               .filter(models.AiLearningConsent.tenant_code == tenant,
                       models.AiLearningConsent.capability == capability).first())
        previous = None if row is None else row.granted is True
        if row is None:
            row = models.AiLearningConsent(tenant_code=tenant, capability=capability, granted=False)
            db.add(row)
            db.flush()
        if granted:
            row.granted = True
            row.granted_by = actor
            row.granted_at = now
            row.revoked_by = None
            row.revoked_at = None
        else:
            row.granted = False
            row.revoked_by = actor
            row.revoked_at = now
        row.updated_at = now
        details = json.dumps({"capability": capability, "previous": previous, "new": granted}, sort_keys=True)
        db.add(platform_routes.build_audit_row(actor, AUDIT_GRANTED if granted else AUDIT_REVOKED,
                                               AUDIT_ENTITY, row.id, details=details, tenant_code=tenant))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return row


def remove_for_company(db, tenant, actor) -> list:
    """Delete every consent row of ``tenant``, adding one audit record per row. Does NOT commit.

    For removing a company from the registry (saas_routes.delete_company_tenant),
    with or without the data purge. Consent is given by a company's Admin and
    must not outlive that company: a registry delete without ``purge`` leaves the
    tenant's other rows behind, and the same code can be registered again for a
    different company, which never opted in. The caller commits ONCE, so the
    registry row, the consent rows and their audit records go together.

    Returns the capabilities removed. Raises ValueError for a blank tenant.
    """
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("a consent belongs to exactly one company; tenant is required")
    found = (db.query(models.AiLearningConsent)
             .filter(models.AiLearningConsent.tenant_code == tenant)
             .order_by(models.AiLearningConsent.id).all())
    removed = []
    for row in found:
        details = json.dumps({"capability": row.capability, "previous": row.granted is True, "new": False,
                              "reason": "the company was removed from the tenant registry"}, sort_keys=True)
        db.add(platform_routes.build_audit_row(actor or "unknown", AUDIT_REMOVED_WITH_COMPANY, AUDIT_ENTITY,
                                               row.id, details=details, tenant_code=tenant))
        removed.append(row.capability)
        db.delete(row)
    return removed


def is_consent_audit_record(action, entity_type) -> bool:
    """True for an audit record in the consent history's namespace, however it is cased or padded."""
    def norm(value):
        return value.strip().lower() if isinstance(value, str) else ""
    return norm(action).startswith(AUDIT_ACTION_PREFIX) or norm(entity_type) == AUDIT_ENTITY


def consent_view(db, tenant) -> list:
    """Every learning capability for ``tenant``: what it reads, what it stores, and its current state."""
    rows = {r.capability: r for r in db.query(models.AiLearningConsent)
            .filter(models.AiLearningConsent.tenant_code == tenant).all()}
    out = []
    for capability in LEARNING_CAPABILITIES:
        r = rows.get(capability)
        entry = {"capability": capability}
        entry.update(CAPABILITY_INFO[capability])
        entry.update({
            "granted": bool(r is not None and r.granted is True),
            "granted_by": r.granted_by if r is not None else None,
            "granted_at": _iso(r.granted_at) if r is not None else None,
            "revoked_by": r.revoked_by if r is not None else None,
            "revoked_at": _iso(r.revoked_at) if r is not None else None,
            "updated_at": _iso(r.updated_at) if r is not None else None,
        })
        out.append(entry)
    return out
