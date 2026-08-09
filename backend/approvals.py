"""THE approval gate. Every decision on an agent action passes through here.

WHAT WAS MEASURED BEFORE THIS EXISTED
-------------------------------------
An adversarial probe against master, one attempt per row:

    1. another tenant's Admin approves : refused 404      PO stays Draft
    2. role guard declared on the route: True             Admin/Supervisor
    3. the SAME approve replayed       : refused 400      PO stays Approved
    4. reject an already-approved one  : refused 400      PO stays Approved
    5. ai.agents.apply_decision() direct: ACCEPTED        PO -> Approved
    6. apply_decision() a SECOND time  : action Rejected, PO still Approved
    7. a DELETED user approves         : ACCEPTED         PO -> Approved
    8. a 400-day-old proposal          : ACCEPTED         PO -> Approved

Rows 1-4 held. Rows 5-8 did not, and each is a different kind of failure:

* **5** — ``apply_decision`` carried no guard of its own. The route checked the
  status; the function did not. Today every caller happens to be guarded, so
  this was not exploitable — but it means the safety of a money-moving action
  rested on every future caller remembering, which is the same shape as the
  ingest defect in ADR-0011.

* **6** — the worst of the four, because it corrupts the RECORD rather than the
  action. Calling apply_decision twice flipped the AgentAction to "Rejected"
  while the purchase order stayed "Approved" (the item-level guard held). The
  audit trail then says a human rejected something the system went ahead with.
  An audit log that contradicts reality is worse than no audit log, because it
  is believed.

* **7** — ``auth.get_current_user`` decodes the JWT and performs NO database
  lookup, so there was no way to revoke access at all. A user deleted from the
  database still approved a purchase order. Their token simply had to expire.

* **8** — no staleness bound. A recommendation is evidence about a moment; a
  400-day-old "reorder steel because stock is low" is a claim about a stock
  level that has long since changed.

WHAT THIS MODULE DOES
---------------------
``authorise(db, action, actor, decision)`` answers one question — may this
person make this decision on this action, right now — and raises a precise
HTTPException when the answer is no. It is called by the route AND by
``ai.agents.apply_decision``, so the guard cannot be bypassed by reaching past
the HTTP layer.

The checks, in order, cheapest and most-specific first:

    tenant     the action belongs to the actor's tenant
    state      the action is still Proposed (not decided, expired or cancelled)
    freshness  the action has not passed its expiry
    actor      the approver still exists, is active, is in this tenant, and
               still holds a role that may approve

The actor check is a database lookup, deliberately, and deliberately HERE
rather than in ``get_current_user``: putting it on every request would add a
SELECT to all of them to defend an action that happens a handful of times a day.
This is the boundary where a stale credential actually costs something.

AUTO-APPROVAL IS STILL VALID APPROVAL
------------------------------------
``ai.agents.should_auto_approve`` lets a tenant declare, in advance and in
writing (AgentPolicy), that a given agent may act without a human. That is a
decision a human made — it is simply made earlier. The gate treats it as such:
the policy path skips the ACTOR check (there is no human actor) but still passes
tenant, state and freshness. An expired proposal is not auto-approved either.
"""
from datetime import datetime, timedelta

from fastapi import HTTPException

import models

# How long a proposal stays actionable when nothing set an explicit expiry.
# Seven days: long enough to survive a holiday, short enough that the stock
# level or machine condition it was based on has probably not changed.
DEFAULT_TTL_DAYS = 7

# Roles that may decide an agent action. Mirrors the route's require_roles, and
# is re-checked here against the DATABASE rather than the token, so a demotion
# takes effect without waiting for the token to expire.
APPROVER_ROLES = ("Admin", "Supervisor")

# Statuses from which a decision may still be made.
DECIDABLE = ("Proposed",)


class ApprovalDenied(HTTPException):
    """Refused by the gate. Carries the precise reason, never a generic 403 —
    an operator who cannot tell 'expired' from 'not yours' cannot fix it."""

    def __init__(self, status_code, detail):
        super().__init__(status_code=status_code, detail=detail)


def is_expired(action, now=None):
    """Has this proposal passed its expiry?

    An explicit expires_at wins. A NULL falls back to DEFAULT_TTL_DAYS from
    created_at — a missing expiry must not mean "never expires", which is what
    every row written before this column existed would otherwise mean.
    """
    now = now or datetime.utcnow()
    if action.expires_at is not None:
        return now >= action.expires_at
    if action.created_at is None:
        # No creation time either: treat as expired rather than eternally
        # actionable. Fail closed on an unmeasurable age.
        return True
    return now >= action.created_at + timedelta(days=DEFAULT_TTL_DAYS)


def _check_tenant(action, tenant):
    if not tenant or action.tenant_code != tenant:
        # 404 rather than 403: a cross-tenant probe must not learn that the id
        # exists. Matches the route's existing behaviour.
        raise ApprovalDenied(404, "Agent action not found")


def _check_state(action):
    status = action.status or "unknown"
    if status not in DECIDABLE:
        raise ApprovalDenied(400, f"Already {status.lower()}")


def _check_freshness(action, now=None):
    if is_expired(action, now):
        raise ApprovalDenied(
            409,
            "This proposal has expired and can no longer be actioned. The "
            "conditions it was based on may have changed; ask the agent to "
            "re-evaluate.")


def _check_actor(db, action, actor):
    """The approver must still be a real, active, sufficiently-privileged user
    OF THIS TENANT — checked against the database, not the token."""
    username = (actor or {}).get("sub") or (actor or {}).get("username")
    if not username:
        raise ApprovalDenied(401, "Not authenticated")

    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise ApprovalDenied(401, "This account no longer exists")
    # Falsy, not `is False`. A NULL here is unreachable — the column is NOT NULL
    # with a server default, and migration 0005 backfills it — but where the
    # value cannot be established, a security guard should refuse rather than
    # admit, the same way is_expired treats an unmeasurable age as expired.
    # What makes NULL unreachable is the SCHEMA, so that is what is pinned
    # (test_approval_gate: "the schema cannot produce an unknown is_active").
    if not user.is_active:
        raise ApprovalDenied(403, "This account has been disabled")
    if (user.tenant_code or "") != action.tenant_code:
        raise ApprovalDenied(404, "Agent action not found")
    if user.role not in APPROVER_ROLES:
        raise ApprovalDenied(
            403, "You do not have permission to perform this action")
    return user


def authorise(db, action, actor, decision, now=None, require_actor=True):
    """May this decision be made on this action right now? Raises if not.

    ``require_actor=False`` is the auto-approval path: a tenant declared in
    advance (AgentPolicy) that this agent may act unattended, so there is no
    human to verify — but tenant, state and freshness still apply.
    """
    if decision not in ("approve", "reject"):
        raise ApprovalDenied(400, f"Unknown decision {decision!r}")

    if require_actor:
        tenant = (actor or {}).get("tenant")
        _check_tenant(action, tenant)
    _check_state(action)
    _check_freshness(action, now)
    if require_actor:
        return _check_actor(db, action, actor)
    return None


def expire_stale(db, tenant, now=None):
    """Mark proposals past their expiry as Expired.

    Housekeeping, so the queue a human sees does not fill with proposals they
    cannot action. The gate does not depend on this having run — is_expired is
    evaluated at decision time — which is deliberate: a guard that needs a
    background job to have run is a guard that is off whenever the job is.
    """
    now = now or datetime.utcnow()
    stale = [a for a in db.query(models.AgentAction).filter(
        models.AgentAction.tenant_code == tenant,
        models.AgentAction.status == "Proposed").all()
        if is_expired(a, now)]
    for a in stale:
        a.status = "Expired"
        a.decided_at = now
        a.decided_by = "system-expiry"
    return len(stale)
