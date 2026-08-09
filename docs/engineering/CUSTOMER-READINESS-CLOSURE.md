# Customer-readiness closure

**Question:** would you approve AMP today for three independent manufacturing
customers?

**Answer: YES, WITH CONDITIONS.** The conditions are listed in §9 and are
operational, not architectural. Every blocker that made multi-tenancy unsafe is
closed and verified on real PostgreSQL; what remains is a backup interval, a
performance ceiling, and four schema limits that are documented rather than
fixed.

Date: 2026-08-09 · master `16b9c3c` (this branch) on top of `6adeb55`

---

## 1. What was closed

| # | Blocker | PR | Decision record |
|---|---|---|---|
| 1 | MQTT resolved a machine by name across tenants | [#502](https://github.com/AshwinVar/AMP/pull/502) | ADR-0011 |
| 2 | Document numbers were globally unique | [#503](https://github.com/AshwinVar/AMP/pull/503) | ADR-0012 |
| 3 | A bill of materials was global | [#504](https://github.com/AshwinVar/AMP/pull/504) | ADR-0013 |
| 4 | OEE meant a different thing on every surface | [#505](https://github.com/AshwinVar/AMP/pull/505) | ADR-0014 |
| 5 | Approvals were enforced only at the HTTP route | [#506](https://github.com/AshwinVar/AMP/pull/506) | ADR-0015 |
| 6 | `/ws/live` authenticated nothing | [#507](https://github.com/AshwinVar/AMP/pull/507) | ADR-0016 |

Each was found by measurement, not by reading. The measurements are in the
decision records; the shortest summary of why they mattered:

- three customers' `CNC-01` were **one machine**;
- **20 of 20** business codes could not be reused by a second tenant, so
  FACTORY_B's first issue slip 500'd;
- FACTORY_B's stock moved at **GMATS's** recipe, and a customer's own product
  moved nothing;
- the dashboard said **100%** while the assistant said **10%** about the same
  factory at the same moment, and a comms failure *improved* plant OEE from
  67% to 94%;
- a **deleted user** could approve a purchase order, and a second call flipped
  the audit record to "Rejected" while the order stayed "Approved";
- a **deleted user's token** kept streaming their factory's live telemetry.

## 2. Test and verification counts

| Measure | Value |
|---|---|
| Backend suites | 179, all green |
| Backend tests (pytest, one process) | 1122 passed, 1 skipped |
| Backend coverage | **81.64%** (gate: 78%) |
| Frontend tests | 256 across 22 files |
| Mutation matrices | 5 files, **84 mutations**, 83 caught + 1 documented shadow |
| Adversarial attempts (final re-audit) | **37**, all refused cleanly |
| Three-customer isolation assertions | **75** across 11 surfaces |
| WebSocket auth assertions | 51 (SQLite **and** PostgreSQL 18.3) |
| eslint | 134 (baseline, unchanged) |

The skipped test is documented in place: `test_mqtt_ingest_without_orm_hooks.py`
needs a process where `tenancy.install_scoping()` has not run, which pytest's
shared process cannot give it. The per-file runner does, and it passes there.

### The coverage gate, and a correction

Seven new verification harnesses (`verify_pg_approvals`, `mutate_approval_gate`,
`mutate_ws_auth`, `audit_three_customers`, `audit_adversarial_final`,
`loadtest`, `restore_drill`) dropped the figure from 80.24% to **76.07%** —
below the 78 floor — without one line of application code changing.

`backend/.coveragerc` already anticipated exactly this. It carries an explicit
policy, written when the earlier `mutate_*` and `verify_pg_*` harnesses were
added, that counting operator tooling "would let adding verification LOWER the
number that describes how well the application is tested". The new harnesses
simply were not on its list. Adding them, in the same block and the same style,
is the whole fix.

| Basis | Statements | Coverage |
|---|---|---|
| Before the campaign | 11,740 | 80.24% |
| New harnesses counted as application code | 11,778 | **76.07%** ← failed |
| **New harnesses listed, as the policy requires** | **11,550** | **81.64%** |

The rise from 80.24% to 81.64% is not an accounting trick: `ws_auth.py` and the
`approvals.py` additions are real application code and are well covered by the
suites that ship with them.

**A correction worth recording.** The first attempt at this checked for a
coverage config from the repository root rather than `backend/`, found nothing,
concluded none existed, and *overwrote* the real one — discarding a carefully
reasoned file. It was restored from git and amended by hand instead. Two
intermediate figures produced during that detour (89.62% and 82.94%) described
configurations that never existed on master and mean nothing; they are recorded
here only so that nobody later finds them in the history and takes them for
measurements.

### One order-dependent flake, found and fixed

`test_ai_copilot_context.py::test_context_oee_is_pooled_not_mean_of_ratios`
failed once in a full pytest run and passed standalone every time. Rather than
retry it, the condition was reproduced deterministically: bind a tenant before
importing the module and every assertion in the file fails, because
`_build_factory_context` reads through the ADR-0002 scoping hook and an earlier
suite had left a different tenant bound. The fixture now binds its own tenant.
Verified by re-running the file with `FACTORY_Z` deliberately leaked.

**Mutation matrices**, all on the guards this campaign added:

| Harness | Mutations | Result |
|---|---|---|
| `mutate_mqtt_identity.py` | 15 | all caught |
| `mutate_doc_numbers.py` | 11 | all caught |
| `mutate_bom.py` | 15 | all caught |
| `mutate_oee_contract.py` | 16 | all caught |
| `mutate_approval_gate.py` | 26 | all caught |
| `mutate_ws_auth.py` | 17 | 16 caught, 1 shadow with a written reason |

## 3. PostgreSQL 18.3 results

Nothing in this campaign is trusted on SQLite alone. SQLite has hidden three
defect classes here already: `Decimal` vs `float` from `SUM`, `NULL != NULL`
voiding a UNIQUE over a nullable column, and VARCHAR lengths being ignored.

| Verification | Result |
|---|---|
| `verify_pg_migration.py` | migrations apply and reverse |
| `verify_pg_docnumbers.py` | per-tenant sequences hold under concurrency |
| `verify_pg_bom.py` | per-tenant BOM resolves correctly mid-upgrade |
| `verify_pg_approvals.py` | migration 0005 over a **populated** users table; NOT NULL enforced by the database; reverses without losing users |
| `test_live_ws_auth.py` | 51 assertions green on PostgreSQL |
| `audit_three_customers.py` | 75 assertions, 11 surfaces |
| `audit_isolation.py` | runtime read isolation, every scoped model |

Migration 0005 was applied to a users table that **already had rows** — adding a
NOT NULL column to an empty table proves nothing. Existing users came out
active, so no login breaks on deploy.

## 4. OEE golden datasets

`backend/test_oee_contract.py` — 12 datasets, every expected value derived **by
hand** from ADR-0014 and written out longhand beside the assertion. None is
copied from a program run; if the implementation and the expectation came from
the same code, the test would only say the code equals itself.

| # | Dataset | Expected |
|---|---|---|
| 1 | A perfect shift | A 100, P 100, Q 100, OEE 100 |
| 2 | Textbook (480/400/30 s/600/570) | A 83, P 75, Q 95, **OEE 59** |
| 3 | Pooling vs averaging | **OEE 90**, not the naive 54 |
| 4 | Unplanned period | `has_data` false, every component `None` |
| 5 | Scheduled, ran, produced nothing | A 1.0, P 0.0, Q `None` — a *real* zero |
| 6 | Scrap (100 made, 90 good) | Q 90 |
| 7 | Impossible values | clamped to [0, 100] both ways |
| 8 | Window boundary | start included, end excluded |
| 9 | A silent machine | coverage 2/3 = 67%, `complete` false |
| 10 | Two tenants, nothing bound | 100% and 25%, neither sees the other |
| 11 | Dashboard vs AI assistant | identical number, identical window |
| 12 | Partial coverage in AI prose | stated, not hidden |

Independently, `audit_three_customers.py` hand-derives three more against live
PostgreSQL: **FACTORY_A 59%, FACTORY_B 100%, FACTORY_C 17%**.

## 5. Approval-gate results

Every bypass the original probe found now refuses. The last two rows are CONTROL
assertions — without them every result above is equally satisfied by a gate that
refuses everything.

| Attempt | Before | After |
|---|---|---|
| `apply_decision()` direct, no actor | **accepted** | refused, PO stays Draft |
| `apply_decision()` twice | action→Rejected, PO→Approved | refused; record and PO agree |
| Deleted user approves | **accepted** | 401 |
| Disabled user approves | *(no such concept)* | 403 |
| Token claims Admin, DB says Operator | *(never checked)* | 403 |
| Another tenant's Admin | 404 | 404 |
| 400-day-old proposal | **accepted** | 409 |
| Explicitly expired | *(no such concept)* | 409 |
| Misspelled decision | silently **rejected** the item | 400 |
| Malformed actor (string/list/int) | **AttributeError → 500** | 401 |
| **Valid Admin approves** | works | **works** |
| **Auto-approval policy** | works | **works**, minus the freshness bypass |

Idempotency was considered and deliberately not added: replay is already refused
by the state check, so a duplicate request is a 400 rather than a second effect.
An idempotency key would change the status code, not the outcome.

## 6. WebSocket isolation results

| Attempt | Before | After |
|---|---|---|
| No token | **accepted**, tenant `None` | 4401 "A token is required" |
| Garbage / wrong-secret | **accepted** | 4401 "Invalid token" |
| **Expired** token | **accepted** | 4401 "Token expired" |
| **Deleted** user | **accepted, bound to FACTORY_A** | 4403 |
| **Disabled** user | *(no such concept)* | 4403 |
| Token claims another workspace | *(never checked)* | 4403 |
| Client sends a frame | silently ignored | 4400, closed |
| Tenant-less payload → anon sockets | **delivered** | reaches nobody |
| **Valid Admin / Operator** | works | **works** |
| **FACTORY_A → FACTORY_B** | never leaked | still never leaks |
| **Reconnect** | works | **works** |

Close codes are split by what the client can do (4401 = get a new credential,
4403 = stop). Without that split the browser's retry loop would turn every
revoked session into a reconnect every 30 seconds, forever.

## 7. Three-customer isolation

FACTORY_A, FACTORY_B and FACTORY_C, all using **CNC-01, LINE-01, WO-001,
INV-001, PO-001, FG-001**, differing only in the data behind those names. On
PostgreSQL 18.3, 75 assertions, no crossover:

| # | Surface | Result |
|---|---|---|
| 1 | 9 scoped models × 3 tenants | only own rows |
| 2 | Machine identity | `CNC-01` → 3 machines (Chennai / Pune / Coimbatore) |
| 3 | Document numbers | all three wrote `WO-2` without colliding |
| 4 | BOM | RM-STEEL ×2 kg / RM-ALLOY ×5 kg / RM-RESIN ×0.5 L |
| 5 | Inventory | 100 kg / 250 kg / 7 L |
| 6 | OEE | 59% / 100% / 17%, hand-derived |
| 7 | MQTT | A's topic moved A's machine only |
| 8 | WebSocket | A's broadcast reached A only |
| 9 | Approvals | A's and C's Admins refused B's action |
| 10 | AI context | no other tenant code or site name present |
| 11 | CSV exports | 7 exports × 3 tenants, no foreign values, own data present |

## 8. Load and recovery

### Load — `backend/loadtest.py`, PostgreSQL 18.3

k6 is not installed and installing it would mean fetching a binary from the
network, so the driver uses `requests` + a thread pool against a **local**
uvicorn on a **disposable** database. It never touches production. Client floor
measured at ~8 ms; **zero errors at every scale**.

| Endpoint | 10 machines | 1000 machines |
|---|---|---|
| `/machines` | 258 rps · p50 24 ms | 41 rps · p50 189 ms |
| `/oee/summary` | 221 rps · p50 28 ms | 111 rps · p50 54 ms |
| `/work-orders` | 238 rps · p50 27 ms | 112 rps · p50 51 ms |
| `/inventory/items` | 234 rps · p50 28 ms | 59 rps · p50 162 ms |
| `/downtime-logs` | 240 rps · p50 27 ms | 165 rps · p50 36 ms |
| `/agent-actions` | 246 rps · p50 26 ms | 101 rps · p50 66 ms |
| **`/analytics/summary`** | 72 rps · p50 94 ms | **18 rps · p50 492 ms** |
| **`/analytics/executive-oee`** | 82 rps · p50 81 ms | **14 rps · p50 615 ms** |

p95/p99 at 1000 machines: `/analytics/executive-oee` 726/813 ms; everything else
under 290 ms.

**First bottleneck: the two `/analytics` endpoints.** Slowest at 10 machines and
worst at 1000 — `executive-oee` degrades 7.6× against 4.6× for `/machines`.

**It is not the database.** The same aggregates without HTTP stay flat as data
grows: plant OEE 1.43 → 1.76 ms, count machines 0.67 → 0.86 ms, list machines
0.36 → 3.59 ms. The cost is per-request application work.

WebSocket and MQTT are nowhere near the constraint: 1000 concurrent connections
register in 0.34 ms, a broadcast round to all 1000 takes 0.08 ms (11.8M
frames/sec), and the real MQTT handler ingests **~40,000 messages/sec** to
PostgreSQL at every scale.

### Recovery — `backend/restore_drill.py`

`pg_dump` → new empty database → restore → `alembic upgrade head` → boot AMP →
three customers log in → verify their data and their isolation. Monotonic clock
on every phase.

| Phase | Measured |
|---|---|
| pg_dump | 0.27 s |
| create empty database | 1.42 s |
| restore | 0.73 s |
| alembic upgrade head | 1.31 s |
| boot AMP | 3.05 s |
| customers log in | 0.58 s |
| verify data + isolation | 0.11 s |
| **MEASURED RTO** | **7.47 s** |

**Measured RTO: 7.47 s** — for a small dataset, locally, with the dump already
on disk. A real recovery adds artifact download and a larger dump. This measures
the software's part of an outage, not the whole outage.

**RPO: 24 hours, and it is NOT measured — it is read from the schedule.**
`.github/workflows/backup.yml` runs `cron: "17 2 * * *"`, once a day. A failure
at 02:16 UTC loses almost a full day of production, quality and inventory
movements. No restore drill can measure this; only the backup interval sets it.

## 9. Remaining risks

### P0 — none open

### P1

| Risk | Evidence | Fix |
|---|---|---|
| **RPO is 24 hours.** A daily dump means a bad day loses a day of shop-floor data — the data a factory is least able to reconstruct. | `backup.yml` `cron: "17 2 * * *"` | More frequent dumps, or continuous archiving (PITR). This is a schedule change, not a code change. |
| **`/analytics` degrades 7.6× by 1000 machines** (p50 615 ms, p99 813 ms). Not fatal at three customers, but it is the first thing that will break. | §8 | Profile the two handlers; the DB is flat, so the cost is in Python. |

### P2

| Risk | Evidence | Note |
|---|---|---|
| **L1 — downtime and availability do not reconcile.** Availability comes from `ProductionRecord`; downtime from `DowntimeLog`. Two tables, no link. Measured: a shift losing 80 min by the production record carried 120 min of downtime logs. | ADR-0014 | Needs a reconciliation report, or availability derived from the downtime ledger. |
| **L2 — overlapping downtime is not representable.** `DowntimeLog` stores a duration and a `created_at`, not an interval. Two operators logging the same hour produce 120 min of loss from 60 min of stoppage. | ADR-0014 | Needs an interval column. |
| **L3 — rework is invisible.** A reworked unit is counted good or scrap; first-pass yield cannot be derived. | ADR-0014 | Needs a rework count. |
| **L4 — day and shift boundaries are UTC.** `TenantConfig` has no timezone. A plant on IST has its 06:00 shift split across two UTC days. | ADR-0014 | Needs a tenant timezone and shift calendar. |
| **`MachineResponse` exposes no `site`.** After ADR-0011 made a machine `(tenant, site, name)`, the REST roster cannot tell a customer's two plants apart. Isolation is unaffected. | Found during the restore drill | Add `site` to the response model. |
| **No WebSocket connection-rate limit.** Unauthenticated sockets are now refused, so the free-resource case is closed; a valid token holder can still open many connections. | ADR-0016 | Revisit with the load results. |
| **CSV machine import matches on name within a tenant at the empty site.** | Phase notes | Import path only. |
| **No warning when a work order's part has no BOM.** | Phase notes | Silent no-op on backflush. |

### Accepted, with reasons

- **Revocation on the live feed takes effect at the next reconnect**, not within
  30 s. Re-verifying on every heartbeat costs a query per connection per 30 s
  forever. Recorded in ADR-0016 as the thing to revisit if a customer requires
  faster revocation.
- **The actor is re-verified at the approval boundary, not on every request.** A
  `SELECT` in `get_current_user` would cost all 282 routes to defend an action
  that happens a handful of times a day.
- **Bulk `UPDATE`/`DELETE` are not scoped by the ORM hook** (ADR-0002 scopes
  SELECTs). The control is `test_bulk_write_scoping.py`, which statically
  requires every bulk write to carry an explicit tenant filter or be listed with
  a reason.

## 10. Scores

Each is a judgement, so each carries its reason.

| Score | Value | Why not higher | Why not lower |
|---|---|---|---|
| **Production readiness** | **8 / 10** | RPO is 24 h; `/analytics` degrades at 1000 machines. | Migrations reverse, restore is drilled and timed, structured logs, rate limits, security headers, backups automated and verified. |
| **Enterprise readiness** | **7 / 10** | No SSO/SAML, no per-tenant audit export, no SOC2 artefacts, UTC-only shift calendars. | Tenant isolation proved on 11 surfaces with colliding identifiers; RBAC; append-only audit trail that can no longer contradict the deed; revocation works. |
| **Commercial readiness** | **7 / 10** | The four OEE limits are real and a serious buyer will ask about L1 and L4; no published SLA. | The numbers on screen are now defensible and consistent everywhere, coverage is stated rather than hidden, and the money story has a per-tenant rate rather than an invented one. |
| **Confidence in this assessment** | **8 / 10** | Load numbers come from one machine with a Python driver, not a distributed rig; the 1000-machine case is synthetic. | Everything asserted was measured on PostgreSQL 18.3; 84 mutations; 37 adversarial attempts; two harness bugs were caught *because* the numbers looked too uniform to be real. |

## 11. The verdict

> **WOULD YOU APPROVE AMP TODAY FOR THREE INDEPENDENT MANUFACTURING CUSTOMERS?**
>
> ## YES, WITH CONDITIONS

The conditions, in order:

1. **Shorten the backup interval before the third customer signs.** A 24-hour
   RPO is the largest single exposure in this document, and it is a schedule
   change rather than an engineering project.
2. **Tell customers what OEE does and does not measure.** L1–L4 are properties
   of the schema. A customer who discovers L1 themselves will stop trusting
   every number on the screen; a customer who was told will not.
3. **Watch `/analytics` as machine counts grow.** Fine for three plants of
   ordinary size; the first thing that will hurt at scale.
4. **Set `SECRET_KEY` on Railway** (outstanding from an earlier phase) and run
   the `#245` backfill.

What makes this a YES rather than a NO: every defect that made *multi-tenancy
itself* unsafe is closed, and each closure is verified on the real database, by
tests that fail when the guard is removed, against three customers who share
every identifier. The remaining risks are ones a customer can be told about
truthfully, which is not true of a system where one tenant's telemetry reaches
another.

What keeps it from being an unconditional YES: a 24-hour RPO is a real amount of
a real factory's data, and four documented OEE limitations are not the same as
four fixed ones.
