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

## Addendum (2026-09-17): an agent proposal holds its item until it is decided

### What was measured

The gate above guards the *action*. Nothing guarded the *item*. On master
(794d483), through the real handlers:

```
PATCH /maintenance/tasks/{id}  {status: Open}       as Operator : ACCEPTED  task Open, action still Proposed
PATCH /purchase-orders/{id}    {received_quantity}  as Operator : ACCEPTED  stock booked against an unapproved draft
PATCH /escalations/{id}        {status: Resolved}   as Operator : ACCEPTED
DELETE on all three                                  as Admin    : ACCEPTED  action left pointing at nothing
then approve / reject                                             : ACCEPTED  "Approved"/"Rejected" recorded; item never moved
```

The last row is failure 6 of the original probe, reached by a different road:
an audit record that contradicts what the system did. `test_agent_item_lock.py`
reproduces all of it (34 failures on master).

### Decision

1. **A fifth check, last: the item.** `authorise` refuses (409,
   `ProposalWithdrawn`) unless the action's item still exists *in the action's
   tenant* and is still in its pending status (`approvals.PENDING`: task
   `Proposed`, PO `Draft`, escalation `Proposed`). It runs after the actor check,
   so an outsider still gets 404 and a non-approver 403. The row is read
   `FOR UPDATE` and re-read into the session (`populate_existing`), so a
   concurrent decision waits and then sees the item moved -- even when its
   session already held the item. `apply_decision` is unchanged: its transitions
   now always move the item in the same transaction as the decision.
   *Where that is proven:* only PostgreSQL can hold a row lock, so the race is
   exercised only there. `test_agent_item_lock.py` section 11 overlaps approve
   and reject (and a double approve with the item already loaded) with barriers
   inside the item check; with `FOR UPDATE` removed every race answered 200 twice
   and the later commit overwrote the first decision. On SQLite that section
   prints SKIP; `verify_pg_approvals.py` runs it on a local scratch PostgreSQL
   and requires it not to skip. The re-read is also pinned sequentially on both
   engines (section 7).
2. **The route withdraws what can no longer take effect.** On
   `ProposalWithdrawn` the decide route rolls back and runs a compare-and-set
   (`UPDATE agent_actions SET status='Cancelled', decided_by='system-withdrawn'
   WHERE id AND tenant AND status='Proposed'`), answering 409. If the update
   changes nothing, a concurrent request decided first, and the answer is 400
   `Already <status>` -- the earlier decision is never overwritten. A
   withdrawal that changed the row also records who caused it (point 11).
   Orphans are withdrawn **lazily**, when an approver tries to decide one.
   `approvals.withdraw_orphaned` exists and is tested, but nothing calls it at
   boot or from a script: rewriting production rows on deploy waits for the
   founder's go-ahead.
3. **The lock.** `approvals.refuse_if_awaiting_decision` sits in the six
   PATCH/DELETE handlers straight after the 404 lookup and refuses *every* change
   and delete of a held item with 409, for every role including Admin. Order in
   each handler: role 403, tenant lookup 404, lock 409, validation 400. The whole
   row is locked, not just `status`: `received_quantity` books stock, and
   `downtime_minutes`/`completed_date` record work nobody approved; a per-field
   allowlist would drift as schemas change.
4. **What "held" means** (one implementation, `approvals.awaiting_decision`,
   batched, zero queries when nothing is pending): the item is in its pending
   status **and** a `Proposed` AgentAction of the same tenant, kind and id points
   at it, the item having been written no later than that action (point 10),
   **and** the tenant's plan can reach the decision API (the pack
   `module_manifest` maps `/agent-actions` to is in `TenantConfig.enabled_modules`,
   using the plan gate's own rule; no config row counts as licensed, so the lock
   fails closed). Expiry is ignored. Human look-alikes (pending status, no
   Proposed action) and tenants without the Intelligence Pack keep today's
   behaviour: their items stay editable, because nobody there can reach
   Approvals to release a hold. *Corrected in verifier round 2:* this point
   first said that for such tenants "nobody there can record a decision, so no
   record can contradict the item". That held only while the plan stayed put --
   an upgrade makes a decision reachable over whatever was edited meanwhile.
   Point 9 closes it.
5. **Freshness now gates approve only.** Check 3 no longer applies to reject.
   The reason is the lock: an expired proposal still holds its item, and without
   an exit the item would be frozen for good. Rejecting is that exit: it needs a
   database-verified Admin/Supervisor of the tenant, it is recorded with their
   name, and it moves the item to `Cancelled` rather than into effect. Approving
   an expired proposal, by a human or by policy, stays 409 -- acting on stale
   evidence is what check 3 exists to stop. `expire_stale` is unchanged. The 409
   says the proposal "can only be rejected, which releases the item it holds":
   the old "ask the agent to re-evaluate" pointed nowhere, because no agent can
   propose again while the item is held (every agent dedup counts the pending
   status as open). Each action in `GET /agent-actions` carries `expired`
   (undecided and past expiry), and the Approvals inbox shows the same sentence
   and does not offer Approve on it.
6. **Contract.** No schema change or migration. The task, PO and escalation
   list responses gain an additive
   `awaiting_approval: {agent_action_id, agent, expired} | null`. The screens read
   only that flag -- never status strings -- to disable controls, hide Delete and
   say who decides. A PATCH response carries the field too, and it is null by
   construction: a PATCH succeeds only on a row that is not held, and nothing may
   move a row INTO its pending status by hand (point 7). The PATCH handlers used to
   annotate it anyway; review round 3 found that call could never return anything
   but null, so it was removed rather than tested.
7. **Only an agent puts an item into its pending status** (verifier round 1).
   Lazy withdrawal leaves orphans: an item moved while its action stayed
   `Proposed`. Measured through `main.app`: an Operator PATCHed such an item's
   content (200, not held), then PATCHed `{status: <pending>}` (200, now held),
   and a Supervisor's approval recorded `Approved` under the agent's name for
   content the agent never proposed. `approvals.refuse_manual_pending_status`
   now answers 400 to a PATCH that moves a row *into* its pending status and to
   a POST that creates one there, in the three PATCH and three create handlers
   (after the lock, so a held item still answers 409). Re-sending the status a
   row already has is not a move and passes, so human look-alikes stay
   editable. It applies on every plan: the value is `systemOnly` in
   `status-vocab.json` for everyone, and the lock's licence clause is about who
   can *decide*, not who may write agent statuses.
8. **The Approvals list pages.** With the lock, the list is the only way out
   for a held item, and it answered one capped page (300, newest first):
   measured, a held Draft PO older than 300 newer proposals was in no response
   while its PATCH answered "decide it in Approvals". `GET /agent-actions` now
   takes `limit` (1-300) and `offset` (>= 0), breaks `created_at` ties by id so
   pages neither repeat nor skip, and the inbox and the activity log load older
   pages on request (`frontend/lib/agent-actions.ts`).
9. **A plan change cannot re-arm a proposal a human changed** (verifier round
   2). Measured through `main.app`: a growth-plan tenant (no Intelligence Pack,
   so nothing held); the maintenance agent proposed a Critical task; an
   Operator PATCHed it to priority Low, assigned `nobody`, planned 2030, new
   notes, re-sending status `Proposed` (200: unheld, and re-sending the pending
   status is not a move); the founder's `apply-plan enterprise` made the task
   held; a Supervisor's approval answered 200 and recorded `Approved` for "Open
   a Critical maintenance task" over content the agent never proposed. The same
   edits were open on escalations and Draft POs. This is point 7's defect by
   another road. Now `refuse_if_awaiting_decision(db, item, actor)`, when a live
   proposal points at the item but the item is unheld *only* because of the
   licence clause, lets the PATCH or DELETE through as before and withdraws the
   proposal in the **same transaction** (`approvals.withdraw`, compare-and-set;
   a PATCH the handler refuses later rolls the withdrawal back with it),
   recorded against the user who made the change. A proposal nobody touched is
   still re-armed by an upgrade and decided normally. Rejected alternatives:
   refusing at decision time an item edited after its proposal needs an
   `updated_at` column the three tables do not have; withdrawing at plan-change
   time would have to live in every path that changes `enabled_modules`, and
   would also end proposals nobody edited.
10. **A proposal names only a row written no later than itself** (verifier
    round 2). Measured on SQLite through `main.app`: a Draft PO (id 1) and its
    proposal #2 "Draft a PO for Steel (90 kg)"; purchase orders bulk-deleted,
    as `reseed_inventory.py` does (agent actions untouched); a new Copper Draft
    PO took id 1 again with proposal #3. `GET /purchase-orders` showed it held
    by #2, and approving #2 answered 200 and moved the Copper PO, orphaning #3.
    SQLite reuses the highest deleted rowid; PostgreSQL sequences do not (the
    verifier's PostgreSQL run of the same probe answered 409 and withdrew #2),
    but an explicit id or a restarted sequence would. Every agent flushes its
    item before recording the proposal that names the item's id, so
    `approvals._written_before`
    (`item.created_at <= action.created_at`) now applies in both
    `awaiting_decision` and `locate_pending_item` -- and so in the gate, the
    lock, the list flag and `withdraw_orphaned`. A row or proposal with no
    `created_at` cannot be shown to be the proposal's: not held, and a decision
    on it is withdrawn rather than recorded. Test fixtures now write items
    before their proposals as the agents do; `test_agents.py` had added both in
    one flush, where SQLAlchemy inserts `agent_actions` first, and passed only
    because this Windows machine's clock ticks once a millisecond (checked by
    re-running every backend suite with a clock that gives each insert a
    distinct, later reading, as a microsecond clock on Linux CI would).
11. **Every withdrawal records who caused it** (verifier round 2). The route
    withdrew with `decided_by='system-withdrawn'` and nothing else; only the
    request log line named the approver. `approvals.withdraw(..., by, reason)`
    now stages an `AuditLog` row (`withdraw_agent_action`, entity
    `agent_action` #id, the reason, the action's tenant) through
    `platform_routes.add_audit`, in the withdrawal's own transaction and only
    when the compare-and-set changed the row. `by` is the approver on the decide
    route, the editor on point 9's path, and `system` for `withdraw_orphaned`.
    `decided_by` stays `system-withdrawn`: nobody *decided* it.
12. **Mission Control is a decision surface too** (verifier round 2). It
    offered Approve on expired proposals and, after a refusal, kept a
    withdrawn proposal's buttons until the 30-second poll. Each `/insights`
    action now carries `expired` (`approvals.is_expired`; `null` for
    recommendations and events), the screen disables Approve with the inbox's
    sentence, and it reloads after a refused decision.

### Guards

`test_agent_item_lock_guard.py` walks `main.app.routes` (every PATCH/PUT/DELETE,
and every POST on an item-addressed path, under the three URL families must call
the lock; the six known handlers must be found by name), AST-scans the route modules for any write to a loaded
task/PO/escalation not preceded by the lock, checks the `PENDING` table against
what agents propose and what `apply_decision` moves, and probes itself by
removing and moving the real call in memory; it also requires
`refuse_manual_pending_status` in every PATCH/PUT of the three families and in
the three create handlers, with its own in-memory probe. The lock's call now
passes the request's user (point 9), and the probes match that call.
`mutate_approval_gate.py` grows to 77 mutations across six suites
(`test_insights.py` joined for point 12), including:
the item check removed or moved before the actor, the compare-and-set losing its
status predicate, the reject exemption flipped either way, the lock ignoring
status, decided actions, kind, tenant or licence, the lock removed from a
handler, the pending-status rule dropped or removed from any of its six
handlers, the list ignoring its offset or cap or tie order, the `expired` flag
wrong either way, and the row lock removed three ways (`lock=False`,
`FOR UPDATE` dropped, re-read dropped). Round 2 added: an edit on a plan
without Approvals leaving the proposal live, that withdrawal committing on its
own outside the edit's transaction, a held item's write withdrawing instead of
refusing, either withdrawal path recording nobody, no audit row, an audit row
when the compare-and-set lost, the audit row losing its tenant, the lock or the
gate accepting a newer row with the item's id, an unknown `created_at` counting
as earlier, a same-instant item not counting, and Mission Control never
reporting `expired`. Two of them only PostgreSQL can judge
(`FOR UPDATE` dropped; a negative offset reaching the database, which SQLite
reads as 0): on SQLite the harness reports them `pg-only`, never `caught`, and
`--postgresql` runs them with the other row-lock mutations against PostgreSQL,
where `verify_pg_approvals.py` requires all four caught. *Verifier round 2:*
CI did not run any of that -- the PostgreSQL job never called
`verify_pg_approvals.py` -- so the race was proven only on a developer's
machine. The migration gate job now runs it against its `postgres:18` service
(`pg_scratch` falls back to the job's `DATABASE_URL` when there is no `.env`).

### Review round 3 (2026-09-18)

An adversarial review of the rebased branch confirmed seven findings. Each is
fixed with a test that was run against the unfixed code and failed there:

- **Two more screens decide proposals.** The agent drawer (Agent Activity) and the
  machine cockpit drawer offered Approve on expired proposals and, after a refused
  decision, kept offering Approve and Reject for a proposal the server had just
  withdrawn. Both now read `expired` from their payloads (`ai/roster`,
  `ai/twin._open_actions`, from `approvals.is_expired`), grey out Approve with the
  same sentence, and reload after a refusal, like the inbox and Mission Control.
- **The route inventory skipped POST.** A new `POST /escalations/{id}/resolve` that
  resolved through a helper passed the whole guard, and through the real handler an
  Operator resolved a held escalation with 200. POST on an item-addressed path is
  now inventoried; a probe shows that route caught, and green once it calls the lock.
- **The withdrawal's commit was untested.** Every assertion read back through the
  handler's own session, which sees its own uncommitted writes. Section 17 rolls
  that session back and still finds the withdrawal and its audit row.
- **The expired 409 check was vacuous.** It asserted only "reject", which the
  generic held message also says. It now requires "has expired" and no "approve".
- **A false cause on the record.** An item with no creation time was withdrawn as
  though a newer row had reused its id. Fail-closed stays (it cannot be shown to be
  the proposal's), but the refusal and the audit row now say what is actually true.

### Not addressed (open founder questions)

- Agents propose for tenants without the Intelligence Pack, who cannot decide.
  Their items stay unlocked suggestions until that is decided. Since point 9 a
  change or delete there withdraws the proposal, so choosing later to hold
  those items (or upgrading such a tenant) re-arms only proposals nobody
  touched; whether agents should propose there at all is still open.
- Whether an unattended proposal should be cancelled automatically on expiry.
- Whether "approve, then edit" is acceptable, or supervisors need a recorded
  "edit proposal" flow.
- Whether to withdraw historic orphans in one pass at deploy
  (`withdraw_orphaned`), and what to do about decisions recorded before
  2026-09-17 against items that had already moved (they are not rewritten).
