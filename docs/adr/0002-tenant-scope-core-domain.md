# ADR 0002 — Tenant-scope the core domain

- **Status:** Accepted (2026-07-12)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0001 — Introduce a domain event bus](0001-domain-event-bus.md)

## Context

AMP is multi-tenant **at the platform layer**: `backend/platform_routes.py` (`TenantConfig`, `enabled_modules`, branding, audit) and the GMATS inventory tables carry `tenant_code`, and the JWT carries the tenant.

But the **core operational tables** in `backend/models.py` — machines, work orders, production records, downtime, quality inspections, inventory items, etc. — have **no `tenant_code`**. Today one tenant's data is effectively the entire table.

The vision ("power thousands of factories") — and simply onboarding customer #2 or #3 — requires strict **per-tenant data isolation**. Retrofitting this gets more expensive with every new table, endpoint, and query written against the un-scoped model. **This is the #1 scaling blocker and it compounds weekly.**

## Decision

- Add an **indexed `tenant_code`** column to all core operational tables (nullable + backfill, then `NOT NULL`).
- **Derive tenant from the authenticated principal (JWT) — never from client input** — and enforce it centrally via a **tenant-scoped repository / base-query layer** that automatically filters every read and stamps every write with the caller's tenant.
- Route core data access **through the scoped repository**; disallow raw `db.query(...)` on core tables that bypasses scoping.
- Seed the existing rows to their real owner (`DEFAULT` = founder/demo, `GMATS` = first client).
- Add a **CI guard** that fails loudly when a core query is missing a tenant filter (defense in depth).

## Consequences

**Positive**
- Real data isolation → safe to onboard customer #2 with no data bleed. Unblocks the platform story.
- Central enforcement eliminates per-endpoint tenant-filter mistakes (a classic security-bug class).
- Aligns core with the already-tenant-aware platform/GMATS layers **and** the event model (events are tenant-stamped — see ADR-0001).

**Negative / risks**
- A migration touching many tables + a backfill → must be reversible and verified against the live DB.
- Every core query path moves to the scoped repository → churn in `main.py`, done **table/endpoint at a time**.
- Minor index/storage overhead (acceptable; `tenant_code` is low-cardinality and indexed).

## Alternatives considered

- **Database-/schema-per-tenant:** strongest isolation, but heavy operationally now and complicates cross-tenant analytics/AI later. Row-level `tenant_code` on a shared schema matches the existing GMATS/platform pattern; revisit per-tenant DBs for large/regulated customers.
- **Postgres Row-Level Security (RLS):** excellent defense-in-depth; layer it **on top** later. App-level scoping is the first, portable step.
- **Keep going without it:** every week of new code against un-scoped tables raises future cost and risk. Not viable.

## Rollout (incremental)

1. Migration: add nullable `tenant_code` + index to core tables; backfill existing rows to their owner; then set `NOT NULL`.
2. Introduce `TenantScopedRepository`; route the highest-traffic contexts first — machines, work orders, inventory.
3. Convert remaining core endpoints table-by-table; add the CI guard for un-scoped core queries.
4. (Later) optionally add Postgres RLS as belt-and-suspenders.

**Backward compatibility:** existing single-tenant (GMATS/DEFAULT) behaviour is preserved by the backfill; API contracts unchanged.

## Sequencing note

ADR-0001 and 0002 are complementary — events carry `tenant_code`, so deriving tenant (0002) alongside the event envelope (0001) avoids rework. **Recommended order: 0001 first** (smaller, proves the pattern), then 0002.

## Postmortem — PR #3 enforcement (2026-07-13)

The enforcement PR first bound the request tenant with `@app.middleware("http")` (Starlette `BaseHTTPMiddleware`). That buffers the request body and runs the endpoint in a **separate task**, which (a) **deadlocked every POST** — `POST /login` hung in production — and (b) would not have propagated the tenant `contextvar` into the threadpool handler anyway. It passed unit tests because they never exercised the HTTP layer.

**Fix:** bind the tenant with a **pure-ASGI middleware** (`TenantScopeMiddleware`) that shares the endpoint's task — POST bodies stream and the contextvar reaches the ORM. Verified on a running server (`POST /login` → 200; a GMATS login saw 0 of 7 `DEFAULT` machines) before redeploy.

**Rule going forward:** never use `BaseHTTPMiddleware` for request-context/tenant binding — use pure ASGI. **Any middleware or auth change must be smoke-tested against a running server (boot + `POST /login`), not only unit tests** — see the deploy checklist (`docs/Production-Setup.md` §7).

## Postmortem — writers with no request (2026-09-20)

The scoping above is a contextvar a request binds. The simulator
(`factory_simulator.tick_*`) is the one writer with **no request**: it animates
several tenants from one background loop, and main binds the tenant itself
before each tenant's ticks. With **nothing** bound, both halves of the
mechanism stand down — the read filter is off, and the write stamp is off — and
each `tenant_code` column's `default="DEFAULT"` fills the gap. So an unbound
tick read **every** tenant's machines and work orders and filed what it wrote
under the demo tenant: measured, 41 rows in twelve unbound rounds against the
three-factory fixtures, telemetry and inspections for FACTORY_B's machines with
`tenant_code = DEFAULT`. The loop never ran unbound; the CLI runner and any
direct caller did. Only the heartbeat refused (ADR-0021).

**Fix:** every tick refuses with no tenant bound (`factory_simulator._bound_tenant`,
a `ValueError` that names the rule), and the CLI binds DEFAULT explicitly.
Pinned per tick by `test_sim_ticks_need_a_tenant.py`, bent 16 ways by
`mutate_sim_tenant_guard.py`, and proved against the whole three-factory world
by `audit_three_factory_simulation.py` (CI, every push).

**Rule going forward:** a writer that runs outside a request — a background
loop, a CLI, a scheduled job — must bind its tenant explicitly and **refuse to
run unbound**. "No tenant bound" is a legitimate state for a migration or a
founder script that reads across tenants; it is never a legitimate state for
something that writes rows a tenant will read back. The column default is a
schema convenience for the single-tenant past, not a scoping rule.
