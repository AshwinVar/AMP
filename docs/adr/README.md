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
| [0006](0006-machine-health-twin.md) | Machine Health twin (per-machine read-model) | Accepted |
| [0007](0007-read-models-projections.md) | Read-models: projections that answer one question | Accepted |
| [0008](0008-tenant-lifecycle-and-commercial-enforcement.md) | Tenant lifecycle & commercial enforcement | Accepted |
| [0009](0009-modularize-main-route-modules.md) | Modularize main.py by domain (route modules) | Accepted |
| [0010](0010-oee-money-story-unit-value.md) | The money story: one per-tenant £/good-unit rate | Accepted |
| [0011](0011-machine-identity-and-tenant-aware-ingest.md) | A machine is (tenant, site, name); telemetry is routed by topic | Accepted |
| [0012](0012-tenant-scoped-document-numbers.md) | Document numbers are unique per tenant, issued from a sequence | Accepted |
| [0013](0013-per-tenant-bill-of-materials.md) | A bill of materials belongs to a tenant, and lives in the database | Accepted |
| [0014](0014-canonical-oee-contract.md) | One canonical OEE contract, and an honesty rule | Accepted |
| [0015](0015-server-side-approval-gate.md) | The backend is authoritative about approvals | Accepted |
| [0016](0016-authenticated-live-websocket.md) | The live WebSocket authenticates before it accepts | Accepted |
| [0017](0017-oem-fleet-and-cross-tenant-equipment.md) | OEM fleet and cross-tenant equipment relationships | Accepted |

**Recommended order:** 0001 first (smaller, proves the pattern; events carry `tenant_code`), then 0002.

## Format

Each ADR: Context → Decision → Consequences (positive / negative) → Alternatives → Rollout. Keep them short. New records are numbered sequentially and never edited once `Accepted` — supersede with a new ADR instead.
