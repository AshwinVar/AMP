# ADR 0004 — AI agents: act on the stream

- **Status:** Accepted (2026-07-14)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0001 — Domain event bus](0001-domain-event-bus.md), [0003 — AI as an event-consuming platform](0003-ai-as-event-consuming-platform.md)

## Context

The AI platform (ADR-0003) **observes** the event stream and **advises** — it writes recommendations. Everything it produces is still a suggestion a human must action. The vision calls for **autonomy**: software that closes the loop by taking bounded actions on its own.

We have the pieces to take the first safe step: a clean event stream (ADR-0001), per-tenant boundaries (ADR-0002), and a Prediction service that scores machine failure risk (ADR-0003). The missing concept is an **agent** — something that observes, reasons, and *acts*.

## Decision

Introduce **agents** in the AI platform: an agent subscribes to the event stream and takes a **bounded, autonomous action** rather than only recommending.

The first is the **Maintenance agent** (`ai/agents.py`). On a maintenance-relevant event (production completed, downtime started) it reassesses the machine via `ai.prediction`, and if risk is **Critical** it **opens a maintenance task** — a real work item — instead of only recommending one.

Guardrails that make autonomy safe here:

- **Bounded action.** The agent only *creates* an internal `MaintenanceTask`. It never deletes, never mutates external systems, never touches another tenant.
- **High bar.** It acts only on **Critical** risk (score ≥ 75); merely elevated risk stays advisory (a recommendation).
- **Idempotent.** One open auto-task per machine — a repeat trigger never duplicates.
- **Auditable & reversible.** The task is tagged `Predictive (auto)` and its notes cite the risk that opened it; a human can close or reassign it.
- **Tenant-scoped.** Inputs and the created task are the event's tenant (ADR-0002).

This gives a clear **autonomy ladder**: elevated risk → *recommendation* (advisory); critical risk → *agent opens a task* (action).

## Consequences

**Positive**
- AMP crosses from advising to acting — the foundation for autonomous operations.
- The pattern is reusable: future agents (scheduling, purchasing, quality) subscribe to the same stream with their own bounded actions.
- Safe by construction: bounded, high-threshold, idempotent, reversible, tenant-scoped.

**Negative / risks**
- Autonomous writes need trust; mitigated by the guardrails and by keeping the action internal and reversible.
- More consumers on `ProductionCompleted` / `DowntimeStarted`; dispatch stays synchronous and in-transaction for now (ADR-0001).
- Threshold tuning (what counts as "Critical") will need review as real data accrues.

## Alternatives considered

- **Stay advisory-only:** safest, but never realises the autonomy in the vision.
- **Act on every recommendation:** too noisy and too bold — opens tasks for merely-elevated risk.
- **A human-approval queue before acting:** valuable later, but premature; the action here is already internal, bounded and reversible, so a queue adds friction without much safety today.
- **A separate agent runtime/scheduler:** unnecessary infrastructure now; a subscriber on the existing bus is enough and extracts cleanly later.

## Rollout (incremental)

1. **PR #13 (this ADR):** the Maintenance agent — opens a task on Critical risk, idempotent, tenant-scoped; wired to `ProductionCompleted` and `DowntimeStarted`. Unit-tested + boot/login smoke.
2. Surface auto-opened tasks in Mission Control (they already flow through CMMS).
3. More agents (scheduling, purchasing) on the same pattern.
4. Later: an approval/oversight layer and an agent activity log as autonomy widens.
