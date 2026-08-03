# AMP — backup and restore

AMP ran for its first two months with **no database backups of any kind**.
Railway's Postgres plugin does not back up on the hobby tier, nothing else was
taking a copy, and `docs/GMATS-Pilot-Proposal.md` §6 was meanwhile selling
"daily database backups" to a paying customer. One dropped table, one bad
migration or one deleted volume would have ended the company, and would have
done it while we were contractually promising the opposite.

This page describes what now runs, how to prove it works, and exactly what to do
at the worst possible moment.

Two things are worth internalising before anything else:

1. **A backup you have never restored is not a backup.** The deliverable of this
   work is `scripts/restore_check.sh`, not the nightly dump.
2. **None of this works until a human sets one repository secret.** See below.

---

## 1. Manual setup — required before any of this functions

The workflow cannot create its own credentials. Until this is done, the nightly
run fails on its first step with a message pointing back here. That is
deliberate: a backup system that quietly does nothing is worse than none,
because it buys false confidence.

### 1.1 Add the `DATABASE_PUBLIC_URL` repository secret

1. Railway → the **Postgres** service → **Variables** → copy
   `DATABASE_PUBLIC_URL`.
   It looks like `postgresql://postgres:<password>@<something>.proxy.rlwy.net:<port>/railway`.
   **It must be the public URL.** Railway's internal `DATABASE_URL`
   (`*.railway.internal`) resolves only inside Railway's private network and a
   GitHub runner cannot reach it.
2. GitHub → the AMP repository → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
3. Name: `DATABASE_PUBLIC_URL`. Value: the string from step 1. Save.

### 1.2 Secrets this repository's backup workflow reads

| Secret | Required | Used for |
|---|---|---|
| `DATABASE_PUBLIC_URL` | **Yes** | The connection string `pg_dump` reads production from. Set it manually as above. |
| `DATABASE_URL` | No — fallback only | Used only if `DATABASE_PUBLIC_URL` is unset. It exists so that setting the more obvious name still produces backups rather than silence, and it only works if the value happens to be publicly reachable. Prefer `DATABASE_PUBLIC_URL`. |

No other secret is involved. The restore drill runs against a throwaway Postgres
service container inside the same workflow run and needs no credentials.

### 1.3 Confirm it works, once, by hand

GitHub → **Actions** → **Database backup** → **Run workflow**. Both jobs must go
green. If `pg_dump` fails on a version mismatch, bump `postgresql-client-17` in
`.github/workflows/backup.yml` to match the Railway server major version.

### 1.4 Rotate the secret whenever the database password changes

Railway rotates credentials on some plan changes and on manual password resets.
The nightly run turns red the same night, which is the intended behaviour — go
and repeat §1.1.

---

## 2. What runs, and when

`.github/workflows/backup.yml` — two jobs, nightly at **02:17 UTC** and on
manual dispatch.

**Job `dump`**

1. Installs a `pg_dump` at least as new as the server (the hosted runner image
   trails current Postgres, and `pg_dump` refuses to dump a newer server).
2. `pg_dump --format=plain --no-owner --no-privileges --clean --if-exists`,
   piped through `gzip -9`.
3. Asserts the result is actually a backup: intact gzip, at least 10 KB, at
   least 40 `CREATE TABLE` statements, at least one `COPY` block. A schema-only
   or truncated dump fails the run instead of being uploaded.
4. Uploads it as a GitHub Actions artifact named `amp-db-backup-<UTC stamp>`
   containing `amp-<UTC stamp>.sql.gz`, retained **30 days**.

**Job `restore-drill`**

Downloads the artifact just produced, restores it into a throwaway Postgres 17
service container via `scripts/restore_check.sh`, and asserts the result is a
real database. This is the job that matters. It also exercises the artifact
download path — an artifact you cannot retrieve is not a backup either.

A red `restore-drill` with a green `dump` means the file exists but cannot be
relied on. Treat it as an incident, not a flaky test.

### Why the dump uses those flags

| Flag | Reason |
|---|---|
| `--no-owner`, `--no-privileges` | The dump must restore into a database owned by whichever role the restorer has — a local `postgres` during a drill, Railway's own role in production. Ownership metadata is not worth losing a restore over. |
| `--clean --if-exists` | Makes the file self-sufficient for an in-place restore over a live database, which is what §5 does. It is also precisely why §5 step 5 is destructive: the dump opens by dropping every object it is about to recreate. |
| `--format=plain` | Restorable with nothing but `psql`, and greppable when you need to know what is in it before you commit. Custom format buys parallel restore, which AMP's data volume does not yet need. |

---

## 3. RPO and RTO

**RPO (how much data a disaster loses): up to 24 hours.** Everything written
since the last successful 02:17 UTC run is gone. In practice slightly worse:
GitHub's scheduled runs are best-effort and can be delayed under load, so budget
24 hours plus skew. There is no point-in-time recovery.

**RTO (how long recovery takes): target 30–60 minutes**, almost all of it human.
The mechanical part — download, verify, apply — is a few minutes at current data
volume. The rest is deciding, stopping writes, and checking afterwards. It is
30 minutes only for someone who has read this page before the incident; it is
several hours for someone reading it for the first time under pressure. That is
the argument for running §4 by hand once a quarter.

---

## 4. The customer-facing promise, and where it stands

`docs/GMATS-Pilot-Proposal.md` §6 tells the customer:

> Hosted on managed cloud (Railway/AWS) with **daily database backups**

With this workflow live and the secret set, that sentence is true: daily, off-box,
and verified by restore. What it does **not** promise, and what should not be
implied in any conversation, is:

- point-in-time recovery (there is none — worst case is a lost day),
- a recovery-time guarantee (§3 is a target, not an SLA),
- geographic redundancy (one copy, in GitHub's artifact store),
- retention beyond 30 days.

If a customer contract needs any of those, §7 is the upgrade path and it costs
money rather than effort.

---

## 5. Restoring into production during an incident

Read all of it before starting. Step 5 is irreversible.

> **Before anything: is a restore actually the right move?** A restore costs
> every write since 02:17 UTC. If the damage is one table or one tenant, restore
> the dump to a *scratch* database (§6) and copy the rows back instead. Restoring
> the whole database over a partially-healthy one turns a small loss into a
> day-sized one.

**1. Stop the writes.**
Railway → **AMP** service → the active deployment → remove it (or scale the
service to zero). Every minute the API stays up is another minute of writes you
are about to discard, and a write landing mid-restore can leave the data
inconsistent in ways the dump cannot explain.

**2. Fetch the dump you want.**
GitHub → **Actions** → **Database backup** → the run for the date you want →
**Artifacts** → download → unzip. Or, with the `gh` CLI:

```bash
mkdir -p backups
gh run list --workflow "Database backup" --limit 10
gh run download <run-id> -D backups          # yields backups/amp-<stamp>.sql.gz
```

**3. Verify the dump before it goes anywhere near production.**
Never restore a file you have not just proved restorable. Against any local
Postgres:

```bash
bash scripts/restore_check.sh backups/amp-<stamp>.sql.gz
```

`RESTORE CHECK PASSED` — continue. Anything else — stop, and try the previous
day's artifact. Do not proceed on a FAIL, however plausible the file looks.

**4. Snapshot the broken database first.**
This is the step people skip and regret, because it is the only copy of whatever
evidence the incident left behind — and your only way back if the restore turns
out to be the wrong call.

```bash
export PROD_URL='<the Railway DATABASE_PUBLIC_URL>'
pg_dump --dbname="$PROD_URL" --format=plain --no-owner --no-privileges \
  | gzip -9 > pre-restore-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
```

**5. DESTRUCTIVE — apply the dump over production.**

> This command **drops and recreates every object in the target database**. The
> dump was taken with `--clean --if-exists`, so its first few hundred lines are
> `DROP` statements. Everything currently in `$PROD_URL` is gone the moment it
> runs. Read the value of `$PROD_URL` out loud and confirm it is the database
> you mean to overwrite before pressing return.

```bash
echo "$PROD_URL" | sed -E 's#//[^@]*@#//***:***@#'    # confirm host+db, not the password
gzip -dc backups/amp-<stamp>.sql.gz \
  | psql -X -v ON_ERROR_STOP=1 --single-transaction -d "$PROD_URL"
```

`--single-transaction` means a failure part-way leaves the database untouched
rather than half-restored. If it fails, nothing was lost by trying; read the
error and go back to step 3.

**6. Check for an armed `RESEED_FACTORY` before restarting.**
AMP-specific and genuinely dangerous. `main.py`'s startup rebuilds the factory
when `RESEED_FACTORY` is set, and skips it only when a matching `FactoryReseeded`
row already exists in `event_log`. Restoring a dump taken *before* that flag was
consumed restores an `event_log` without the marker — which **re-arms the reseed
and wipes the database you just restored** on the next boot. Railway → AMP
service → Variables → delete `RESEED_FACTORY` if it is present at all.

**7. Bring the API back and smoke-test it.**
Redeploy the AMP service, then run the post-deploy check from
`docs/Production-Setup.md` §7 — `/health` **and** a `POST /login`, because a GET
alone will not catch a middleware or body-handling failure.

**8. Confirm the data, then say so out loud.**
Log in, check machine count, work orders and stock against what you expect for
that dump's timestamp. Then tell the customer which window was lost — between
the dump's UTC stamp and the incident — before they find it themselves.

### Restoring under different application code

The schema migrations in `main.py` and `tenancy.py` are additive and idempotent
(`create_all`, `ensure_tenant_columns`), so an **older** dump restored under
**current** code self-heals on boot: missing columns get added and backfilled.
The reverse is not supported. Never restore a dump that is newer than the
deployed code — roll the code forward first.

---

## 6. Restoring to a scratch database (non-destructive)

For reading yesterday's data, recovering one table, or answering "what did this
look like before?", restore into a throwaway database and leave production
alone. That is what `RESTORE_CHECK_KEEP=1` is for:

```bash
RESTORE_CHECK_KEEP=1 bash scripts/restore_check.sh backups/amp-<stamp>.sql.gz
# ... PASS, and the database amp_restore_check_<stamp>_<pid> is left in place
psql -d postgres://postgres:postgres@localhost:5432/amp_restore_check_<stamp>_<pid>
```

Drop it yourself when you are done.

---

## 7. Running the drill by hand

The nightly CI drill covers the automated case. Run it locally once a quarter,
and always before a risky migration, so that the human path is also known to
work.

```bash
# 1. any local Postgres will do
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres --name amp-scratch postgres:17

# 2. newest dump in ./backups, or pass a path explicitly
bash scripts/restore_check.sh

# 3. tidy up
docker rm -f amp-scratch
```

What it asserts, and the failure each assertion pins:

| Check | Catches |
|---|---|
| gzip integrity | A truncated or corrupted artifact, named as corruption rather than as a mystery SQL error 40,000 lines in. |
| `psql` replays the whole dump under `ON_ERROR_STOP` | A restore that "mostly worked". |
| At least 40 tables | A dump taken against the wrong database, or one that stopped early. |
| `users`, `machines`, `work_orders` all hold rows | The failure that looks like success: a `--schema-only` dump restores perfectly and hands you a factory with no machines in it. |
| Every `users` row has an intact password hash | A restore that loses the password column locks everyone out of their own factory. Length, not prefix — `security.py` still accepts legacy SHA-256 hashes alongside bcrypt. |
| Data age | Reported and warned on, never failed on. Staleness is a property of the schedule, not of the file, and a check that cries wolf gets muted. |

Tunable via environment: `BACKUP_DIR`, `RESTORE_CHECK_ADMIN_URL`,
`EXPECTED_MIN_TABLES`, `REQUIRED_TABLES`, `WARN_DATA_AGE_DAYS`,
`RESTORE_CHECK_KEEP`, `RESTORE_CHECK_ALLOW_REMOTE`. The script issues
`CREATE DATABASE` and `DROP DATABASE`, so it refuses hosted-looking hosts
outright and will only ever drop a database whose name it generated itself.

---

## 8. Known limits — read these before relying on any of it

- **The artifact is readable by anyone with repository read access**, and by any
  token that can call the Actions API. It is a complete production database:
  customer data and bcrypt password hashes, sitting in GitHub's artifact store
  for 30 days. **Keep the repository private.** This is the single strongest
  argument for §9.
- **The restore drill decompresses production data onto a GitHub-hosted
  runner.** Third-party data egress, already true of the dump itself, but it
  needs saying to a customer who asks.
- **One copy, one provider.** GitHub is both the CI system and the backup store.
  If GitHub is unavailable during the incident, so is the backup.
- **GitHub disables scheduled workflows in repositories with no activity for 60
  days.** A quiet repository silently stops backing up. Check the Actions tab
  monthly; the drill's data-age warning is the second line of defence, not the
  first.
- **30-day retention.** Corruption discovered on day 31 is unrecoverable.
- **No point-in-time recovery.** Worst case is a lost day (§3).
- **Nothing here backs up anything but Postgres.** Environment variables,
  Railway configuration and Vercel settings are not captured; keep
  `docs/Production-Setup.md` accurate, because it is effectively the backup of
  everything that is not a row.

---

## 9. When to move off this

This design is chosen for a single-engineer product with one paying pilot: free,
legible, and provably restorable. Move when any of these becomes true.

| Trigger | Move to |
|---|---|
| A contract requires PITR or an RTO guarantee | Railway Postgres on a plan with managed backups; keep this workflow as the off-provider second copy. |
| More than one customer's data is in the database | Push the dump to S3/B2 with server-side encryption, object lock and lifecycle rules, and stop retaining full dumps in a code-hosting artifact store. |
| Losing a day becomes unacceptable | WAL archiving (`wal-g` or the provider's own), for minutes-level RPO. |
| The dump starts taking more than a few minutes | Switch to `--format=custom` and restore with `pg_restore -j`. |
