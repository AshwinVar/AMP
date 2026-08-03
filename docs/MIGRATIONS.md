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

## Deploying a migration

```bash
python backend/migrate.py --status    # what would run
python backend/migrate.py --sql       # the exact SQL, reviewable
python backend/migrate.py             # apply
```

For production, run `--sql` first and read it. A migration you have not read
the SQL for is a migration you are running blind.

Railway currently runs `migrate.py` **manually** — it is deliberately not in
the start command yet, because an automatic migration on every boot combined
with health-gated cutover means two instances can run migrations concurrently
during a deploy overlap. Adding it to the release phase is tracked separately.

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
