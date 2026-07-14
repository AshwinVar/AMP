# Architecture Decision Records

ADRs capture significant architectural decisions for AMP — the context, the decision, and its consequences — so the *why* survives as the codebase evolves toward an AI operating system for manufacturing.

Guiding principles: incremental (strangler) evolution, never a rewrite; backward compatibility (the GMATS pilot stays live); event-driven backbone; multi-tenant by construction. See the target architecture and migration path diagrams shared alongside these records.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-domain-event-bus.md) | Introduce a domain event bus | Accepted |
| [0002](0002-tenant-scope-core-domain.md) | Tenant-scope the core domain | Accepted |
| [0003](0003-ai-as-event-consuming-platform.md) | AI as an event-consuming platform | Accepted |
| [0004](0004-ai-agents-act-on-the-stream.md) | AI agents — act on the stream | Accepted |
| [0005](0005-agent-oversight.md) | Agent oversight: propose, log, approve | Accepted |

**Recommended order:** 0001 first (smaller, proves the pattern; events carry `tenant_code`), then 0002.

## Format

Each ADR: Context → Decision → Consequences (positive / negative) → Alternatives → Rollout. Keep them short. New records are numbered sequentially and never edited once `Accepted` — supersede with a new ADR instead.
