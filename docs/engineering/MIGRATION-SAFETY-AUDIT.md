# Migration safety audit

Every migration in `backend/alembic/versions/`, reviewed for what it will do the
**first time it runs against production** — which, for all of them, is the
deploy that introduces [ADR-0018](../adr/0018-migrations-run-before-the-application-serves.md).

Until now Alembic never ran on production. `migrate.py` will stamp the baseline
and then apply 0002 → 0007 in one go, against a year of real data. This document
is the pre-flight check for that.

---

## The thing that makes this bounded

**PostgreSQL has transactional DDL.** Alembic runs each migration inside a
transaction, so a migration that fails halfway rolls back completely — the
database is left at the previous revision, not in a half-applied state. Proved,
not assumed: `verify_pg_deploy.py` scenario 5 adds a column, raises, and asserts
both that the column is gone and that `alembic_version` did not advance.

Combined with `preDeployCommand`, a failure means **the deploy aborts and the
previous build keeps serving**. The worst realistic outcome is "the release
doesn't ship", not "production is broken".

A backup exists as well: `.github/workflows/backup.yml` has been dumping
production nightly and proving the restore, most recently **2026-08-10, success**.

---

## Per-migration verdict

| # | Operations | Risk | Verdict |
|---|---|---|---|
| 0001 | none | none | Empty anchor. `upgrade()` is `pass` by design — it exists so an existing database can be stamped without claiming work was done. |
| 0002 | `add_column`, backfill `UPDATE`, `alter_column` → NOT NULL, **rename duplicate rows**, `create_unique_constraint` | **medium — writes data** | See below |
| 0003 | `drop_constraint` / `drop_index`, `create_unique_constraint` | low | See below |
| 0004 | `create_table` ×2, `create_index`, **seeds rows** | **medium — writes data** | See below |
| 0005 | `add_column` ×2, backfill, `alter_column` → NOT NULL | low | Guarded by `_has_column`; production already has both columns (added by #513's boot patches), so this is a no-op there. |
| 0006 | `create_table` ×5, `create_index` | low | Guarded by table-existence; production already has all five (created by `create_all`), so no-op. |
| 0007 | `add_column` ×2 (nullable, no backfill) | low | Guarded; production already has both (#513), so no-op. |

Every migration checks for the thing it is about to create. That is not
incidental — they were written for exactly this adoption scenario.

### 0002 — machine site identity

Adds `machines.site`, backfills `''`, makes it `NOT NULL`, then applies
`UNIQUE (tenant_code, site, name)`.

**It renames rows.** Before applying the constraint it finds machines sharing a
`(tenant, site, name)` identity and renames the later ones to `name#<id>`.

- It **deletes nothing** and touches no history — the rename is the least
  destructive way to make an identity constraint applicable.
- Every rename is logged at WARNING with the old and new name and the row id.
- If `create_all` already built the constraint (the model declares it), the
  function returns early and there can be no duplicates to rename.
- If duplicates existed *and* the constraint did not, this migration is the
  first thing that would have noticed.

**What to do:** read the deploy log for `renaming machine id=` lines. If any
appear, those machines were carrying a duplicate identity and the MQTT
resolution path (ADR-0011) was ambiguous for them already.

### 0003 — tenant-scoped document numbers

Converts single-column uniques (`work_orders.work_order_no`) to
`(tenant_code, col)`.

This is a **relaxation** — every row that satisfied the old constraint satisfies
the new one — so it cannot fail on existing data. On PostgreSQL the old
constraint name is *reflected*, not guessed, because it depends on server version
and column history. Tables the deployment has never created are skipped with a
log line rather than crashing the deploy.

### 0004 — per-tenant bills of materials

Creates two tables (skipped if present) and then **seeds legacy recipes**.

- A recipe is only written to a tenant that already stocks an item that recipe
  names — "demonstrably theirs already". A tenant holding neither the input nor
  the output is given nothing.
- Existing `(tenant, part_number)` rows are never overwritten.
- It is the one migration that inserts business data. It is idempotent, but the
  first run on production **will** create BOM rows for tenants that qualify.

**What to do:** check the row count in `bills_of_materials` after the deploy
against the log line the migration prints.

---

## Hazard checklist

| Hazard | Present? | Notes |
|---|---|---|
| Destructive column change | **no** | no `ALTER TYPE`, no narrowing |
| Column drop on upgrade | **no** | `drop_column` appears only in `downgrade()` |
| Table rewrite | **only on SQLite** | `batch_alter_table` rebuilds the table; on PostgreSQL these are plain `ALTER`s |
| Unsafe `NOT NULL` add | **no** | 0002 and 0005 both do add-nullable → backfill → set NOT NULL, three steps |
| Missing default | **no** | every NOT NULL add carries a server default |
| Large-table lock | **low** | the biggest tables (`production_records`, `iot_telemetry`) are untouched by every migration |
| `CREATE INDEX` blocking writes | **yes, briefly** | 0004/0006 index tables they just created (empty). No index is added to an existing populated table. |
| Constraint addition that can fail | **0002 only** | mitigated by the de-duplication pass that runs first |
| Irreversible operation | **no** | every migration has a real `downgrade()` |
| Data backfill | **0002, 0004, 0005** | all idempotent and guarded; described above |

**No migration in this set adds an index to an existing populated table**, which
is the classic production-lock hazard. When one does, it must use
`CREATE INDEX CONCURRENTLY` inside an `autocommit_block()` — noted in
`docs/MIGRATIONS.md` rather than left to be rediscovered.

---

## Order of operations for the ADR-0018 deploy

1. Confirm the nightly backup is green (it was, 2026-08-10).
2. Merge. Railway builds, then runs `python migrate.py` as the pre-deploy step.
3. If it exits non-zero the deploy aborts and the **current build keeps
   serving** — nothing to undo.
4. If it succeeds, the app starts, `/readiness` returns 200 and traffic cuts
   over.
5. Verify: `/readiness` shows `state: "ok"` with `current_revision` =
   `expected_revision`, and login works.

### If something goes wrong

| Symptom | Cause | Action |
|---|---|---|
| Deploy aborts at the pre-deploy step | a migration failed | read the log; the database is unchanged and the old build is serving. Fix the migration and redeploy. |
| Deploy starts but `/readiness` is 503 with `state: "behind"` | the pre-deploy step did not run | check `preDeployCommand` in the service settings; run `python migrate.py` against the database to recover immediately (readiness re-checks without a restart) |
| `/readiness` 503 with `state: "unmanaged"` | production has no `alembic_version` | the pre-deploy step did not run. Same fix. |
| Login fails after a green deploy | not this class | `/readiness` will say `ok`; look elsewhere |
| A migration corrupted data | — | restore from the nightly artifact per `docs/BACKUP-AND-RESTORE.md` |

`python migrate.py --status` reports current vs head and changes nothing.
`python migrate.py --sql` prints the exact SQL without running it — the reviewable
form, and the right thing to read before any migration you are unsure of.
