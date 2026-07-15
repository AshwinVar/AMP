# ADR 0006 — Machine Health twin (a per-machine read-model)

- **Status:** Accepted (2026-07-15)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0003 — AI as an event-consuming platform](0003-ai-as-event-consuming-platform.md), [0005 — Agent oversight](0005-agent-oversight.md)

## Context

The platform holds a lot per machine — live state, predictive risk, downtime history, open maintenance tasks, pending agent actions — but scattered across endpoints. There's no single "what's the state of this machine right now" object. The charter names a **digital twin** as a pillar; the existing "Digital Twin Command Center" is actually a spatial floor map, not a per-machine live model.

## Decision

Add a **Machine Health twin**: a per-machine read-model (`ai/twin.py`) that composes one live snapshot per machine from signals the platform already produces —

- **state** (status, utilization, downtime),
- a **health score** (0–100) derived from predictive risk (`ai.prediction`) with a band (Healthy / Watch / At risk / Critical),
- **recent downtime** (from `downtime_logs`),
- **open maintenance tasks** and **pending agent actions** targeting the machine.

Exposed at `GET /machine-health` (all machines) and rendered as a **Machine Health** dashboard view — a grid of live machine cards. It is a **read-model over existing tables** (no new storage), **tenant-scoped explicitly** (agent/event tables are stamped, not auto-scoped), and composes ADR-0003 (prediction) + ADR-0005 (agent actions).

## Consequences

**Positive**
- One pane per machine: the operator sees state + health + risk + what the agents want to do, together.
- Pure projection — no schema change, no migration; it recomposes on read and stays correct as the underlying data evolves.
- A foundation for a richer event-sourced twin later (live timeline, telemetry replay) without changing consumers.

**Negative / risks**
- Recomputes risk per request (N machines); fine at SME scale, cache later if needed.
- Distinct from the older "Digital Twin Command Center" (floor map) — named **Machine Health** to avoid confusion.

## Alternatives considered
- **Extend the floor-map "Digital Twin":** it's a spatial layout tool; bolting a health model on it muddies both.
- **A materialized twin table updated by subscribers:** premature; the read-model is correct and cheap now, and can be materialized later behind the same API.

## Rollout
1. **PR (this ADR):** `ai/twin.py` + `GET /machine-health` + the Machine Health dashboard view.
2. Later: per-machine detail with a live event timeline; optional materialization; twin-driven agent context.
