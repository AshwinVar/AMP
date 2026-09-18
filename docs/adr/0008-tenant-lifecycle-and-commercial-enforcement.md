# ADR 0008 — Tenant lifecycle & commercial enforcement

- **Status:** Accepted (2026-07-19)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md), [0007 — Read-models: projections](0007-read-models-projections.md)

## Context

ADR-0002 made tenant *data* isolation automatic, but the platform still had no
commercial machinery around it: onboarding a company was a manual multi-step
chore, the founder could not see a customer's workspace, pricing plans were
cosmetic, nothing expired, and deleting a tenant orphaned its data forever. To
sell AMP as a SaaS, the tenant's whole life — join, trial, pay, upgrade, leave —
had to become mechanical, enforced server-side, and demoable from one screen
(PRs #115–#129).

## Decision

Treat the tenant lifecycle as a set of **chokepoints**, in the same spirit as
ADR-0002's single scoping hook: each rule lives in exactly one place that
requests must pass through, never in per-endpoint code that can be forgotten.

- **Founder preview (effective tenant).** A request's scope is
  `effective_tenant(jwt_claim, X-Tenant header)`: the header is honoured
  **only** when the claim is `DEFAULT`, so the founder's company switcher
  previews any tenant — data, read-models, licence, branding, even the plan
  gate — exactly as the customer sees it, while a client token can never
  escape its own tenant. Token *issuance* (login/refresh) always uses the raw
  claim; preview never changes identity.
- **Onboarding is one form.** Creating a registry row (`CompanyTenant`) seeds
  a generic starter factory (industry-neutral; every business key
  tenant-prefixed — `item_code` is globally unique) and syncs the licence from
  the chosen plan. A "Create admin" action provisions `<code>_admin` with a
  bcrypt-stored one-time password; the customer rotates it via self-service
  change-password.
- **Commercial states are enforced at login and at the API.** `Cancelled` and
  expired `Trial` (a `TRIAL_DAYS` clock from the registry row's `created_at`)
  block sign-in with honest messages. Plan tiers map to module packs
  (`apply_plan_tier`; which packs a plan bundles is read from `modules.json`
  through `module_manifest.plan_modules` and nowhere else,
  `test_plan_bundles_one_rule.py`), padlocked in the UI and enforced by
  `PlanGateMiddleware` — a path→pack table gating by effective tenant, with a
  briefly-cached licence (dropped by `commit_tenant_config`, the one way a
  tenant config is written, so a revocation bites at once) that **fails open**:
  availability beats enforcement for a plan gate. `core` and `admin` packs are
  never gated, so no tenant is locked out of basics or account management.
- **Background work is allowlisted per tenant.** The simulator animates only
  `SIM_TENANTS` (each ticked under its own bound scope) so it can never
  overwrite a real customer's machine data; the proactive escalation pass, by
  contrast, runs for every tenant because it acts on their real data.
  `/platform/status` self-reports the loaded allowlist and a tick heartbeat
  (founder-only) so "is it running, and over whom?" is answerable from the app.
- **Offboarding sweeps by construction.** `purge_tenant_data` deletes every
  mapped model carrying `tenant_code` — future tenant-stamped tables are
  covered automatically, no list to forget — via multi-pass savepoint deletes
  (FK-safe on Postgres, no hardcoded order). `DEFAULT` and blank codes are
  never purgeable; `EventLog`/`AuditLog` are kept as immutable history.
  *Correction (2026-09): "by construction" covered only tables WITH a
  `tenant_code`. A tenant-less child of a tenant table is invisible to the sweep
  and blocks its parent on PostgreSQL — `gmats_proforma_lines` and
  `gmats_min_lines` did exactly that ("purge blocked by constraints on:
  gmats_proformas, gmats_items, gmats_min", reproduced on PostgreSQL 18.3), so no
  tenant that had used GMATS could be offboarded. Each such child now has a
  DECLARED fate in `offboard_tenant.TENANTLESS_CHILDREN` (delete with parent, or
  unlink for OEM-owned installations), and `test_offboarding` enumerates the real
  model registry so an undeclared one fails. A line belonging to another tenant
  that references a purged item is detached, never deleted. The SaaS delete now
  purges BEFORE removing the registry row, so a failed purge leaves the company
  listed and retryable instead of gone from SaaS Admin with its data intact.*

## Consequences

**Positive**
- The whole commercial story is mechanical and demoable: create → seeded
  factory → plan-driven licence → provisioned admin → trial clock → cancel
  blocks → purge cleans. One SaaS Admin screen drives all of it.
- Preview fidelity: the founder sees literally what the customer sees,
  because preview rides the same chokepoints instead of a parallel path.
- New tables and endpoints inherit the rules (registry-mapper sweep, path
  table, ORM hook) rather than needing to remember them.

**Negative / accepted risks**
- Enforcement at login means an already-issued token outlives a cancellation
  by up to its 4-hour lifetime.
- The plan gate's path table is deliberately conservative; endpoints woven
  into the core Overview stay open even when they also serve premium views.
- ~~The licence cache can serve a stale allowlist for up to 60 s after an edit
  made outside `apply_plan_tier`.~~ **Closed (2026-09).** It was not a property
  of the cache but of where the rule lived: three handlers dropped the entry and
  `PATCH /tenant-config` did not, while a comment on a neighbouring handler
  described it as already doing so. `platform_routes.commit_tenant_config` now
  commits and invalidates together, so every tenant-config write takes effect at
  once and a fifth handler cannot forget — an AST guard in
  `backend/test_licence_change_is_immediate.py` fails if one commits on its own.

**Lessons encoded in tests**
- SQLite does not enforce foreign keys by default: offboarding tests run with
  `PRAGMA foreign_keys=ON`, or deletion-order bugs pass locally and explode on
  Postgres.
- Starlette runs the *last-added* middleware first: anything that returns
  responses itself (the plan gate's 403s) must be added **before**
  `CORSMiddleware`, or browsers see opaque network errors.
- Multi-tenant seeds must be tested with **two** tenants in one database —
  the single-tenant suite hid a global-uniqueness collision that left every
  tenant after the first with an empty factory.

## Alternatives considered

- *Per-endpoint decorators* for plan/tenant checks — rejected: 80+ call sites
  to forget; chokepoints fail closed by construction.
- *Hard-deleting on tenant delete without an explicit purge flag* — rejected:
  registry removal and irreversible data loss are different decisions, so the
  UI asks them separately.
- *Trial state on `TenantConfig` (`trial_ends_at`)* — rejected for
  enforcement: the SaaS registry the founder actually edits is
  `CompanyTenant`; one source of truth for commercial state.

## Rollout

Shipped incrementally as PRs #115–#129, each verified live on production
(second tenant APEX onboarded end-to-end; offboarding exercised with a
throwaway tenant; plan gate probed with real client tokens). Two production
bugs found by the live end-to-end checks — the seed collision and the FK purge
order — are covered by regression tests.
