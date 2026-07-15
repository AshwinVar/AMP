# ADR 0007 — Read-models: projections that answer one question

- **Status:** Accepted (2026-07-15)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md), [0003 — AI as an event-consuming platform](0003-ai-as-event-consuming-platform.md), [0006 — Machine Health twin](0006-machine-health-twin.md)

## Context

The same shape kept recurring: the UI needs *one object that answers one question*, composed from data already spread across tables — "what does the factory need to know now?" (Mission Control), "what's the state of this machine?" (twin), "what has my agent fleet done?" (impact), "how's my factory and what needs me?" (pulse). Each was built the same way — a pure function that reads existing rows and projects them — but the pattern was never named, so every new one risked drifting: leaking tenant scope, duplicating logic in the frontend, or growing a premature table.

## Decision

Name and standardize the pattern. An **AI read-model** is a pure projection in the `ai/` package (`build_*`) that —

- **composes** signals from existing tables — and from other read-models — into one snapshot answering a single question;
- **adds no storage**: it recomputes on read, so it stays correct as the underlying data evolves (no migration, no sync job to keep consistent);
- is **tenant-scoped explicitly** for stamped tables (`event_log`, `agent_actions`) and rides the auto-scoping layer (ADR-0002) for the rest, so it is leak-proof regardless of the global scoping state;
- is **exposed 1:1** at a `GET` endpoint and rendered as a dashboard section;
- is **unit-tested in isolation** against an in-memory database.

Today's read-models: `insights` (the Mission Control feed — recommendations + notable events + proposed actions), `twin` (Machine Health — the per-machine snapshot and the single-machine detail cockpit), `impact` (agent-fleet ROI), and `pulse` (the owner's command header — *a read-model over the read-models*, composing `twin` + `impact`). Concrete engines (rule-based today) stay behind these functions, so a scorer can become an ML model or an LLM later without any consumer changing (ADR-0003).

## Consequences

**Positive**
- No schema churn: a new question ships as a function + endpoint + view, never a migration.
- Always-correct: a projection can't go stale — there is nothing to invalidate.
- Composable: `pulse` proves read-models compose into higher-order ones.
- Testable + swappable: each is a pure function over a session; the engine behind it is replaceable.

**Negative / risks**
- Recompute cost per request (e.g. `pulse` scores the whole fleet and reads the feed). Fine at SME scale; cache or materialize behind the same signature when a tenant grows.
- Fan-out queries (N machines, several tables) — watch for N+1; batch or precompute if it bites.

## Alternatives considered
- **Materialized projection tables kept in sync by subscribers:** premature; the read-model is correct and cheap now, and can be materialized later behind the identical API.
- **Composing in the frontend (multiple fetches, client-side joins):** leaks tenant-scoping decisions to the client and duplicates logic across views.
- **Fat, bespoke endpoints:** the projection logic becomes untestable and un-reusable.

## Rollout
1. **Retroactive:** this ADR records the pattern already established by `insights` (0003), `twin` (0006), and now `impact` + `pulse`.
2. **Going forward:** new "one object that answers one question" needs are built as read-models in `ai/`, tested in isolation, and exposed 1:1.
3. Later: a shared caching / materialization seam behind `build_*` when read cost warrants it — no consumer change.
