# Database migrations

AMP uses [Alembic](https://alembic.sqlalchemy.org/) for schema changes. This
document covers how to make one, how to deploy one, and how to undo one.

## Why this exists

For its first two months AMP built its schema with
`Base.metadata.create_all()` plus ~30 idempotent boot-time DDL helpers in
`main.py` (`_ensure_column`, `_ensure_index`, `tenancy.ensure_tenant_columns`).
That worked, but once real customer data is in the database it has three
properties that stop being acceptable:

- **No history.** Nothing recorded which changes had been applied, so a
  half-migrated database was indistinguishable from a correct one.
- **No rollback.** Every helper was forward-only.
- **Silent failure.** Each helper is wrapped in `try/except` that prints and
  continues — deliberately, so a migration hiccup cannot stop the app booting.
  The cost is that a genuinely failed `ALTER` is one log line, and the app then
  serves traffic against a schema it believes is correct.

Alembic fixes all three for changes made from now on.

## The transition (already done, but worth understanding)

Adopting a migration tool on a live database is the risky part — not writing
migrations afterwards. `migrate.py` handles it:

| Database state | What happens |
|---|---|
| Existing schema, no `alembic_version` | **Stamped** at `0001_baseline`. No DDL runs. |
| Empty database | `create_all` builds it, then stamped at `0001_baseline`. |
| Already stamped | `upgrade head` — runs only revisions added since. |

`0001_baseline` is an **empty anchor**: its `upgrade()` is `pass`. That is
deliberate. Production already has all 48 tables, so writing them out as a
create-table migration would produce a revision that has to be skipped on the
only database that matters, and that would immediately rot against `models.py`.
`test_migrate.py` guards that the baseline stays empty.

`create_all` and the boot helpers in `main.py` are **still in place**. They will
be retired in a later, separate change — removing both mechanisms in the same
deploy that introduces the new one would leave no fallback if either has a bug.

## Making a schema change

```bash
cd backend
export DATABASE_URL="postgresql://..."      # or your local sqlite URL
```

1. Edit `models.py`.
2. Generate a revision:

```bash
alembic revision --autogenerate -m "short description"
```

3. **Read the generated file before committing it.** Autogenerate is a
   starting point, not an answer. In particular it cannot see:
   - data migrations (backfills) — write those by hand;
   - the boot-time indexes from `_ensure_index`, which exist in the database
     but not in `models.py` metadata. `env.py` filters reflected-only indexes
     out of autogenerate for exactly this reason, so it will not propose
     dropping them — but check anything index-related by eye.
4. Write the `downgrade()`. If a change genuinely cannot be reversed (a
   destructive data migration), make `downgrade()` raise with an explanation
   rather than leaving a silent no-op.
5. Add a test. Schema changes that carry data implications need a test that
   pins the data outcome, not just the column's existence.

## How a migration reaches production

**Automatically, as part of the deploy.** This changed in
[ADR-0018](adr/0018-migrations-run-before-the-application-serves.md), after a
migration that had been written, reviewed and merged was never run and caused a
day-long total authentication outage ([#513](https://github.com/AshwinVar/AMP/pull/513)).

```
DEPLOY  →  MIGRATE  →  START  →  READINESS  →  TRAFFIC
             ↓ fails
        deploy aborts, previous deployment keeps serving
```

`backend/railway.toml`:

```toml
[deploy]
preDeployCommand = "python migrate.py"
startCommand = "python -m uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/readiness"
```

Railway runs `preDeployCommand` to completion, in the new build's image, with
the service's environment, **before** the new deployment serves. A non-zero exit
aborts the deploy. There is exactly one replica, so nothing runs it concurrently.

> **This section used to say migrations were applied by hand.** They are not,
> and that sentence is the one that made #513 possible: it described a manual
> step that nothing enforced and nobody performed.

### The second line: the application refuses a schema it cannot use

Even with the step above, `preDeployCommand` is configuration somebody can
change. So the build also checks itself (`backend/schema_guard.py`):

| Database state | Result |
|---|---|
| at this build's head | serves |
| **behind** head (migrations pending) | **503 on every application route**, everywhere |
| ahead (unknown revision) | serves, logs a warning — this is the rollback case |
| unmanaged (no `alembic_version`) | serves in dev; **refused in production** |

`/readiness` returns 503 with **both revisions in the body**, and it is what the
deploy healthcheck probes — so a build whose migrations have not been applied
never cuts over. `/health` stays a liveness probe (see below).

### Running one by hand

Still supported, and the recovery path when the deploy step has not run:

```bash
python backend/migrate.py --status    # current vs head; changes nothing
python backend/migrate.py --sql       # the exact SQL, reviewable
python backend/migrate.py             # apply
```

For anything you are unsure of, run `--sql` first and read it. A migration you
have not read the SQL for is a migration you are running blind.

A refusing instance **recovers without a restart**: `schema_guard` re-checks on
every readiness probe while unhealthy, so running `migrate.py` against a
503-ing deployment brings it into service on the next probe.

## Backwards compatibility is mandatory: expand / migrate / contract

A deploy can be rolled back, and a rolled-back application runs against the
*newer* schema (the `ahead` state above is allowed precisely so that lever
exists). **A migration must therefore leave the previous application version
working.** Risky changes are split across releases:

| Phase | Release | What happens |
|---|---|---|
| **Expand** | N | add the new column — nullable, or with a server default. Old code ignores it. |
| **Migrate** | N | backfill; write both shapes; read the new one |
| **Contract** | N+1 or later, once nothing reads the old shape | drop the old column |

Rules that follow, and are enforced in review:

- **Never rename** a column in one step. Add → backfill → switch reads → drop.
- **Never add `NOT NULL`** without a server default to a populated table. Add
  nullable, backfill, then alter — as `0002` and `0005` both do.
- **Never drop** a column in the same release that stops using it.
- **Index a large existing table with `CREATE INDEX CONCURRENTLY`**, inside an
  `op.get_context().autocommit_block()` — it cannot run in a transaction. No
  migration in the current set does this; the first one that needs to must.
- **Data backfills belong in migrations**, not in boot code that re-runs forever.

A per-migration risk review of everything currently in the tree is in
[docs/engineering/MIGRATION-SAFETY-AUDIT.md](engineering/MIGRATION-SAFETY-AUDIT.md).

## How migrations are tested

Three layers, because the one that was missing is the one that mattered:

| Layer | What it proves | Where |
|---|---|---|
| **Drift** | a model changed and the migration is missing — `alembic` autogenerate against a freshly-migrated PostgreSQL must produce an **empty** diff | CI `migrations` job |
| **Upgrade path** | fresh → head; a production-shaped OLD database refuses, migrates, then serves and logs in; idempotent re-run; behind → refused; failed migration → non-zero exit and full rollback; unknown revision → fails closed | `backend/verify_pg_deploy.py` |
| **Guard logic** | the states, the 503 middleware, and the liveness/readiness split | `backend/test_schema_guard.py` |

All of it runs on **real PostgreSQL 18** in CI. That is not decoration: every
one of the 186 backend suites builds its schema with `create_all()` on a fresh
database, which is the single arrangement in which model/schema drift *cannot*
occur — which is exactly why they were all green throughout #513.

## What happens when a migration fails

1. `migrate.py` exits non-zero.
2. Railway aborts the deploy. **The previous deployment keeps serving.**
3. PostgreSQL's transactional DDL rolls the failed migration back completely —
   the database stays at the previous revision, not half-applied. (Asserted, in
   `verify_pg_deploy.py` scenario 5.)
4. Nothing to undo. Fix the migration and deploy again.

If a deploy somehow starts with pending migrations, the application refuses
traffic and `/readiness` names both revisions.

## Inspecting the current revision

```bash
python backend/migrate.py --status
```

or, against a running deployment, with no database access at all:

```bash
curl -s https://flowmes-production.up.railway.app/readiness
```

```jsonc
{
  "ready": true,
  "schema": {
    "state": "ok",
    "expected_revision": "0007_oem_service_clock",
    "current_revision": "0007_oem_service_clock"
  }
}
```

## Emergency recovery

| Situation | Do this |
|---|---|
| Migrations pending on a live deployment | `python backend/migrate.py` against it; readiness recovers on the next probe, no restart needed |
| A migration is wrong but applied | write a **forward** migration that corrects it. Prefer rolling forward. |
| The release is bad and the schema is fine | roll the application back. The `ahead` state is allowed for exactly this. |
| Data was destroyed | restore from the nightly artifact — `docs/BACKUP-AND-RESTORE.md`. A downgrade undoes schema, never data. |
| You need to see what would run | `python backend/migrate.py --sql` |

## Rolling back

```bash
alembic downgrade -1        # one revision
alembic downgrade <rev>     # to a specific revision
```

Rollback only undoes **schema**. If a migration deleted or transformed data,
the downgrade cannot invent it back — that is what the backups are for
(see `docs/BACKUP-AND-RESTORE.md`).

`0001_baseline` refuses to downgrade. There is nothing below it, and a silent
no-op would let someone believe they had rolled back to a state that never
existed.

## SQLite vs PostgreSQL

Production is PostgreSQL; the test suite runs on SQLite. SQLite cannot `ALTER`
most things in place, so `env.py` enables Alembic's batch mode automatically
when the dialect is SQLite (it rewrites the table instead). This is harmless on
PostgreSQL and it means the same migration file can be exercised against both.

Migrations that use PostgreSQL-only features must guard on
`op.get_bind().dialect.name` and say so in a comment.

## Files

| Path | Purpose |
|---|---|
| `backend/alembic.ini` | Alembic config. `sqlalchemy.url` is deliberately blank. |
| `backend/alembic/env.py` | Reads `DATABASE_URL`; refuses to run without it. |
| `backend/alembic/versions/` | Revisions. |
| `backend/migrate.py` | Deploy entry point; handles baseline adoption. |
| `backend/test_migrate.py` | Guards the adoption path and the empty baseline. |
