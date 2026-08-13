# #245 tenant backfill — preflight

**Date:** 2026-08-13 · **Script:** `backend/backfill_enterprise_tenants.py` ·
**Preflight:** `backend/preflight_backfill_245.py` (24 checks, green)

**Status: READY TO RUN, BLOCKED ON ONE DECISION THAT IS YOURS.** Everything that
can be verified without touching production has been. What remains needs
production, and I have no production database access — by design.

---

## What it changes

Seven tables were given a `tenant_code` column **nullable, with no blind
backfill** (ADR-0002, `tenancy.FAIL_SAFE_TENANT_TABLES`). Their pre-existing
rows are `NULL`, and the scoping hook's `tenant_code == <tenant>` never matches
`NULL`, so **those rows are currently hidden from everybody**.

The script assigns each `NULL` row to its owning tenant using a source already
in the data:

| Table | How the owner is determined |
|---|---|
| `Remnant`, `MaterialIssueSlip`, `GRNItem`, `CycleCountItem` | the tenant of the `InventoryItem` the row names |
| `GoodsReceiptNote` | its line items' tenant **if they all agree**, else the `received_by` user's tenant |
| `CycleCount` | its count items' tenant **if unanimous**, else the `counted_by` user's tenant |
| `AuditLog` | the `actor` username's tenant; `system`/unknown → left `NULL` |

## Why it still matters

Those rows are business records — goods receipts, issue slips, cycle counts,
remnants and the audit trail — that no customer can currently see. Until they
are assigned, the history predating the tenant column is invisible in the
product. It is not a security hole (hidden is the fail-safe direction); it is
data the customer owns and cannot reach.

## The actual risk, stated plainly

**This operation makes hidden rows VISIBLE to one tenant.** A wrong assignment
is not a bad number in a report — it is one customer's receipts, stock movements
and audit trail appearing inside another customer's workspace.

Data damage is reversible. **Disclosure is not.** That asymmetry is why the
checks below exist and why the rollback was added before anything runs.

---

## Preflight results

`python backend/preflight_backfill_245.py` — 24 checks, all passing. It runs the
real `plan()` against a fixture built to be adversarial, rather than reading it.

| Property | Result |
|---|---|
| A dry run writes nothing | verified |
| A row whose item names its owner is assigned that owner | verified |
| An **orphaned** `item_id` is left `NULL`, never guessed | verified |
| A GRN whose lines **agree** is assigned that tenant | verified |
| A GRN whose lines span **two customers** is not assigned from its lines | verified |
| …and is certainly not handed to the other customer | verified |
| A GRN with no lines and an unknown receiver is left `NULL` | verified |
| An audit row is assigned its actor's tenant | verified |
| A `system` audit row is left `NULL` | verified |
| **Nothing anywhere is defaulted to `DEFAULT`** | verified |
| A row that already has a tenant is **not in the plan at all** | verified |
| Running it twice leaves the database identical (idempotent) | verified |
| The rollback reverts exactly what was assigned | verified |
| …and leaves alone any row somebody has since changed | verified |
| A forged manifest naming another table is refused | verified |

**The assumption that mattered most, checked:** `User.username` is
`unique=True` — **globally unique** (`models.py:104`). The audit-log mapping is
`{username → tenant_code}`, so it cannot collide and cannot mis-assign an audit
row to the wrong customer. Had usernames been unique only per tenant, this whole
operation would have been unsafe.

## Idempotency

Yes. Only rows with `tenant_code IS NULL` are ever considered, so a second run
finds nothing to do. Proven, not assumed: the preflight applies twice and
compares full table snapshots.

## Downtime

None expected. It is a single transaction of `UPDATE`s on rows nobody can
currently read. It takes no locks on anything a request path writes, and it adds
no schema change. **The one thing I cannot size is how many rows** — see below.

## Rollback — this was missing, and now exists

The script's docstring offered "set the affected rows back to `NULL`", but
nothing recorded **which** rows, and after the fact a backfilled row is
indistinguishable from one the application wrote normally. The only undo would
have been restoring a backup — losing every write since.

Now every applied run writes a **manifest** into `event_log` (append-only, and
the factory reset never touches it) **inside the same transaction as the
writes**, so there is no path that changes rows without recording them.

```bash
python backend/backfill_enterprise_tenants.py --rollback
```

Each revert is conditional on the row still holding the value this script
assigned; if somebody has since moved it deliberately, that decision wins and
the row is reported as skipped.

## Backup

**Available and proven.** The `Database backup` workflow has run 14 times, most
recently 2026-08-13 04:22 UTC, all green — and it includes a restore-validation
job, so it is a proven round trip rather than a dump nobody has opened. It is
`workflow_dispatch`-enabled and its own comments name "before a risky migration"
as the reason.

**Take one immediately before applying.** Not because the rollback is doubted,
but because a backup is the only recovery from a class of mistake nobody
predicted.

---

## What I could NOT determine, and why

I have no production database access. `backend/.env` points at `localhost`,
there is no Railway CLI on this machine, and I will not handle production
credentials. So these remain open, and **only a dry run against production can
close them**:

1. **How many rows are affected**, per table and per tenant.
2. **Whether production already contains partially migrated data** — i.e.
   whether some of these rows already carry a tenant from an earlier attempt.
3. **How many rows are ambiguous** and would stay hidden.

The dry run answers all three and **writes nothing**.

---

## THE DECISION I NEED FROM YOU

The apply step assigns real customer records to real customers. That is your
call, not mine, and it should be made against production's actual numbers rather
than my fixture's.

**Step 1 — dry run (read-only, safe, no decision needed).**
On Railway, run against the production database:

```bash
python backend/backfill_enterprise_tenants.py
```

It prints a per-table, per-tenant plan and the ambiguous count, and exits
without writing.

**Step 2 — read the plan and tell me.** The things worth checking: does every
tenant named look like a real customer; is the ambiguous count small and
plausible; does any single tenant receive a suspiciously large share of the
audit log.

**Step 3 — only if the plan looks right.** Dispatch the `Database backup`
workflow, wait for it to go green, then:

```bash
python backend/backfill_enterprise_tenants.py --apply
```

**If anything looks wrong afterwards:**

```bash
python backend/backfill_enterprise_tenants.py --rollback
```

I have deliberately not wired this into a boot-time env flag the way
`RESEED_FACTORY` is. That pattern exists for an operation whose outcome is known
in advance; this one's outcome depends on data I have not seen, and it should be
run by a person who has just read the plan.
