# ADR 0003 — AI as an event-consuming platform

- **Status:** Accepted (2026-07-14)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0001 — Domain event bus](0001-domain-event-bus.md), [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md)

## Context

Today "AI" is **scattered scripts**, not a platform:

- `predictive_engine.py` — rule-based failure-risk scoring per machine.
- `ai_copilot.py` — an LLM copilot (off unless `ANTHROPIC_API_KEY` is set) plus rule-based recommendations.
- `analytics_engine.py` — OEE, shift KPIs, smart alerts.

Each is a standalone module called ad-hoc from HTTP handlers, only when a user asks. There is no shared interface, and none of it **consumes the event stream** — so the `event_log` (ADR-0001) accumulates the factory's history but nothing turns it into intelligence.

The vision — an **AI OS for manufacturing** — requires the opposite: AI as a **platform** every business module consumes through stable interfaces, and one that **reacts to factory events in real time** (production completed, downtime, quality failed, inventory low). The charter is explicit: *do not implement AI as isolated helper scripts; design AI as a platform; every business module consumes AI services.*

## Decision

Introduce a first-class **`backend/ai/` package** — the AI platform — with clean service interfaces.

- **Capabilities, behind one package.** Start with what already exists rule-based — **Prediction** (predictive maintenance), **Recommendations**, **Copilot** — and let the vision's other capabilities (Optimization, Reasoning, Scheduling, Vision, Knowledge, Agents) slot in behind the same package as they're built. Consumers import `ai.<service>`, never a specific script.
- **AI consumes the event stream.** AI services **subscribe to domain events** (ADR-0001's bus). Factory events become the triggers that generate predictions / recommendations / insights — not just on-demand HTTP calls. The `event_log` becomes the substrate AI reasons over (the compounding moat).
- **Strangler, not rewrite.** Wrap the existing `predictive_engine` / `ai_copilot` / `analytics_engine` logic behind the platform interfaces. Behaviour is preserved; callers migrate to the interface incrementally.
- **Rule-first, LLM-optional.** Every AI service works rule-based with no external dependency; an LLM (Anthropic, gated on `ANTHROPIC_API_KEY` — the existing pattern) *enhances* output when available. AI stays always-on and cheap; the intelligence improves without changing consumers.
- **Tenant-aware.** AI inputs (events) carry `tenant_code` (ADR-0001); AI outputs (predictions, recommendations, insights) are stored and served **per-tenant** (ADR-0002). No cross-tenant intelligence.
- **Reports → Insights.** AI-generated insights are the product surface; static reports become the fallback.

## Consequences

**Positive**
- AI becomes a platform consumed through stable interfaces — low coupling, testable, swappable implementations (rules → ML → LLM) without touching callers.
- AI reacts to the factory in real time, the path toward autonomous operations and agents.
- The `event_log` turns into a compounding intelligence asset.
- Rule-first keeps it always-on and offline-safe; LLM/ML slot in behind the interface.

**Negative / risks**
- Adds a package + indirection; existing direct calls migrate to the interface over time.
- Event-driven AI needs more events published than exist today (only `ProductionCompleted` so far) — added incrementally.
- LLM cost/latency must be designed in from the start: gated, async, cached, and never on the request's critical path.

## Alternatives considered

- **Keep the scattered scripts (status quo):** simplest, but blocks the platform vision; every consumer re-wires to each script and nothing consumes events.
- **One "AI god-module":** couples all capabilities; hard to test/extend; violates high cohesion.
- **LLM-first (require a key):** expensive, latency-bound, offline-fragile. Rule-first + LLM-enhance is the pragmatic path.
- **A separate AI microservice now:** premature infrastructure at current scale. The package interface lets us extract a service later without changing consumers.

## Rollout (incremental)

1. **PR #6 (first step):** create the `ai/` package + a base interface; move **Prediction** behind `ai/prediction.py` (wraps `predictive_engine`, behaviour-preserving); wire one **event subscriber** — on `ProductionCompleted`, the AI layer generates a recommendation/insight and persists it (tenant-stamped) — proving AI consumes the event stream. Verified with unit tests + the boot/`POST /login` smoke.
2. Move **Recommendations** + **Copilot** behind the platform; publish more domain events (downtime, quality, inventory-low) so AI reacts to them.
3. Add an **Insights** read-model (AI outputs surfaced per-tenant) — the Mission Control feed.
4. Later: **Optimization**, **Reasoning**, **Scheduling**; then **Agents** (observe → reason → recommend → act) on top of the same platform.

## Sequencing note

ADR-0003 builds directly on **ADR-0001** (events to consume) and **ADR-0002** (tenant boundaries for AI I/O). It is the first *capability* ADR after the two foundation ADRs — the point where AMP starts becoming intelligent, not just isolated.
