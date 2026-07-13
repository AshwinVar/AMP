# ADR 0001 — Introduce a domain event bus

- **Status:** Accepted (2026-07-12)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md)

## Context

AMP's business logic currently lives inside FastAPI HTTP handlers in `backend/main.py` (~3,600 lines). Several cross-domain reactions are **already event-like, but hardcoded inline and point-to-point**:

- Work order marked `Completed` → deduct raw material + add finished goods (the `PART_BOM` movement).
- Machine → `Breakdown` → downtime log → escalation.
- Inventory at/below reorder level → reorder escalation.

The product vision (an AI OS for manufacturing) needs **many consumers** of these same facts: prediction/AI, analytics, the digital twin, notifications, scheduling, and future integrations. Wiring each new consumer directly into the handlers multiplies coupling and makes the monolith harder to change. Separately, the **longitudinal event history is itself the strategic data asset** for AI/ML and reasoning — we should start capturing it now.

## Decision

Introduce a lightweight **in-process domain event bus** behind an explicit `publish`/`subscribe` interface.

- Model domain events as **immutable, versioned, tenant-scoped** records: `ProductionCompleted`, `MachineStateChanged`, `DowntimeStarted`, `DowntimeEnded`, `QualityFailed`, `InventoryLow`, `MaintenanceScheduled`, `AiRecommendationGenerated`, `ProductionScheduleUpdated`, …
- **Publish at the decision points that already exist**, and move today's inline reactions into **subscribers**. Behaviour stays identical.
- **Persist every event** to an append-only `event_log` (tenant, type, version, payload, `occurred_at`, correlation id) — the substrate for analytics / AI / twin later.
- **Keep transport behind the interface.** Start in-process (synchronous within the request, or a background task). The interface lets us move to an outbox + broker (NATS / Kafka / Redis Streams) later **without touching publishers or subscribers**.

## Consequences

**Positive**
- Decouples producers from consumers — new capabilities (twin, AI, notifications) *subscribe* instead of editing handlers.
- Makes the implicit event-driven behaviour explicit and **testable** (assert an event fired; test handlers in isolation).
- Starts accumulating the **event history (data moat)** immediately.
- Opens an async-first path without forcing async everywhere today.

**Negative / risks**
- Adds indirection. A failed/mis-subscribed handler must **not** break the originating transaction → define delivery semantics (outbox + at-least-once later; best-effort with logging initially) and keep handlers **idempotent**.
- Ordering/consistency guarantees are deferred until we move off in-process.
- Events become a **public contract** → versioned, additive changes only.

## Alternatives considered

- **Keep direct calls (status quo):** simplest, but coupling grows with every consumer and blocks the vision.
- **Go straight to Kafka/NATS:** premature infrastructure and ops burden at current scale (one pilot). The interface lets us defer it until the streaming phase.
- **DB triggers / CDC (e.g. Debezium):** powerful for later analytics but couples events to the schema and is heavy to start; revisit when we build the streaming event store.

## Rollout (incremental, behaviour-preserving)

1. Add `EventBus` interface + in-process implementation + `Event` base type + `event_log` table.
2. Migrate **one** reaction first: WO-complete → publish `ProductionCompleted`; move BOM movement into a subscriber. Verify parity with `backend/e2e_sim.py`.
3. Migrate breakdown→escalation and low-stock→reorder the same way.
4. New consumers (analytics rollups, notifications) subscribe — no handler edits.

**Backward compatibility:** API contracts unchanged; only internal wiring moves. The GMATS pilot is unaffected.
