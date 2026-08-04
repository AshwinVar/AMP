# Data retention policy

Several AMP tables grow forever. `analytics_routes.py` describes `iot_telemetry`
in its own comment as "an unbounded, never-pruned table", and the demo simulator
writes to it every 45 seconds in production. This document is the policy that
stops that, and the reasoning behind each number.

## The policy

| Table | Timestamp | Keep | Why this number |
|---|---|---|---|
| `iot_telemetry` | `created_at` | **14 days** | Fastest-growing table in the schema. The connectivity read-model only looks at a 15-minute freshness window; nothing reads older telemetry. |
| `industrial_signals` | `created_at` | **14 days** | Same shape, same reasoning — per-tick signal rows with no long-horizon consumer. |
| `machine_events` | `created_at` | **180 days** | The closest thing to a machine's service record. `/analytics/machine-state-summary` tallies status counts over the whole history, so this is not pure noise. Six months keeps a seasonal comparison. |
| `notifications` | `created_at` | **90 days** | The UI lists the newest 500 and the only aggregate is an unread count. A notification nobody opened in a quarter will not be opened. |
| `ai_recommendations` | `created_at` | **365 days** | The AI advice log. A year lets you answer "did the maintenance agent warn us before that failure?" — the first question anyone asks after an incident, and unanswerable if you pruned the evidence. |
| `agent_actions` | `decided_at` | **365 days** | The agent oversight trail (ADR-0005). Pruned on `decided_at`, so a proposal still sitting in an approval queue has a NULL timestamp and is kept forever. A retention job must never quietly clear a work queue. |
| `report_requests` | `created_at` | **180 days** | A request log, not an archive — reports are generated on demand and never stored. |
| `inventory_transactions` | `created_at` | **1095 days** | The stock ledger. Not log noise: `ai/trace` reconstructs a work order's material history from it with no time filter, and it carries financial and traceability weight. Three years. **If a tenant needs longer, raise this — do not shorten it to make the numbers look better.** |
| `audit_logs` | — | **forever** | The security audit trail. It exists to answer "who did that, and when". An audit trail you prune on a timer is not an audit trail. |
| `event_log` | — | **forever** | The append-only domain event history (ADR-0001). It is the substrate a projection could be rebuilt from; deleting it destroys the ability to reconstruct anything. |

## Three rules the module enforces

**Dry run is the default.** `--apply` is required to delete anything. An
operational script that deletes on its default invocation is a loaded gun.

**NULL timestamps are kept, never deleted.** A row whose age is unknown is not
the same as a row that is old. Treating unknown as expired is how a retention
job quietly destroys data it was never meant to touch. This is also what makes
the `agent_actions` policy safe.

**Deletes are batched.** A first run against months of accumulation would
otherwise be one enormous transaction holding locks on a live database, turning
a cleanup into an outage.

## Tenancy

This is an operational job with no request context, so the ADR-0002 auto-scoping
hook is inert and the job sees every tenant's rows. That is correct here — a
per-tenant prune would leave the unbounded tables unbounded for whichever tenant
nobody remembered to run.

To make sure that cannot become a hole, the module **refuses a policy on any
table whose tenant column is nullable**, so it can never delete rows that a
tenant-scoped read would have hidden. `test_bulk_write_scoping.py` allowlists
`retention.py` with that reasoning recorded rather than silently skipping it.

## Running it

```bash
python backend/retention.py                 # report what would go
python backend/retention.py --apply         # do it
python backend/retention.py --days 30       # override the window
python backend/retention.py --tables iot_telemetry
```

The `--days` override cannot resurrect an exempt table: `audit_logs` and
`event_log` stay exempt even at `--days 0`. Changing that requires editing the
policy table, which is a reviewed code change.

## Scheduling

`.github/workflows/retention.yml` runs a **dry run every Sunday at 03:41 UTC**
and writes the report to the workflow summary. It never deletes on a schedule.

Deleting requires a manual dispatch with `apply=true`. That asymmetry is
deliberate: an automated job that removes production rows unattended is a thing
you regret exactly once, and the weekly report is enough to notice a table
growing before it becomes a problem.

It runs in GitHub Actions rather than in the app because AMP has no worker
process — the Procfile defines only `web:`. Putting a scheduler in the web
process would mean every future replica racing to prune the same tables.

## Not covered

**Temporary files.** There are none. CSV exports stream inline from the request
and nothing is written to disk, so there is no file cleanup to schedule. If file
storage is ever added, it needs its own policy here.

**Backups.** Dump retention is a property of the backup workflow's artifact
`retention-days`, not of this job. See `docs/BACKUP-AND-RESTORE.md`.
