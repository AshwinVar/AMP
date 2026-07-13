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
