# AMP — Production Readiness, Final Audit

Campaign run against master `f909450`, ending at `578b48d`.
PostgreSQL 18.3 (local, disposable databases) + production Railway + CI.

---

> ## ⚠ SUPERSEDED — this document's verdict is no longer current
>
> This audit ended at **NO**, with seven "minimum blockers before a YES"
> (§ *Minimum blockers*, at the end). **All seven are now closed and verified.**
>
> | Blocker | Closed by | Record |
> |---|---|---|
> | 1 · MQTT tenant dimension | [#502](https://github.com/AshwinVar/AMP/pull/502) | ADR-0011 |
> | 5 · tenant-safe document numbering | [#503](https://github.com/AshwinVar/AMP/pull/503) | ADR-0012 |
> | 2 · per-tenant BOM | [#504](https://github.com/AshwinVar/AMP/pull/504) | ADR-0013 |
> | 3 · OEE correctness | [#505](https://github.com/AshwinVar/AMP/pull/505) | ADR-0014 |
> | 4 · server-side approval gate | [#506](https://github.com/AshwinVar/AMP/pull/506) | ADR-0015 |
> | 6 · authenticate `/ws/live` | [#507](https://github.com/AshwinVar/AMP/pull/507) | ADR-0016 |
> | 7 · load test + timed restore drill | this branch | see closure doc |
>
> **The current verdict is YES, WITH CONDITIONS** — see
> [`CUSTOMER-READINESS-CLOSURE.md`](CUSTOMER-READINESS-CLOSURE.md) for the
> evidence, the measured load and recovery numbers, the remaining risks and the
> conditions.
>
> Everything below is kept unedited. It is the record of what was true before
> the fixes, and the reason each fix exists — an audit rewritten to match its
> outcome stops being evidence.

---

## 1. Executive verdict

*(Superseded — see the banner above. Kept as written on the day.)*

**NO — not yet approved for three independent manufacturing customers.**

The blocker is not code quality. Test coverage, CI discipline and the guard
programme are genuinely strong, and two of the three campaign-critical defects
found here were fixed and merged during the audit. The blocker is that **three
of the remaining P1s are the specific failures a multi-customer deployment
produces and a single-customer deployment never does** — and AMP has only ever
run one real customer.

This audit set out to disprove readiness and succeeded twice at P0. That is the
finding: the previous audits were green because they never ran the multi-tenant
case against a real database.

**Full detail: [40 confirmed findings](#2-all-issues-discovered), of which 9 are P1.**

---

## 2. All issues discovered

49 raw findings from six specialist auditors; **9 refuted** on adversarial
re-check; **40 confirmed**. Severity mix: **9 P1, 15 P2, 11 P3, 5 P4**.

Every finding below was required to cite `file:line`, name a reachable
entrypoint, and be marked `tested` only if a repro was executed. Most were.

### Fixed and merged during this campaign

| # | Severity | Issue | PR |
|---|---|---|---|
| 1 | **P0** | A `%` in `DATABASE_URL` crashed the release command *after* `create_all` but *before* the stamp — leaving a schema with no `alembic_version`, un-migratable forever, while the app still booted | [#499](https://github.com/AshwinVar/AMP/pull/499) |
| 2 | **P0** | X-Tenant preview gated on *workspace*, never *role* — any founder-workspace Operator/Supervisor read **and wrote** every tenant's factory | [#500](https://github.com/AshwinVar/AMP/pull/500) |
| 3 | **P1** | GMATS `?tenant=` override + `_guard_record` returning early for all DEFAULT logins → by-id routes had *no* tenant boundary | [#500](https://github.com/AshwinVar/AMP/pull/500) |

### Remaining P1 — these are the blockers

| Issue | Why it blocks multi-customer |
|---|---|
| **MQTT ingest runs with no tenant bound** (`mqtt_service.py:62-64,147-149`) | Resolves machines by name across *all* tenants (`Machine.name` has no unique constraint) and files telemetry under the `DEFAULT` column default. Measured: a TENANT_B machine's production/downtime/event rows all landed as `DEFAULT`; DEFAULT's pooled OEE read 79% off another customer's output while the owner saw **zero** of its own telemetry. Two customers publishing `CNC-01` corrupt each other. |
| **BOM is a hardcoded 6-entry Python dict** | A new customer's work-order completion consumes *someone else's* bill of materials. Onboarding cannot proceed without a code change. |
| **`/analytics/executive-oee` fabricates A/P/Q/OEE** for machines with zero production data | A plant manager sees a believable OEE for a machine that has produced nothing. |
| **Plant OEE computed over ALL TIME** on `/analytics/summary`, `/management`, `/executive-oee` | Last year's scrap permanently drags today's number; the figure never recovers and never reflects the current shift. |
| **Enterprise-inventory document numbers collide** — generated from a tenant-scoped `count()` into a globally-unique column | Second customer's GRN/issue-slip creation fails or collides. |
| **Operator bypasses the ADR-0005 approval gate** by PATCHing the proposed item directly | An agent recommendation can be enacted without approval — the oversight loop is advisory, not enforced. |

### Confirmed P2/P3 highlights

- Two overlapping "mark Completed" PATCHes **consume the BOM twice**
- `approve_cycle_count` applies a stale variance as a delta → can store **negative stock**
- A GRN can accept **more than was received** (no check against `received_qty`)
- Voiding a GMATS tax invoice makes the next invoice **reuse a live number**
- `/ai/ask` and `/ai/report` — 60s blocking call in a sync handler → **stalls the API for every tenant**
- Prompt injection: machine names, downtime reasons, item names reach the LLM **unescaped**
- `/ws/live` accepts **unlimited unauthenticated** WebSocket connections
- Login/blocked-login audit rows written with **NULL tenant** → invisible to the tenant they concern
- `apply_plan_tier` **fails open to the enterprise licence** on an unrecognised plan name
- Currency is a hardcoded GBP constant on both stacks
- Every OEE/quality/maintenance target is a module constant, not per-tenant configuration

---

## 3–4. PRs created and merged

| PR | Title | State |
|---|---|---|
| [#499](https://github.com/AshwinVar/AMP/pull/499) | `%` in DATABASE_URL killed the release command | **merged** |
| [#500](https://github.com/AshwinVar/AMP/pull/500) | Any founder-workspace login could read and write every tenant | **merged** |

Both shipped with failing-test-first, mutation verification, and full gates.

---

## 5. Security results

| Check | Result |
|---|---|
| Fail-closed JWT secret | **PASS** — app refuses to boot in production without `SECRET_KEY`; production booting *is* the proof |
| Security headers | **PASS** — CSP, HSTS, nosniff, X-Frame-Options DENY, Referrer-Policy, COOP, Permissions-Policy all live |
| Request correlation | **PASS** — `x-request-id` echoed |
| `/system-health` gating | **PASS** — 401 unauthenticated, no body leak |
| **X-Tenant role gate** | **WAS BROKEN → FIXED** (#500) |
| **GMATS tenant override** | **WAS BROKEN → FIXED** (#500) |
| WebSocket auth | **FAIL** — `/ws/live` accepts unlimited unauthenticated connections |
| Prompt injection | **FAIL** — user-controlled strings reach prompts unescaped |

Not tested: password brute-force and rate-limit behaviour against production
(deliberately — exercising a lockout on a live deployment is destructive).

---

## 6. Tenant-isolation results

Run against **real PostgreSQL 18.3**, three tenants (`FACTORY_A/B/C`) with
distinct data — `backend/audit_isolation.py`:

```
1. tenant-bound reads      A saw 3/3, B saw 5/5, C saw 2/2, foreign rows: none
2. cross-tenant fetch-by-id                              None (correct)
3. every scoped model                     cross-tenant rows visible: 0
4. NULL tenant_code row                        visible to: nobody (fail-closed)
```

**Architecture:** 45 of 48 models carry `tenant_code`; 35 are auto-scoped by the
ADR-0002 hook, and the other 10 are enumerated in `test_unscoped_model_reads.py`
with a written reason plus a call-site check. That guard is real.

**Bulk writes bypass the hook by design** (it rewrites SELECTs only). The control
is `test_bulk_write_scoping.py` — which proved itself by **rejecting this audit's
own harness**, the only unguarded bulk writes in the backend.

**But:** MQTT ingest sits outside all of this (P1 above). Isolation holds for the
HTTP surface and fails for the ingest surface.

---

## 7. Database results

- **Alembic from zero on real PostgreSQL: PASS** — 49 tables, stamped `0001_baseline` (only after #499; it crashed before)
- **Existing-schema adoption: PASS** — stamped, not rebuilt
- Schema: **42 FKs, 21 unique constraints, 170 indexes**
- 13 tables have a **nullable** `tenant_code` — safe today because `NULL == 'X'` is NULL, so those rows are visible to nobody, verified empirically
- **PostgreSQL enforces FKs that SQLite does not** — `production_records.machine_id` has no cascade; deleting a machine with history raises. Any delete path needs checking against Postgres, not SQLite
- **Investigated and refuted:** `func.avg()` returns `Decimal` on PostgreSQL and `float` on SQLite. All four call sites feeding `pooled_oee_from_sums` cast with `int(...)` and sum rather than average. Not a defect

---

## 8. Manufacturing-correctness results

**This is where AMP is weakest, and it is the category that matters most.**

Confirmed: OEE can be **believable but wrong** in four distinct ways —
fabricated A/P/Q for machines with no production; all-time rather than windowed
plant OEE; an unweighted **mean of ratios** on Machine Health (contradicting the
pooled figure everywhere else); and a fabricated `avg_oee` in the no-production
fallback.

The pooling standardisation (`analytics_engine.pooled_oee`) is sound and is used
correctly in most places. The failures are at the edges — empty data and time
windows — which is exactly where a new customer starts.

---

## 9. AI-agent safety results

Loop is implemented (observe→recommend→approve→execute→audit) but **not
enforced**: an Operator can PATCH the proposed item directly and skip approval.
Plus unescaped prompt injection, a 60s blocking LLM call that stalls the whole
API, no dedup of stale/rejected recommendations, and second-resolution agent
identifiers that collide.

No evidence found of an agent **expanding its own permissions** — that specific
guarantee holds.

---

## 10. MQTT / industrial results

- Resilience suites pass (reconnect, malformed payloads, redelivery, broadcast failure)
- **No tenant dimension** — P1 above
- **PLC connectivity is entirely simulated**: every industrial signal is `random.randint`. There is no real OPC-UA/Modbus driver; `SimulatorAdapter` is the only `ProtocolAdapter` subclass

### Phase 7 — the Offline decision

**Recommendation: `Offline` needs its own state; it must not stay neutral, and
must not share Breakdown's red.**

Reasoning from manufacturing semantics:
- Neutral grey currently means *"I don't recognise this status"*. `Offline` **is**
  recognised — it's in `VALID_MACHINE_STATUSES` and both `briefing.py` and
  `assistant.py` classify it as hard-down. Rendering a known state as unknown is
  the one thing grey must not do.
- Sharing Breakdown's red is also wrong: red must mean *"something failed, send
  someone"*. Offline is typically **not scheduled / powered down** — no call-out.
  Diluting red degrades the signal operators scan for.

**Not implemented.** It changes what four screens tell an operator about plant
state, and the right hue is a product decision. The code is now one line in one
function (`statusHue`, #498) and every screen follows.

---

## 11. Frontend / E2E results

- 245 unit tests, 92.66% branch coverage of `lib/`
- 22/22 Playwright specs (auth, dashboard, Mission Control, Machine Cockpit, role-gating, accessibility) green in CI and locally
- **NOT DONE:** the full journey across desktop/tablet/mobile viewports with huge-data, network-delay and expired-auth states. Reported as not done rather than claimed

---

## 12. Performance baselines

**Measured** on real PostgreSQL 18.3 — median of 5, warm, single-threaded,
**no HTTP, no concurrency** (`backend/audit_perf.py`):

| probe | 10 machines | 50 | 250 |
|---|---|---|---|
| machine list | 1.4ms | 1.4ms | 2.4ms |
| production scan (hydrate) | 3.2ms | 6.3ms | **86.6ms** |
| pooled OEE (hydrate + pool) | 2.7ms | 9.8ms | **100.3ms** |
| pooled OEE (SQL aggregate) | 2.2ms | 2.1ms | **3.4ms** |

**First bottleneck: any read model that hydrates `production_records` instead of
aggregating in SQL** — 30× slower at 250 machines and still climbing, while the
aggregate path is flat. Measured evidence that #405/#411/#419 was the right
programme, and that any remaining hydrating read model is on borrowed time.

**NOT DONE:** the 1000-machine STRESS tier, API p50/p95/p99, requests/sec,
concurrent users, WebSocket connection ceiling, MQTT throughput, memory/CPU.
k6 is not installed and Docker is unavailable in this environment.
`docs/PERFORMANCE.md`'s baseline tables therefore **stay UNMEASURED** — filling
them from the numbers above would misrepresent a database probe as an HTTP load
test.

---

## 13. Backup / restore results

- `pg_dump` → artifact → **restore into a throwaway database**: passing on a
  schedule, **5 consecutive days** verified in GitHub Actions
- Restore drill is part of the workflow, not a manual step
- **NOT DONE:** the full timed drill (production-style DB → restore → migrate →
  boot → login → verify critical data) with a measured wall clock

### RPO / RTO

**Cannot be stated from measurement.** RPO is bounded by the backup schedule
(daily → **up to 24h of data loss**), which is a design fact, not a measurement.
RTO was **not measured** — the timed restore-to-serving drill was not run.
Publishing an RTO without running it would be exactly the fabrication this
campaign forbids.

---

## 14. Customer-onboarding results

**15 findings. This is the commercial blocker set.**

Requires a **developer / code change** today:
- Bill of Materials (hardcoded 6-entry dict) — **P1**
- MQTT tenant routing — **P1**
- Currency (hardcoded GBP, both stacks)
- Every OEE/quality/maintenance/supplier target (module constants)
- Enterprise inventory module gated on the literal string `'GMATS'`
- AI Copilot branches on the literal string `'GMATS'`
- `modules.json` is a code artifact — pack composition needs a redeploy
- Preventive maintenance has no recurrence (a PM programme cannot be expressed)
- A customer's branding and demo login are hardcoded in shared startup code

Works at runtime: tenant creation, users, roles, machines, inventory items,
shifts, plan/module entitlement, unit-value rate, partial branding.

Also: **every operational business key is globally unique across tenants** —
customer #2 cannot use their own work-order numbering scheme if it collides
with customer #1's.

---

## 15–17. Limitations, deferred risks, technical debt

**Limitations of this audit** (stated so they are not mistaken for clean results):
- No k6, no Docker → no HTTP load test, no 1000-machine tier, no timed DR drill
- No multi-viewport browser journey
- Failure injection covered only by existing resilience suites, not newly exercised
- Production observations limited to unauthenticated endpoints (no credentials used)

**Deferred risks:** 205 stale remote branches; eslint baseline frozen at 134
(118 errors, dominated by 68 `set-state-in-effect` and 39 `no-explicit-any`);
frontend components at 4.5% Vitest coverage (covered by e2e instead).

---

## 18–20. Test counts and coverage

| | Count | Coverage |
|---|---|---|
| Backend | 1086 tests / 171 suites | 81.19% branch (floor 78) |
| Frontend unit | 245 tests / 21 files | 92.66% branch of `lib/` (floor 89) |
| E2E | 22 Playwright specs | — |
| CI jobs | backend, coverage, frontend, e2e | all green on master |

---

## 21–24. Scores

| Dimension | Score | Basis |
|---|---|---|
| **Production readiness** | **6.5 / 10** | Infrastructure, observability, backups and CI are strong. Two P0s were live until today. |
| **Enterprise readiness** | **5 / 10** | Tenant isolation holds on HTTP and fails on ingest. Approval gate bypassable. |
| **Commercial readiness** | **3.5 / 10** | Onboarding a second manufacturer requires editing source in at least 9 places. |
| **Confidence in this assessment** | **7 / 10** | High for what was executed against real PostgreSQL; reduced because load, DR timing and the browser journey were not run. |

---

## Would I personally approve AMP for onboarding three independent manufacturing customers today?

# NO

Not because the engineering is weak — it isn't. Because **three of the confirmed
P1s are precisely the failures that only appear once there is more than one
customer**, and AMP has never run more than one.

The decisive one: **MQTT ingest has no tenant dimension.** Two customers each
running a machine called `CNC-01` will silently write into each other's
production history — measured, not theorised. Both plants would then make
scheduling and maintenance decisions from numbers that belong to someone else.
That is the worst outcome this system can produce, and it is reachable on day one
of customer #2.

Second: **the BOM is a hardcoded Python dict.** Customer #2's work-order
completion consumes customer #1's materials. Onboarding is not possible without
a code change, which also means it is not repeatable.

Third: **OEE can be believably wrong** — fabricated for machines with no
production, and computed over all time so it never reflects the current shift.
A manufacturing platform that reports a confident wrong OEE is worse than one
that reports nothing.

### Minimum blockers before a YES

1. **Give MQTT ingest a tenant dimension** — topic-per-tenant or a credential-to-tenant map; make `Machine.name` unique *per tenant*; stamp child rows from the machine's tenant, never a column default.
2. **Make the BOM per-tenant data**, not a module constant.
3. **Fix the two OEE correctness bugs** — no fabricated factors for zero-production machines; window the plant OEE.
4. **Enforce the ADR-0005 approval gate** server-side so a direct PATCH cannot skip it.
5. **Make document numbering tenant-safe** (unique per tenant, or a per-tenant sequence).
6. **Authenticate `/ws/live`** before accepting the connection.
7. **Run the two tests this environment could not**: a real k6 load test against a disposable PostgreSQL, and one timed end-to-end restore drill to establish an actual RTO.

Items 1–3 are the ones I would not ship without. With those six code fixes and
item 7's evidence, I would expect to reach **YES, WITH CONDITIONS** — the
conditions being per-tenant configuration of currency and targets, which are
commercial friction rather than safety risks.

**Green tests did not make AMP production-ready, and this campaign is the
demonstration: 1086 passing tests coexisted with two P0s, both found only by
running the multi-tenant case against a real database.**

---

## Closure

All seven blockers above were closed in PRs #502-#507 and this branch, and
re-verified on PostgreSQL 18.3 against three customers sharing every identifier
(CNC-01, LINE-01, WO-001, INV-001, PO-001, FG-001).

The sentence above still holds, and the closure campaign proved it a second
time: 1115 passing tests coexisted with an approval gate that a deleted user
could walk through, and a live telemetry feed that authenticated nothing at all.
Neither was found by a test. Both were found by attacking the running system.

Current verdict, evidence and remaining risks:
[`CUSTOMER-READINESS-CLOSURE.md`](CUSTOMER-READINESS-CLOSURE.md).
