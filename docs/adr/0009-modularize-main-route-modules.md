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

Peel cohesive **domains** off `main.py` into route modules, one per PR. The
extraction wave used the existing `register(app)` pattern; once every domain was
separated, the modules were migrated to FastAPI `APIRouter`s (see Rollout) — each
now exposes a module-level `router = APIRouter()` with `@router.<verb>` handlers,
and `main` wires it with `app.include_router(module.router)`. The two forms are
behaviourally identical (same routes, same order); everything below applies to
both.

- **No import cycles.** A route module imports only lower-level modules
  (`models`, `database`, `auth`, `tenancy`, `ai`, and peer service modules) —
  **never `main`**. `main` imports the module and includes its router (a module
  may still export a plain symbol back to `main` — `log_audit`,
  `analytics_summary`).
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
- **Group the leftovers behind one router rather than leaving them on `app`.**
  The endpoints that fit no domain — auth/bootstrap, `/platform/status`, `/bom`,
  and the intelligence stragglers (`/ops-trends`, `/briefing/escalate`,
  `/reports/daily-summary.txt`, `/escalations/from-smart-alerts`) — were the last
  routes defined directly on `app` in `main.py`. They now live behind `core_routes`
  (an `APIRouter` tagged "Core"), so `main.py` owns **no HTTP route at all** — it
  assembles the app and keeps only the lifecycle bits (the sim loop, the startup
  event, the `/ws/live` websocket). The one blocker was that `/platform/status`
  reads the sim heartbeat the loop mutates via `global` (which doesn't cross module
  boundaries), so that state moved to a shared `sim_state` leaf module the loop
  writes and the endpoint reads; `CLIENT_TENANTS` moved to `tenancy` for the same
  reason. Grouping — not scattering — is the rule: these belong together as "core",
  not spread into domain modules where each would be an arbitrary fit.

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
- `main.py` defines **no HTTP route** any more: the irreducible core — auth/bootstrap
  (`/login`, `/register`, `/me`, `/auth/*`), `/platform/status`, `/bom`, and the
  intelligence stragglers (`/ops-trends`, `/briefing/escalate`,
  `/escalations/from-smart-alerts`, `/reports/daily-summary.txt`) — is grouped
  behind `core_routes` (#181). `main.py` fell to ~419 lines and now just assembles
  the app and owns the lifecycle (sim loop, startup, `/ws/live` websocket).

**Negative / accepted**
- Two handler-placement conventions coexist (module-level vs nested). The rule
  (by-name testability) is documented here and in each module's docstring.
- A handler moved out of `main` is no longer `main.<name>`; call sites in tests
  must follow (as the onboarding/offboarding suites did).

## Alternatives considered

- **A full `APIRouter`/prefix restructure.** Cleaner FastAPI idiom, but during the
  *extraction* wave it would have changed nothing users see while adding risk;
  `register(app)` matched the existing pattern and was the smaller, safer step.
  Deferred then, **adopted after** — once the domains were separated and
  guard-tested, converting each `register(app)` to an `APIRouter` was mechanical
  and low-risk (#169–#171). See Rollout.
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
move. That left `main.py` at 706 lines / 12 endpoints — the irreducible core
(auth/bootstrap, `/platform/status`, `/bom`, the intelligence stragglers) — which
was then itself grouped behind `core_routes` (#181, after #180 moved the sim
heartbeat to `sim_state` and `CLIENT_TENANTS` to `tenancy`). `main.py` now defines
**no HTTP route at all** (~419 lines): every endpoint lives in a router, and main
just assembles the app and owns the lifecycle (sim loop, startup, `/ws/live`).

**The shape migration is also done.** With every domain separated and
guard-tested, the `register(app)` modules were converted to FastAPI `APIRouter`s
in three mechanical batches (#169 the CRUD modules, #170 the larger/read-model
modules, #171 the remaining + pre-existing ones). All 22 route modules now expose
a module-level `router = APIRouter()` and are wired with
`app.include_router(...)`; the route-count invariant held at 237 throughout, and
the existing registration-guard tests passed unchanged (a handler's `__module__`
is still its module). Handlers being module-level retired the old
nested-vs-module-level split entirely. The lone survivor is `ai.copilot`, a thin
`register(app)` *coordinator* (feature-flag entry point) that holds no routes of
its own and simply includes `ai_copilot.router` — correct as-is.

**Router metadata followed.** Every router now carries an OpenAPI `tags=[...]`
domain label (#173), so `/docs` groups endpoints by domain (22 groups). The ten
modules whose routes all share one root also gained an `APIRouter(prefix=...)`
with the root declared once instead of on every decorator (#174) — work-orders,
inventory, quality, operator, users, reports, gmats, industrial-adapters, and the
two `/ai` modules (recommendations, ai_copilot); collection endpoints became
`@router.<verb>("")` so the URL is unchanged. Multi-root modules (machines,
orders, analytics, factory_ops, read_model, …) keep explicit paths + a tag — a
prefix there would change URLs or need splitting. Both changes were verified
against a captured path+method baseline: **byte-for-byte identical, 237
endpoints, zero URL drift**, not merely the same count.

**Router-level dependencies where the gate is uniform.** `users_routes` was the
one module where every endpoint carried the *same* auth gate — all five were
`require_roles(["Admin"])` — so the gate moved onto the router once,
`APIRouter(..., dependencies=[Depends(require_roles(["Admin"]))])` (#176). Now any
endpoint added to that module inherits the Admin requirement structurally and
can't ship ungated by omission. The handlers still receive `current_user` (they
stamp the tenant and audit-log), but via `Depends(get_current_user)`; the router
enforces the role, and FastAPI caches `get_current_user` so the token is decoded
once. Behaviour is identical — verified by route introspection (all five `/users`
routes still carry the `require_roles` checker) and by the gate rejecting a
non-Admin (403) and passing an Admin, both locked in by guard tests.

The same move applies one level down, at *authenticated* rather than *admin*.
`read_model_routes` (25 read projections) and `ai_copilot` (3 endpoints) are
uniformly gated by `get_current_user`, so each hoists it onto its router,
`APIRouter(..., dependencies=[Depends(get_current_user)])` (#178) — a future read
endpoint can't ship public by omission. Note the difference in payoff: every one
of those 28 handlers uses `current_user` in its body (to derive the tenant), so
they keep the `Depends(get_current_user)` parameter for the *value*; the router
dependency doesn't shorten a signature, it adds the module-wide invariant (and
FastAPI caches `get_current_user`, so it's still decoded once). A guard test
asserts every route in both modules carries the gate.

What it is **not** is a blanket pass — a router-level dependency is only safe when
the module's gate is genuinely *uniform*. The role-mixing modules keep their
per-handler gates: `saas_routes`, for instance, gates writes with Admin + a
founder check but leaves the registry reads open to any authenticated user
(`_registry_scope`), so a router-wide Admin dependency would change who can reach
those reads. So three modules declare their gate at the router (users → Admin,
read-model + copilot → authenticated) and the rest stay per-handler by design.
With this, the ADR-0009 programme is complete on every axis — size, shape,
metadata, and auth-gating.
