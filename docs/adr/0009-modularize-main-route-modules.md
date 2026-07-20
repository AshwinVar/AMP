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
- **Relocate the shared compute first; then the routes follow.** When endpoints
  across several domains lean on a *main-local compute helper*, move the helper to
  a shared engine before peeling the routes — the enabling step is its own PR.
  `analytics_engine.calculate_oee_from_record` was deduped first (#162), then
  `generate_alerts` / `calculate_fallback_oee` / `parse_duration_to_minutes` were
  relocated there (#164), which finally unpinned the whole `/analytics` cluster.
  The one helper that is itself an *endpoint function* (`analytics_summary`,
  called directly by `/reports/daily-summary.txt`) was hoisted to module level in
  `analytics_routes` and re-imported by `main` — a route module may export a
  symbol back to `main`, the mirror of `main` importing `log_audit` from
  `platform_routes`.
- **Leave the genuinely stateful core in main.** Endpoints that read main-local
  globals (`/platform/status` reads the sim heartbeat) or don't fit a clean domain
  (`/ops-trends`, `/briefing/escalate`) stay. `/reports/daily-summary.txt` and
  `/escalations/from-smart-alerts` also stay — not because of an import barrier any
  more (both now import their compute from the shared engine / `analytics_routes`)
  but because they're small intelligence stragglers with no cohesive module of
  their own; moving them would trade one arbitrary home for another.

## Consequences

**Positive**
- `main.py`: **4274 → 706** lines (192 → 12 endpoints) across seventeen domain
  peel-offs, one per PR:
  `read_model_routes` (#143), `agent_routes` (#146), `saas_routes` (#147),
  `costing_routes`, `machines_routes`, `orders_routes`, `factory_ops_routes` (#153),
  then the core-CRUD + reporting wave — `work_orders_routes` (#154, keeps the
  `ProductionCompleted` publish), `inventory_routes` (#155, `InventoryLow`),
  `quality_routes` (#156, `QualityInspectionFailed`), `production_planning_routes`
  (#157), `industrial_iot_routes` (#158), `operator_routes` (#159), `users_routes`
  (#160, `VALID_ROLES` moved with it), and `reports_routes` (#161, all compute
  imported from the shared engines) — and finally the intelligence cluster:
  `analytics_routes` (#165, 27 endpoints, the largest peel-off, after its compute
  helpers were relocated to `analytics_engine` in #164) and
  `recommendations_routes` (#167). Every extraction held the route-count invariant
  and shipped a registration-guard test.
- New endpoints in a domain get an obvious home; merge conflicts localize.
- Duplicate-route hazards are now caught mechanically (registration guards +
  route-count invariant), not by luck — and the discipline paid out: extracting
  the audit domain surfaced a *shadowed duplicate* `GET /audit-logs` (main's copy
  was dead behind `platform_routes`'), the same class of bug as the dead `/health`
  (#142). It was removed and the audit domain consolidated in `platform_routes`
  (#166), dropping the live route count 238 → 237.
- Safe to do *because* CI now runs the full suite (#139): a botched relocation
  fails the boot check and the guard tests, in CI, before merge.
- What's left in `main.py` is the irreducible core: auth/bootstrap (`/login`,
  `/register`, `/me`, `/auth/*`), the sim-stateful `/platform/status` +
  `/ops-trends`, `/bom`, and the three intelligence stragglers
  (`/briefing/escalate`, `/escalations/from-smart-alerts`,
  `/reports/daily-summary.txt`) that share main's request-time compute.

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
full test suite (CI-enforced) + a live smoke on production. **The rollout is
complete.** Every cohesive domain has been peeled: the CRUD clusters named at
first writing (inventory / costing / orders / machines), then work-orders,
quality, planning, industrial-IoT, operator, users, reporting, and finally the
`/analytics` intelligence cluster — the one that needed the two-step treatment
(relocate `generate_alerts` / `calculate_fallback_oee` / `parse_duration_to_minutes`
to `analytics_engine` and hoist `analytics_summary` to module level, #164/#165,
after the `calculate_oee_from_record` dedup in #162) rather than a plain route
move. `main.py` finished at **706 lines / 12 endpoints** — the irreducible core
(auth/bootstrap, the sim-stateful `/platform/status` + `/ops-trends`, `/bom`, and
three intelligence stragglers) that is expected to stay in `main.py`
indefinitely.

If the strangler continues, the next axis is no longer file size but shape:
migrate the `register(app)` modules to FastAPI `APIRouter`s (the alternative
deferred above), which is now a mechanical, low-risk change since the domains are
already separated and guard-tested.
