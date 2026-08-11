# ADR-0018: Migrations run before the application serves

**Status:** Accepted · 2026-08-11
**Supersedes in part:** the "belt and braces" arrangement introduced with Alembic adoption
**Incident:** [#513](https://github.com/AshwinVar/AMP/pull/513) — a day-long total authentication outage

---

## Context

On 2026-08-09 every login on production returned **500** for roughly a day.

`models.User` declared `is_active`. The production `users` table did not have
it. SQLAlchemy names every mapped column in its SELECT list, so *every* query
for a User failed:

```
psycopg2.errors.UndefinedColumn: column users.is_active does not exist
```

The Alembic migration that adds the column had been written, reviewed and
merged. It had never run.

### Three mechanisms, none of which carried the change

| Mechanism | What it does | Why it missed |
|---|---|---|
| Alembic | the authoritative migration tool | **nothing in the deploy ran it.** `docs/MIGRATIONS.md` said so outright: *"Railway currently runs `migrate.py` manually"* |
| `create_all()` | runs at import on every boot | creates missing **tables**; has never altered an existing one |
| `_ensure_column()` boot patches | ~30 idempotent DDL helpers in `main.py` | nobody added one for this column, because Alembic was assumed to cover it |

Two more columns were in the same state: `agent_actions.expires_at`, and the
`machine_installations` service-clock columns from a migration merged the day
before.

### Why nothing caught it

Every one of the 186 backend suites builds its schema with `create_all()` on a
**fresh** database. That is the single arrangement in which model/schema drift
cannot occur — `users` is always created *with* `is_active`. The defect lives
exclusively in the gap between a database that already exists and a model that
has moved on, and no test, and no CI job, went near that gap. CI ran SQLite only
and never executed a migration.

### The shape of the failure

`/health` returned **200** throughout. It runs `SELECT 1`, which a schema
mismatch is invisible to. The product was entirely unusable and every probe said
it was fine — so nothing alerted, and the outage was found by a person trying to
log in.

---

## Decision

**Migrations are part of deployment, and an application that cannot use its
database does not receive traffic.**

```
DEPLOY  →  MIGRATE  →  START  →  READINESS  →  TRAFFIC
             ↓ fails
        deploy aborts, previous deployment keeps serving
```

Four changes, each of which is sufficient to prevent the incident on its own.
They are layered on purpose: the first is configuration somebody can change, the
second is code that travels with the build.

### 1. The deploy runs migrations — `preDeployCommand`

```toml
[deploy]
preDeployCommand = "python migrate.py"
startCommand = "python -m uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/readiness"
```

Railway runs `preDeployCommand` to completion, in the new build's image, with
the service's environment, **before** the new deployment starts serving. A
non-zero exit aborts the deploy and the previous deployment keeps taking
traffic.

`migrate.py` rather than `alembic upgrade head`, because it also handles
adoption — an existing database with no `alembic_version` is stamped at the
baseline first — and is a no-op when there is nothing to do.

**Verified available, not assumed:** Railway's service settings expose
*Add pre-deploy step*, and every other deploy field on that page reads *"the
value is set in `/backend/railway.toml`"*, so config-as-code is authoritative.

### 2. The application refuses a schema it was not written for

`schema_guard.py` compares the revision **this build requires** against the
revision **the database reports**.

| State | Meaning | Verdict |
|---|---|---|
| `ok` | database is at this build's head | serve |
| `behind` | an ancestor of head — migrations pending | **refuse, everywhere** |
| `ahead` | a revision this build has never heard of | serve, warn loudly |
| `unmanaged` | tables exist, no `alembic_version` | serve in dev, **refuse in production** |
| `empty` | no tables at all | serve in dev, **refuse in production** |
| `unreachable` | the database could not be read | refuse |

While incompatible, `SchemaGuardMiddleware` returns **503** on every application
route, with both revisions in the body. `/health`, `/readiness`, `/docs` and
`/openapi.json` stay reachable so the deployment is diagnosable.

Startup also **halts before seeding** when incompatible: the seeders write rows
through the ORM, and against the wrong schema those writes either fail or
half-succeed.

**Why `ahead` is allowed.** Rolling the application back is the primary recovery
lever when a release goes wrong, and refusing to start against a newer schema
would remove that lever exactly when it is needed. It is safe *only* because
migrations are required to be backwards compatible (see §Expand/migrate/contract
below) — the allowance is what makes that rule load-bearing rather than
advisory.

**Why `unmanaged` differs by environment.** In dev and CI it is the normal
state: `create_all()` built the schema in the same process that is about to use
it, so it is at model shape by construction. In production it is a defect, and
it is precisely the state production was in during #513 — a database that has
accumulated over a year is *not* at model shape. Production is detected exactly
as `auth.py` detects it for the JWT signing key (`RAILWAY_ENVIRONMENT`, or an
explicit `PRODUCTION=1`), and the asymmetry is the same one that module argues:
permissive where a mistake costs a developer a minute, fail-closed where it
costs users a day.

### 3. Liveness and readiness are different questions

| | `/health` | `/readiness` |
|---|---|---|
| Asks | is this process alive and can it reach its database? | should this instance receive traffic? |
| 200 when | process up, `SELECT 1` answers | that **and** the schema is at the expected revision |
| Used by | uptime monitors, restart policy | **the deploy healthcheck** |

`/health` now carries the schema state and reports `"status": "degraded"` when
it is wrong, so it can never again describe a product nobody can log into as
healthy. Its **status code** deliberately stays about liveness: a 503 there would
put the container in a restart loop that cannot possibly fix a schema problem,
while paging somebody for an incident whose fix is a migration.

`railway.toml` points its healthcheck at `/readiness`. That is the second half
of the invariant: even if the pre-deploy step is removed, mis-typed or silently
unsupported, a build whose migrations have not been applied fails its
healthcheck and never cuts over.

### 4. Exactly one mechanism owns a given database

Decided by the database itself, not by an environment variable somebody has to
remember to set:

```
alembic_version PRESENT  →  Alembic owns it. create_all() and every boot
                            patch stand down. A new table or column can then
                            only arrive through a migration.
alembic_version ABSENT   →  create_all() + boot patches, exactly as before.
```

`create_all()` is not an innocent bystander in #513: it silently creates missing
tables, which is how `machine_installations` came to exist without the columns
migration 0007 adds to it. On a managed database it no longer gets that
opportunity — asserted by a CONTROL in `verify_pg_deploy.py` that drops a column
from a managed database and proves the next boot does **not** put it back.

---

## Staged retirement, not deletion

`create_all()` and the ~30 `_ensure_column()` calls are **kept**, disabled on
managed databases. Removing both in the same release that introduces the new
mechanism would leave no fallback if the new one is wrong.

| Stage | State | Exit criterion |
|---|---|---|
| **1 — now** | Alembic authoritative on managed databases; boot patches live but inert there | this ADR |
| **2** | remove the `_ensure_column` calls for columns whose migrations have demonstrably run on production | 30 days of deploys with the pre-deploy step green, and production confirmed at head |
| **3** | remove `create_all()` from `main.py` and `factory_simulator.py`; tests move to `migrate.run()` for schema setup | stage 2 complete, and the suites no longer depend on `create_all` |

Stage 2 and 3 are deliberately **not** in this change. They alter how every test
builds its schema, and that is a separate risk from fixing the deploy.

---

## The four concerns, kept apart

| Concern | Mechanism | Where |
|---|---|---|
| **Schema creation** | `alembic upgrade` from the baseline, or `create_all()` on an unmanaged database | `migrate.py` |
| **Schema migration** | Alembic revisions, run by `preDeployCommand` | `alembic/versions/`, `railway.toml` |
| **Schema verification** | revision comparison, refusing traffic on mismatch | `schema_guard.py`, `/readiness` |
| **Emergency recovery** | `migrate.py --status`, `--sql`, roll the app back (`ahead` is allowed), restore from the nightly backup | `docs/MIGRATIONS.md` |

---

## Expand / migrate / contract

Because `ahead` is allowed and because a deploy can be rolled back, **a
migration must leave the previous application version working.** Risky changes
are therefore split across releases:

| Phase | Release | Example |
|---|---|---|
| **Expand** | N | add the new column, nullable or defaulted. Old code ignores it. |
| **Migrate** | N | backfill; write to both old and new; read from the new one |
| **Contract** | N+1 or later, once nothing reads the old shape | drop the old column |

Rules that follow from it, and are enforced in review:

- **Never** rename a column in one step — add, backfill, switch reads, drop.
- **Never** add `NOT NULL` without a server default on a populated table.
- **Never** drop a column in the same release that stops using it.
- Index creation on a large table should be `CONCURRENTLY` (which requires
  `autocommit_block()` in the migration, because it cannot run in a transaction).
- Data backfills belong **in migrations**, not in boot code that re-runs forever.

---

## Consequences

**Good**

- A migration that is written but not run now stops the deploy, instead of
  reaching users as a 500.
- The failure mode of the whole class is "the deploy does not cut over", with
  production still serving the previous build. That is a good day.
- `/readiness` answers "why is this deployment refusing?" in the response body,
  with both revisions. #513 was diagnosable only from a `psycopg2` traceback
  buried in the access log.
- CI runs a real PostgreSQL 18 and fails on model/migration drift **before**
  merge.

**Costs, accepted**

- Every deploy now pays the pre-deploy step (a second or two when there is
  nothing to do).
- A migration failure blocks deployment of unrelated changes. That is the point,
  and the escape hatch is to fix or revert the migration.
- Two schema mechanisms coexist during stages 1–2. The ownership rule keeps them
  from fighting, and a CONTROL test proves the inert one is inert.
- `/readiness` exposes a migration revision unauthenticated. That is operational
  metadata, not a secret, and the endpoint is asserted to leak nothing else.

---

## Evidence

| What | Where |
|---|---|
| Deployment contract on PostgreSQL 18: fresh, production-like (#513 reproduced), idempotent, behind, failed migration, invalid revision | `backend/verify_pg_deploy.py` |
| Guard logic, middleware, and probe semantics | `backend/test_schema_guard.py` |
| Every migration-added column also reaches a database that predates it | `backend/test_boot_migrations.py` |
| Model/migration drift detector, on real PostgreSQL | `.github/workflows/ci.yml` → `migrations` job |
| Adoption path on a live database | `backend/test_migrate.py` |
