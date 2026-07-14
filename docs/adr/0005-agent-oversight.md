# ADR 0005 — Agent oversight: propose, log, approve

- **Status:** Accepted (2026-07-14)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0004 — AI agents: act on the stream](0004-ai-agents-act-on-the-stream.md)

## Context

ADR-0004 gave AMP agents that *act* — the Maintenance agent opens a task, the Reorder agent drafts a PO. As autonomy grows, two gaps appear:

1. **No oversight.** Actions happen with no human sign-off and no audit trail. That's fine for a task or a draft PO; it will not be fine as agents touch money, suppliers, or schedules.
2. **Agents only fire from HTTP.** `InventoryLow` is published from the inventory endpoint, but the sim's BOM consumption decrements stock *directly*, so production never triggers a reorder.

## Decision

Introduce an **agent oversight layer**: agents **propose**, a human **approves**, and every proposal is **logged** — unified in one record, the **`AgentAction`**.

- **Propose, don't act.** An agent creates its item in a *pending* state (maintenance task `Proposed`, PO `Draft` — never live) and records an `AgentAction` with status `Proposed`. Nothing an agent does takes effect until a human says so.
- **`AgentAction` = audit log + approval queue.** One row per autonomous action: which agent, what it wants to do, the item it created, severity, status, and who decided when. It is the durable record of everything the agents do.
- **Approve / reject.** `POST /agent-actions/{id}/approve` advances the item (task → `Open`, PO → `Approved`); `reject` cancels it. Both stamp `decided_by`/`decided_at`. Decisions are idempotent (only a `Proposed` action can be decided).
- **Surface in Mission Control.** The feed shows `Proposed` actions with **Approve / Reject** buttons — oversight where the operator already looks.
- **Close the trigger gap.** The BOM consumption path publishes `InventoryLow` when production drops stock to its reorder level, so the Reorder agent fires from real production, not just manual transactions.
- **Tenant-scoped throughout.** `AgentAction` is tenant-stamped and filtered explicitly (like `event_log`); every query and decision is scoped to the caller's tenant (ADR-0002).

## Consequences

**Positive**
- A human is in the loop for every autonomous action, with a full audit trail — the trust foundation for higher-stakes agents.
- One uniform model (`AgentAction`) for logging, approval, and Mission Control surfacing — agents and UI don't each reinvent it.
- Agents now react to production, not only to API calls — the loop is self-driving.
- Reversible by construction: reject cancels, approve is explicit, nothing auto-goes-live.

**Negative / risks**
- An approval step adds friction; acceptable now (actions are low-stakes) and the point of the gate. A future auto-approve policy for trusted low-risk actions can relax it.
- `InventoryLow` is now published from within a `ProductionCompleted` handler (a nested publish); the in-process bus handles it synchronously, and there is no cycle (inventory handlers don't publish production events).
- One new table (`agent_actions`), created at startup by `create_all`.

## Alternatives considered

- **Keep acting directly, add only a log:** an audit trail without control; misses the "hold before live" requirement.
- **Separate log table and approval flag on each item:** duplicates the concept across item types; `AgentAction` unifies it.
- **A full workflow engine / approval service:** premature; a status field and two endpoints suffice at this scale.

## Rollout

1. **PR #16 (this ADR):** `AgentAction`; agents propose + log; approve/reject endpoints; Mission Control Approve/Reject; BOM → `InventoryLow`. Unit-tested + boot/login smoke.
2. Later: auto-approve policy for trusted low-risk actions; an agent-activity view beyond the live feed; per-agent enable/disable.
