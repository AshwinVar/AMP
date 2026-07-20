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
- **Leave the stateful core in main.** Endpoints that read main-local globals
  (`/platform/status` reads the sim heartbeat) or don't fit a clean domain
  (`/ops-trends`, `/briefing/escalate`) stay until they have a natural home.

## Consequences

**Positive**
- `main.py`: **4274 → 3847** lines across three peel-offs — `read_model_routes`
  (#143), `agent_routes` (#146), `saas_routes` (#147).
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
full test suite (CI-enforced) + a live smoke on production. Remaining large
clusters in `main.py` (inventory / costing / orders / machines CRUD) can follow the
same template when they warrant it.
