# ADR 0009 — Modularize main.py by domain (route modules via register(app))

- **Status:** Accepted (2026-07-20)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md), [0007 — Read-models: projections](0007-read-models-projections.md), [0008 — Tenant lifecycle & commercial enforcement](0008-tenant-lifecycle-and-commercial-enforcement.md)

## Context

`backend/main.py` grew to 4,274 lines and 192 endpoints — the project's standing
structural debt (see the engineering history). It was never a *correctness* risk,
because every load-bearing property is enforced at a chokepoint, not per-endpoint:
tenant isolation in the ORM hook (ADR-0002), plan licensing in `PlanGateMiddleware`,
auth in dependencies. But a single 4k-line file is a navigability and merge-conflict
hazard, and it hides duplicates — a dead, shadowed second `/health` lurked there
until a survey for this very refactor found it (#142).

Two route modules already demonstrated the way out: `platform_routes.py` and
`enterprise_inventory_routes.py` expose a `register(app)` and are wired in from
`main.py` at import time.

## Decision

Peel cohesive **domains** off `main.py` into route modules, one per PR, using the
existing `register(app)` pattern.

- **No import cycles.** A route module imports only lower-level modules
  (`models`, `database`, `auth`, `tenancy`, `ai`, and peer service modules) —
  **never `main`**. `main` imports the module and calls `register(app)`.
- **Preserve the route-count invariant.** An extraction *relocates* endpoints; it
  adds and removes none. The boot check asserts the total registered-route count is
  unchanged, and each module ships a **registration-guard test** proving its paths
  are registered exactly once and owned by the new module — so a future edit can't
  silently drop a route or reintroduce a shadowing duplicate.
- **Testability decides handler placement.** Handlers unit-tested *by name*
  (`saas_routes` — the onboarding/offboarding suites call them directly) are defined
  at **module level** and attached in `register()`. Handlers exercised only through
  their `ai.build_*` projections (`read_model_routes`, `agent_routes`) may nest in
  `register()`.
- **Shared request helpers move to where they belong.** `request_tenant` (the
  effective-tenant resolver, formerly `main._tenant`) moved to `tenancy`; each
  domain's private helpers move with it (`_registry_scope`/`_require_founder` →
  `saas_routes`, `_agent_action_dict`/`_decide_agent_action` → `agent_routes`).
- **Event coupling survives the move, byte-for-byte.** A handler that publishes a
  domain event imports the bus and event class directly (`from events import
  event_bus, ProductionCompleted`) and publishes on the *request* DB session, so
  the event and its subscribers still commit atomically with the write. The guard
  test asserts the publish is still present in the moved module's source, not just
  that the route registered.
- **Leave the stateful core — and the shared compute — in main until it has a home.**
  Endpoints that read main-local globals (`/platform/status` reads the sim
  heartbeat) or don't fit a clean domain (`/ops-trends`, `/briefing/escalate`)
  stay. So does any endpoint that calls a *main-local compute helper*: the
  reporting peel-off left `/reports/daily-summary.txt` behind because it calls the
  `analytics_summary` endpoint function directly, and excluded
  `/escalations/from-smart-alerts` earlier because it shares `generate_alerts`.
  When a shared helper already exists in a service module, the extraction imports
  *that* one instead — e.g. `reports_routes` and `main` both moved to the single
  `analytics_engine.calculate_oee_from_record` (#162), retiring main's duplicate.

## Consequences

**Positive**
- `main.py`: **4274 → 1675** lines (192 → 44 endpoints) across fifteen peel-offs,
  one domain per PR:
  `read_model_routes` (#143), `agent_routes` (#146), `saas_routes` (#147),
  `costing_routes`, `machines_routes`, `orders_routes`, `factory_ops_routes` (#153),
  then the core-CRUD + reporting wave — `work_orders_routes` (#154, keeps the
  `ProductionCompleted` publish), `inventory_routes` (#155, `InventoryLow`),
  `quality_routes` (#156, `QualityInspectionFailed`), `production_planning_routes`
  (#157), `industrial_iot_routes` (#158), `operator_routes` (#159), `users_routes`
  (#160, `VALID_ROLES` moved with it), and `reports_routes` (#161, all compute
  imported from the shared engines). Every extraction held the route-count
  invariant and shipped a registration-guard test.
- New endpoints in a domain get an obvious home; merge conflicts localize.
- Duplicate-route hazards are now caught mechanically (registration guards +
  route-count invariant), not by luck.
- Safe to do *because* CI now runs the full suite (#139): a botched relocation
  fails the boot check and the guard tests, in CI, before merge.

**Negative / accepted**
- Two handler-placement conventions coexist (module-level vs nested). The rule
  (by-name testability) is documented here and in each module's docstring.
- A handler moved out of `main` is no longer `main.<name>`; call sites in tests
  must follow (as the onboarding/offboarding suites did).

## Alternatives considered

- **A full `APIRouter`/prefix restructure.** Cleaner FastAPI idiom, but changes
  nothing users see and needs no path changes today; `register(app)` matches the
  existing pattern and is a smaller, safer step. Revisit if module count grows.
- **Split by HTTP verb or by file size.** Rejected — domain cohesion is the right
  axis; the goal is that related endpoints and their helpers live together.
- **Leave `main.py` as-is.** Rejected — the hidden-duplicate risk is real (#142),
  and the file will only grow.

## Rollout

Incremental, one domain per PR, each verified by the route-count invariant + the
full test suite (CI-enforced) + a live smoke on production. The CRUD clusters
named at first writing (inventory / costing / orders / machines) are done, along
with work-orders, quality, planning, industrial-IoT, operator, users and
reporting.

**What is left, and what it needs first.** The dominant remaining cluster is
`/analytics` (~23 endpoints) plus `/alerts`, `/oee` and `/machine-health`. Unlike
the CRUD domains, these are pinned to two *main-local* compute helpers —
`generate_alerts` and `analytics_summary` (the latter is itself an endpoint
function called directly by other handlers). Extracting the cluster is therefore
a two-step job, not a byte-preserving route move: first relocate those helpers to
a shared engine (`analytics_engine`) as plain functions, rewire main's remaining
callers to import them, *then* peel the routes. The `calculate_oee_from_record`
dedup (#162) was the first of those enabling moves. This is a larger,
higher-touch change on the hot dashboard path and is best done as its own focused
step rather than bundled with a mechanical extraction. The genuinely stateful
core (`/platform/status`, `/ops-trends`, auth/bootstrap, `/briefing`) is expected
to remain in `main.py` indefinitely.
