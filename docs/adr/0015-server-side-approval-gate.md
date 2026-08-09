# ADR-0015: The backend is authoritative about approvals

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0005 (agent oversight), ADR-0002 (tenant scoping)

## Context

ADR-0005 established that an AI agent proposes and a human disposes: an
`AgentAction` sits at `Proposed` until someone with authority approves it, and
only then does the purchase order advance or the maintenance task open. That is
the control that lets a customer switch the agent fleet on at all.

The control was enforced in the HTTP route. An adversarial probe asked whether
it was enforced anywhere else. One attempt per row, against master:

```
1. another tenant's Admin approves  : refused 404      PO stays Draft
2. role guard declared on the route : True             Admin/Supervisor
3. the SAME approve replayed        : refused 400      PO stays Approved
4. reject an already-approved one   : refused 400      PO stays Approved
5. ai.agents.apply_decision() direct: ACCEPTED         PO -> Approved
6. apply_decision() a SECOND time   : action Rejected, PO still Approved
7. a DELETED user approves          : ACCEPTED         PO -> Approved
8. a 400-day-old proposal           : ACCEPTED         PO -> Approved
```

Rows 1–4 held. Rows 5–8 did not, and each fails differently:

**5 — the guard lived only at the door.** `apply_decision()` carried no check of
its own. Every caller today happens to be guarded, so this was not exploitable;
but it means a money-moving action was safe only for as long as every future
caller remembered. That is the same shape as the ingest defect in ADR-0011.

**6 — the audit record could contradict the deed.** Calling `apply_decision`
twice flipped the `AgentAction` to `Rejected` while the purchase order stayed
`Approved` (the item-level `status == "Draft"` check held). The trail then says
a human rejected something the system went ahead with. An audit log that
contradicts reality is worse than none, because it is believed.

**7 — access could not be revoked.** `auth.get_current_user` decodes the JWT and
performs no database lookup, and there was no `is_active` column to consult if it
did. A user deleted from the database still approved a purchase order. The only
remedy was to wait for their token to expire.

**8 — a recommendation never went stale.** "Reorder steel, stock is low" is
evidence about a moment. Nothing bounded how long that moment stayed actionable.

## Decision

**One gate — `backend/approvals.py` — called by both the route and
`ai.agents.apply_decision`.** `authorise(db, action, actor, decision)` answers a
single question, *may this person make this decision on this action right now*,
and raises a precise `HTTPException` when the answer is no.

Four checks, in this order:

| | Check | Refusal |
|---|---|---|
| 1 | the action belongs to the actor's tenant | 404 |
| 2 | the action is still `Proposed` | 400 |
| 3 | the action has not passed its expiry | 409 |
| 4 | the approver still exists, is active, is in this tenant, holds an approving role | 401 / 403 / 404 |

**The order is itself a guard.** Tenant is verified first, so a caller from
another tenant cannot distinguish "already approved" (400) from "expired" (409).
Outside your tenant there is exactly one answer, and it is 404.

**The actor is re-verified against the database, and deliberately here.** Putting
a `SELECT` in `get_current_user` would add a query to every request in the
product to defend an action that happens a handful of times a day. This is the
boundary where a stale credential actually costs money and material.

**Schema.** `users.is_active` (NOT NULL, default TRUE) makes revocation
possible. `agent_actions.expires_at` (nullable) bounds staleness; NULL means
"fall back to `DEFAULT_TTL_DAYS` (7) from `created_at`", never "never expires".

**Freshness is evaluated at decision time**, not by a background job.
`expire_stale()` exists so the queue a human sees does not fill with proposals
they cannot action — but the gate does not depend on it having run. A guard that
needs a job to have run is off whenever the job is.

**Auto-approval is still approval.** `AgentPolicy` lets a tenant declare in
advance that an agent may act unattended. That is a human decision made earlier,
so the policy path skips check 4 — there is no human to verify — and still
passes 1–3. An expired proposal is not auto-approved either.

## Consequences

**Positive.** Every bypass in the probe now refuses, verified on PostgreSQL 18.3
(37 assertions) and by a 25-mutation matrix in `mutate_approval_gate.py` — every
mutation caught, including removing each check, reading the role from the token
instead of the database, moving the tenant check after the state check, and
making `is_active` nullable. A demotion or a suspension takes effect on the next
approval attempt rather than at token expiry.

**Negative.** One extra `SELECT` per decision. Callers reaching
`apply_decision` directly must now pass an `actor` or state `require_actor=False`
explicitly — two test suites encoded the unguarded behaviour and were updated to
seed real users, which is what the world actually looks like.

**Not addressed.** The gate authenticates *who decides*; it does not add
idempotency keys to the decision endpoint. Replay is already refused by check 2
(a decided action is not `Proposed`), so a duplicate request is a 400 rather
than a second effect — an idempotency key would change the status code, not the
outcome.

## Alternatives considered

**Verify the user on every request in `get_current_user`.** Correct, and the
cost falls on all 282 routes to defend one. Revisit if short-lived tokens with
a revocation list are introduced.

**Short-lived tokens instead of a database check.** Bounds the damage window but
does not close it, and shortening it enough to matter means re-authenticating
operators during a shift.

**Backfill `expires_at` for historic rows.** Either expires a queue of
legitimate pending proposals on deploy day, or invents a future date for actions
whose context is gone. NULL with an age-based fallback avoids both.

**Delete `_check_tenant` as redundant** (check 4 also compares tenants). Its
mutation is caught precisely because it is not redundant: without it, state and
freshness are evaluated before the tenant is established, and their status codes
leak across the boundary.

## Rollout

Migration `0005_approval_gate` adds both columns and is reversible; verified on
PostgreSQL 18.3 against a *populated* users table, which is the case that
matters — existing users come out active, so no login breaks on deploy.
