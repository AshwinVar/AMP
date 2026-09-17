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
    freshness  the action has not passed its expiry -- APPROVE only; rejecting
               an expired proposal is the recorded exit for the item it holds
    actor      the approver still exists, is active, is in this tenant, and
               still holds a role that may approve
    item       the item the decision would move still exists in the action's
               tenant and is still in its pending status (409 otherwise)

HELD ITEMS (ADR-0015 addendum, 2026-09-17)
------------------------------------------
Measured on master: PATCH and DELETE on /maintenance/tasks/{id},
/escalations/{id} and /purchase-orders/{id} moved or removed an agent-proposed
item while its AgentAction stayed Proposed, and a later decision then recorded
Approved / Rejected against an item that never moved. Two halves close it:

* the ITEM check above: no decision is recorded unless its item moves in the
  same transaction (the route withdraws a proposal that can no longer take
  effect, with a compare-and-set, instead of recording a false decision);
* the LOCK: ``refuse_if_awaiting_decision`` in the six item write handlers
  refuses every change and delete (409, any role) while the item is held.

An item is HELD exactly when all of these are true (``awaiting_decision`` is the
one implementation):

    its status is its kind's pending status (PENDING)
    a Proposed AgentAction of the same tenant, kind and id points at it
    its tenant's plan can reach the decision API (else nobody could decide it)

Expiry is ignored: an expired proposal still holds its item, and reject is the
way out. Human look-alikes (pending status, no Proposed action) are ordinary.

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
import module_manifest

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

# The items agents propose, keyed by AgentAction.ref_kind: (model, the pending
# status apply_decision moves the item out of). The lock, the gate's item check,
# the list flag and the orphan sweep all read this one table.
PENDING = {
    "maintenance_task": (models.MaintenanceTask, "Proposed"),
    "purchase_order": (models.PurchaseOrder, "Draft"),
    "escalation": (models.Escalation, "Proposed"),
}
_KIND_FOR_MODEL = {model: kind for kind, (model, _) in PENDING.items()}
_NOUN = {"maintenance_task": "maintenance task", "purchase_order": "purchase order",
         "escalation": "escalation"}

# The API a human decides through. Its module pack (module_manifest) must be in
# the tenant's licence for an item to be held: a tenant that cannot reach it
# could never release the lock.
DECISION_API_PATH = "/agent-actions"

# decided_by for a proposal withdrawn because it can no longer take effect.
WITHDRAWN_BY = "system-withdrawn"


class ApprovalDenied(HTTPException):
    """Refused by the gate. Carries the precise reason, never a generic 403 —
    an operator who cannot tell 'expired' from 'not yours' cannot fix it."""

    def __init__(self, status_code, detail):
        super().__init__(status_code=status_code, detail=detail)


class ProposalWithdrawn(ApprovalDenied):
    """Refused by the item check: the item this proposal would move is gone,
    moved, or in another tenant, so no decision may be recorded. The route
    answers it by withdrawing the proposal (``withdraw``)."""


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
    human to verify — but tenant, state, freshness and the item still apply.

    The item check runs LAST, after the actor: a caller from another tenant
    still gets 404 and an unprivileged one 403, so neither learns anything about
    the item -- and only a verified approver can cause a withdrawal.
    """
    if decision not in ("approve", "reject"):
        raise ApprovalDenied(400, f"Unknown decision {decision!r}")

    if require_actor:
        # An actor that is not a mapping is a caller bug, and it must still be
        # a REFUSAL rather than an AttributeError from `.get`. Measured in the
        # final adversarial re-audit: passing the username as a bare string
        # raised AttributeError, which reaches a customer as a 500 with a stack
        # trace instead of "not authenticated". No caller does this today --
        # the route passes a dict -- but the whole reason this module exists is
        # that a guard which only works for well-behaved callers is not a guard.
        if actor is not None and not isinstance(actor, dict):
            raise ApprovalDenied(401, "Not authenticated")
        # No isinstance check on the tenant claim: a list, an int or False are
        # all refused by _check_tenant already -- falsy ones by `not tenant`,
        # truthy ones by inequality with a string column. A type check here
        # mutation-tested as changing nothing, which makes it a comment wearing
        # an if-statement.
        _check_tenant(action, (actor or {}).get("tenant"))
    _check_state(action)
    # Freshness gates APPROVE only (ADR-0015 addendum, 2026-09-17). An expired
    # proposal still holds its item, and a recorded rejection by a verified
    # approver is how that item is released; approving stale evidence stays 409.
    if decision == "approve":
        _check_freshness(action, now)
    user = _check_actor(db, action, actor) if require_actor else None
    _check_item(db, action)
    return user


def _check_item(db, action):
    """No decision without its item moving: the item must still exist in the
    action's tenant and still be pending. Locks the row where the engine can
    (SELECT ... FOR UPDATE), so a concurrent decision waits and then sees the
    item already moved instead of recording a second decision."""
    item, reason = locate_pending_item(db, action, lock=True)
    if item is None:
        raise ProposalWithdrawn(
            409, f"Nothing was decided: {reason}, so this proposal can no longer "
                 "take effect.")


def locate_pending_item(db, action, lock=False):
    """(item, None) when the action's item exists in the action's tenant and is
    in its pending status; otherwise (None, why-not)."""
    entry = PENDING.get(action.ref_kind)
    if entry is None:
        return None, f"it names an unknown kind of item ({action.ref_kind!r})"
    model, pending = entry
    noun = _NOUN[action.ref_kind]
    if action.ref_id is None:
        return None, f"no {noun} is recorded against it"
    query = db.query(model).filter(model.id == action.ref_id,
                                   model.tenant_code == action.tenant_code)
    if lock:
        query = query.populate_existing().with_for_update()
    item = query.first()
    if item is None:
        return None, f"the {noun} it would change no longer exists"
    if item.status != pending:
        return None, (f"the {noun} it would change is no longer waiting for approval "
                      f"(it is now {item.status or 'without a status'})")
    return item, None


def pending_item(db, action):
    """The item a decision on this action would move, or None."""
    return locate_pending_item(db, action)[0]


def decision_api_pack():
    """The module pack the decision API belongs to (None if ungated)."""
    return module_manifest.pack_for_path(DECISION_API_PATH)


def decision_api_licensed(db, tenant):
    """Can this tenant's plan reach the decision API? Uses the plan gate's own
    rule (module_manifest.pack_licensed). A tenant with no TenantConfig row
    counts as licensed, so its items are held: fail closed."""
    config = db.query(models.TenantConfig).filter(
        models.TenantConfig.tenant_code == tenant).first()
    if config is None:
        return True
    return module_manifest.pack_licensed(
        decision_api_pack(), module_manifest.enabled_pack_ids(config.enabled_modules))


def awaiting_decision(db, model, rows):
    """{item_id: AgentAction} for the rows that are HELD. THE predicate.

    One agent_actions query for the whole batch, and none at all when no row is
    in its pending status (the common case on every dashboard poll). A model
    that agents do not propose raises KeyError: a caller bug, loudly.
    """
    kind = _KIND_FOR_MODEL[model]
    pending = PENDING[kind][1]
    candidates = [r for r in rows if r.status == pending and r.id is not None]
    if not candidates:
        return {}
    proposals = db.query(models.AgentAction).filter(
        models.AgentAction.tenant_code.in_({r.tenant_code for r in candidates}),
        models.AgentAction.ref_kind == kind,
        models.AgentAction.ref_id.in_([r.id for r in candidates]),
        models.AgentAction.status.in_(DECIDABLE),
    ).order_by(models.AgentAction.id).all()
    by_ref = {}
    for proposal in proposals:
        by_ref.setdefault(proposal.ref_id, []).append(proposal)
    held, licensed = {}, {}
    for row in candidates:
        # The SQL tenant filter narrows the scan; THIS is the rule: a proposal
        # holds only the item of its own tenant, even in a mixed batch.
        proposal = next((p for p in by_ref.get(row.id, ())
                         if p.tenant_code == row.tenant_code), None)
        if proposal is None:
            continue
        if row.tenant_code not in licensed:
            licensed[row.tenant_code] = decision_api_licensed(db, row.tenant_code)
        if licensed[row.tenant_code]:
            held[row.id] = proposal
    return held


def refuse_if_awaiting_decision(db, item):
    """The lock. Call in every handler that changes or deletes a proposable
    item, straight after its 404 lookup and before any write: a held item
    refuses every change and every delete, for every role, with 409."""
    proposal = awaiting_decision(db, type(item), [item]).get(item.id)
    if proposal is None:
        return
    noun = _NOUN[_KIND_FOR_MODEL[type(item)]]
    if is_expired(proposal):
        raise ApprovalDenied(
            409, f"This {noun} was proposed by the {proposal.agent} agent (agent action "
                 f"#{proposal.id}) and the proposal has expired. An Admin or Supervisor "
                 "must reject it in Approvals before it can be changed or deleted.")
    raise ApprovalDenied(
        409, f"This {noun} was proposed by the {proposal.agent} agent and is awaiting "
             f"approval (agent action #{proposal.id}). An Admin or Supervisor must approve "
             "or reject it in Approvals before it can be changed or deleted.")


def annotate_awaiting_decision(db, model, rows):
    """Set ``awaiting_approval`` on each row ({agent_action_id, agent, expired}
    or None) for the response models, from the same predicate as the lock."""
    held = awaiting_decision(db, model, rows)
    now = datetime.utcnow()
    for row in rows:
        proposal = held.get(row.id)
        row.awaiting_approval = None if proposal is None else {
            "agent_action_id": proposal.id, "agent": proposal.agent,
            "expired": is_expired(proposal, now)}
    return rows


def withdraw(db, action_id, tenant_code, now=None):
    """Withdraw a proposal that can no longer take effect. A compare-and-set:
    it changes the row only while it is still Proposed, so it can never
    overwrite a decision a concurrent request already recorded. Returns the
    number of rows changed (0 or 1). Does not commit."""
    return db.query(models.AgentAction).filter(
        models.AgentAction.id == action_id,
        models.AgentAction.tenant_code == tenant_code,
        models.AgentAction.status.in_(DECIDABLE),
    ).update({models.AgentAction.status: "Cancelled",
              models.AgentAction.decided_by: WITHDRAWN_BY,
              models.AgentAction.decided_at: now or datetime.utcnow()},
             synchronize_session=False)


def withdraw_orphaned(db, tenant=None, now=None):
    """Withdraw every Proposed action whose item is missing, moved, in another
    tenant, unrecorded or of an unknown kind -- the gate's own item check, run
    as a sweep. Live proposals (fresh or expired) and decided rows are never
    touched. Idempotent; does not commit.

    DELIBERATELY NOT CALLED at boot or from any script: rewriting production
    rows on deploy needs the founder's go-ahead. Until then orphans are
    withdrawn lazily, when an approver tries to decide one.
    """
    import tenancy  # lazy: tenancy pulls in auth, which approvals must not need at import

    bound = tenancy.current_tenant()
    if bound is not None and bound != tenant:
        # A bound tenant hides other tenants' items from the lookup, which would
        # make their live proposals look orphaned.
        raise ValueError("withdraw_orphaned must run unbound or bound to the tenant it sweeps")
    now = now or datetime.utcnow()
    query = db.query(models.AgentAction).filter(models.AgentAction.status.in_(DECIDABLE))
    if tenant is not None:
        query = query.filter(models.AgentAction.tenant_code == tenant)
    withdrawn = 0
    for proposal in query.order_by(models.AgentAction.id).all():
        if locate_pending_item(db, proposal)[0] is None:
            withdrawn += withdraw(db, proposal.id, proposal.tenant_code, now)
    return withdrawn


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
