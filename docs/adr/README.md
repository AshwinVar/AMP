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
| [0018](0018-migrations-run-before-the-application-serves.md) | Migrations run before the application serves | Accepted |
| [0019](0019-factory-controlled-machine-claim.md) | Factory-controlled machine claim and installation assignment | Accepted |
| [0020](0020-amp-native-ai.md) | AMP-native AI: standard-library models, pinned artifacts, per-tenant learning consent | Accepted |
| [0021](0021-agreed-downtime-attribution.md) | Agreed downtime attribution for service contracts | Accepted |
| [0022](0022-copilot-typed-tools-evidence-and-grounding.md) | The Copilot answers through typed, authorized AMP tools, with evidence; a model may only word it | Accepted |
| [0023](0023-self-hosted-model-behind-an-earned-switch.md) | A self-hosted model behind the Copilot, switched on only when it has earned it | Accepted |
| [0024](0024-factory-command-centre.md) | The Factory Command Centre ranks problems by what they cost, and never totals them | Accepted |
| [0025](0025-root-cause-explorer.md) | The Root-Cause Explorer separates what AMP measured from what has a reason | Accepted |
| [0026](0026-production-risk-radar.md) | The Risk Radar states a rule and a measurement, never a probability | Accepted |
| [0027](0027-machine-health-explained.md) | Every health score shows its arithmetic, and the trained model is shown as a model | Accepted |
| [0028](0028-daily-factory-brief.md) | The Daily Factory Brief, and the section that says what AMP could not see | Accepted |
| [0029](0029-closed-loop-outcomes.md) | Did it help? Measured, and never claimed as a cause | Accepted |
| [0030](0030-shortage-impact.md) | A shortage is sized from the tenant's own recipe, or not sized at all | Accepted |
| [0031](0031-proactive-restraint.md) | AMP speaks first, and mostly decides not to | Accepted |
| [0032](0032-anomaly-sweep.md) | The anomaly check runs over the whole fleet, and says why for every machine it could not score | Accepted |
| [0033](0033-oem-intelligence-floor.md) | An aggregate can disclose what a field could not, so every cross-customer figure has a floor | Accepted |
| [0034](0034-real-local-model.md) | A reasoning model needs room to name a tool, and AMP gave it none | Accepted |
| [0035](0035-copilot-follow-ups.md) | Follow-up questions: a thread the client carries, re-authorized every turn | Accepted |
| [0036](0036-lists-are-pages.md) | A list is a page, and says so: one helper, the whole count in X-Total-Count, a guard for every capped GET | Accepted |
| [0037](0037-external-model-consent.md) | A company's data leaves AMP only with that company's consent: `external_model` on the ADR-0020 gate, checked where the Copilot builds a request's model | Accepted |

**Recommended order:** 0001 first (smaller, proves the pattern; events carry `tenant_code`), then 0002.

## Format

Each ADR: Context → Decision → Consequences (positive / negative) → Alternatives → Rollout. Keep them short. New records are numbered sequentially and never edited once `Accepted` — supersede with a new ADR instead.
