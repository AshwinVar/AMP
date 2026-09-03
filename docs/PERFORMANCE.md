# Performance

## Status: one measurement now exists (2026-09-01); the rest is still unmeasured

**The dashboard poll cycle has been measured** with `backend/dashboard_perf.py`,
which counts SQL statements per endpoint at 10 / 50 / 200 machines. It found a
real N+1 in `/machine-health` and the fix is measured below. Everything *else*
in this document is still as unmeasured as the heading used to say.

### `/machine-health` — measured, fixed, re-measured

`frontend/app/dashboard/page.tsx` polls the whole dashboard every 3 seconds
(46 requests per round, per open tab). `ai.twin.build_twins` issued three
queries **per machine** — recent downtime, open maintenance tasks, pending agent
actions.

| machines | queries before | queries after |
|---:|---:|---:|
| 10 | 37 | **10** |
| 50 | 157 | **10** |
| 200 | 607 | **10** |

At 200 machines that is 607 statements → 10, and ~520 ms → ~13 ms on this
laptop, repeating every three seconds. The count is now **flat**, which is the
durable property; the milliseconds are laptop-specific and are not a production
figure. `backend/test_machine_health_query_count.py` fails the build if
per-machine growth returns.

**Method and its limits:** route functions are called directly against a seeded
database and statements are counted via a SQLAlchemy `before_cursor_execute`
hook — the same technique `oem_perf.py` uses. It measures SQL shape, **not**
HTTP latency, serialisation, network or the browser. For those, `loadtest.py`
drives a real uvicorn, and it has still never been run.

---

## Everything below here: still nothing in this section has been measured

Every performance number AMP has ever quoted — "265ms to 13ms" on
`/analytics/executive-oee`, "714ms to 28ms" on the flow read-model, "376ms to
1.5ms" on the schedule chase list — came from an ad-hoc timing on one developer
machine, against an unrecorded dataset, on an unrecorded commit. None of it is
reproducible and none of it says anything about the system under concurrency.

The load harness in [`load/`](../load) exists to fix that. **The baseline tables
at the bottom of this document are deliberately empty.** They are not empty
because nobody got round to it; they are empty because inventing a plausible
number is worse than admitting there isn't one. Fill them in from a real run,
following the recording protocol, and this document starts being evidence.

- **How to run the scripts:** [`load/README.md`](../load/README.md)
- **What the numbers mean and what to do about them:** this file

---

## The load that actually exists

Nobody has measured it, but it is not hard to state precisely, because it is
entirely deterministic. From `frontend/app/dashboard/page.tsx`:

```
usePolling(fetchAll, 3000, Boolean(getToken()));
```

`fetchAll` issues **46 requests**: three awaited together (`/machines`,
`/downtime-logs`, `/shifts` — these gate first paint) and 43 more as one
`Promise.allSettled`. Every 3 seconds. For as long as the tab is open.

Two mitigations already landed and both matter to how you model this:

- `usePolling` **skips a tick whose predecessor has not finished**. Rounds cannot
  stack. Instead, when a round overruns 3s the effective refresh rate silently
  degrades to whatever the server can manage — and nothing on screen says so.
- Polling **stops while the tab is hidden**, and fires immediately on return.

On top of the 3s round, 34 components poll a read-model on a timer of their own
(`grep setInterval frontend/components`): 32 at 30s, the weekly report at 60s,
and the industrial-connectivity panel at 8s. Only the *mounted* ones poll — the
dashboard renders one view at a time and only the active Overview group's cards
mount — so the card contribution is a range, not a constant, and depends on
which screen the tab is parked on.

So, per open tab, in steady state:

| | Requests | Cadence | Per minute |
| --- | ---: | --- | ---: |
| `fetchAll` round | 46 | every 3s | **920** |
| Mounted snapshot cards | 0–34 | mostly every 30s | 0–~70 |

The round dominates, overwhelmingly, and it does not vary with the view. Three
tabs — a supervisor, an office desk and a wall display, which is a small
factory, not a large one — is roughly **3,000 requests per minute** against one
process, before anyone clicks anything.

---

## The architecture, and what saturates first

This is a chain, and it is worth knowing the order, because the *first* thing to
give way determines what the failure looks like.

**1. One uvicorn worker.**
`backend/Procfile`, `Dockerfile` and `railway.toml` all run
`uvicorn main:app` with no `--workers`. That is deliberate and documented in the
Dockerfile: the process owns in-memory state that does not survive being
duplicated — the 45-second simulation loop, the MQTT subscriber thread, the
rate-limit counters in `http_security.py`, the plan-gate licence cache. Adding a
worker today would double-tick the simulator and double-ingest MQTT. **Horizontal
scaling is not available as a quick fix**; that state has to move out of process
first.

**2. One event loop, and a background task that blocks it.**
`main.py::_simulation_loop` is `async def`, but everything inside it —
`SessionLocal()`, the `tick_*` calls, `db.commit()` — is synchronous SQLAlchemy
running on the event loop thread. Every 45 seconds the loop stalls for however
long that tick takes, and during the stall no request is accepted or dispatched.
Expect this as a **periodic latency spike affecting every in-flight request**,
not as a gentle slope. It is why `load/README.md` insists on runs of three
minutes or more: a 30-second run can miss it entirely.

**3. A 40-thread pool in front of a 15-connection pool.**
Of 273 route handlers, 269 are plain `def` rather than `async def`; the four
exceptions are the CSV import endpoints, which none of these scripts touch.
FastAPI dispatches a `def` handler to anyio's threadpool. Measured on the pinned
dependency set (anyio 4.13.0):

```
$ python -c "import anyio, anyio.to_thread; ..."
anyio default thread limiter total_tokens = 40
```

Underneath, `backend/database.py` takes SQLAlchemy's pool defaults:

```
$ python -c "from database import engine; ..."
QueuePool size= 5 overflow_max= 10 timeout= 30.0
```

**Forty handler threads contending for fifteen database connections.** That is
the pinch point. Past ~15 genuinely concurrent database-touching requests,
threads block on pool checkout rather than on CPU, so latency goes to a cliff
rather than degrading smoothly. If the pool stays exhausted for the full
`pool_timeout` of 30 seconds, SQLAlchemy raises and the request becomes a 500:

```
QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00
```

`pool_pre_ping=True` also adds a `SELECT 1` round trip on every checkout —
cheap, correct (it is what stops stale-connection 500s after a Railway restart),
but not free at this request rate.

**4. No cache anywhere.**
No Redis, no in-process response cache, no HTTP caching. `frontend/lib/api.ts`
actively defeats caching: every GET carries a `?t=<epoch ms>` cache-buster and
`cache: "no-store"`. Every one of those ~986 requests per tab per minute reaches
Python and touches Postgres. The load scripts reproduce the cache-buster
deliberately, so a run cannot accidentally benefit from a cache the real
dashboard defeats.

**5. Rate limiting is in-process and narrow.**
`http_security.py` throttles only `/login`, `/register`,
`/auth/change-password` and the AI routes. The dashboard round is entirely
unthrottled — there is nothing between an open tab and the worker.

### The arithmetic nobody has checked

A browser opens about **6 concurrent connections per origin**. So each open tab
contributes up to 6 in-flight requests, and:

| Open tabs | Peak concurrent requests | Against a pool of 15 |
| ---: | ---: | --- |
| 1 | 6 | fine |
| 2 | 12 | fine |
| 3 | 18 | **over** |
| 5 | 30 | well over, and past half the threadpool |
| 7 | 42 | past the threadpool too |

**This predicts that the connection pool is exceeded at three open dashboard
tabs.** That is arithmetic from the configured limits, not a measurement, and it
may well be wrong — real rounds are not perfectly overlapped, most requests are
short, and connections are returned as fast as they are taken. Which is exactly
the point: it is a falsifiable prediction, and `load/dashboard-poll.js` is how
you falsify it.

The derived per-request budget in `load/thresholds.js` comes from the same
arithmetic: 46 requests at 6-wide is 8 sequential waves, 3000ms / 8 = 375ms per
wave, minus headroom = **300ms p95 per request**. Not a taste-based SLO.

---

## The metric to read first

`dashboard_round_duration` — wall-clock time for one complete 46-request round.

Its p95 crossing **3000ms** is the moment the product quietly breaks. Not
"becomes slow": `usePolling` starts skipping ticks, the tab stops refreshing at
3 seconds, and an operator makes decisions on numbers that are older than they
believe. There is no spinner, no banner, no error. The dashboard looks exactly
as it does when it is healthy.

That is the failure this whole harness exists to detect, and it is invisible to
single-endpoint timing.

Secondary signals, in order of usefulness:

| Signal | Reading |
| --- | --- |
| `dashboard_round_overran` | Share of rounds over 3s. Non-zero means real tabs are already skipping ticks |
| `dashboard_blocking_duration` | The three awaited endpoints — this is first paint |
| `http_req_duration` `max` / p99 | Where the 45s simulation stall and pool-checkout queueing show up. They barely move the average |
| `dashboard_optional_non_2xx` | Degraded cards. The dashboard tolerates these, so they hide unless counted |
| `http_req_failed` | Anything here under read-only load is a bug or a saturated server |

---

## Recording protocol

A latency number is not evidence unless it carries the four things that
determine it. A row in the baseline tables below is only valid with all four:

1. **Commit** — `git rev-parse --short HEAD` on the running build.
2. **Target shape** — Docker Compose / PostgreSQL 16, or Railway, or bare
   uvicorn on SQLite. These are three different systems.
   Use the compose stack. Pool contention on SQLite tells you nothing about
   pool contention on Postgres.
3. **Dataset size** — read-model latency is a function of row counts and nothing
   else. Capture it:

   ```sql
   SELECT 'machines' t, count(*) FROM machines
   UNION ALL SELECT 'work_orders',           count(*) FROM work_orders
   UNION ALL SELECT 'downtime_logs',         count(*) FROM downtime_logs
   UNION ALL SELECT 'production_records',    count(*) FROM production_records
   UNION ALL SELECT 'production_plans',      count(*) FROM production_plans
   UNION ALL SELECT 'quality_inspections',   count(*) FROM quality_inspections
   UNION ALL SELECT 'inventory_items',       count(*) FROM inventory_items
   UNION ALL SELECT 'inventory_transactions',count(*) FROM inventory_transactions
   UNION ALL SELECT 'maintenance_tasks',     count(*) FROM maintenance_tasks
   UNION ALL SELECT 'customer_orders',       count(*) FROM customer_orders
   UNION ALL SELECT 'purchase_orders',       count(*) FROM purchase_orders
   UNION ALL SELECT 'escalations',           count(*) FROM escalations
   UNION ALL SELECT 'iot_telemetry',         count(*) FROM iot_telemetry
   UNION ALL SELECT 'audit_logs',            count(*) FROM audit_logs
   ORDER BY 1;
   ```

4. **The exact command**, environment variables included.

Export the run so it can be re-read later rather than retyped from a screenshot:

```
k6 run --summary-export=perf/<date>-dashboard-<vus>vu.json load/dashboard-poll.js
```

---

## Baseline tables

> **Every cell below reads UNMEASURED. That is accurate, not a placeholder
> oversight.** No load run has ever been performed against this codebase. Do not
> fill a cell from intuition, from an old commit message, or from a single-request
> `curl`. Only from a k6 run recorded per the protocol above.

### Run context

| Field | Value |
| --- | --- |
| Date | UNMEASURED |
| Commit | UNMEASURED |
| Target | UNMEASURED (`docker compose up`, PostgreSQL 16, expected) |
| Host | UNMEASURED (CPU cores / RAM — a single worker is sensitive to core speed, not core count) |
| Dataset | UNMEASURED (paste the row-count query output) |
| k6 version | UNMEASURED |

### 1. Dashboard poll — the profile that matters

`k6 run load/dashboard-poll.js`, `DURATION=3m` or longer.

| VUs (tabs) | Round p50 | Round p95 | Round p99 | Round max | Overran 3s | Blocking p95 | req p95 | Failed % | 500s |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| 2 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| 3 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| 5 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| 10 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| 20 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |

**The number this table exists to produce:** the largest VU count at which
`dashboard_round_overran` stays at 0. That is the supported number of
simultaneously open dashboard tabs per backend instance, and AMP currently
cannot state it.

Repeat the 3-VU row with `WITH_CARDS=1` — a real tab does both — and record it
separately:

| VUs, `WITH_CARDS=1` | Round p95 | Overran 3s | req p95 | Failed % |
| ---: | --- | --- | --- | --- |
| 3 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |

### 2. Per-endpoint, within the round

`PER_ENDPOINT=1 VUS=3 DURATION=3m k6 run load/dashboard-poll.js`.

Ordered as `fetchAll` issues them. The three blocking endpoints are marked —
their latency is first paint, the rest is card fill-in.

| # | Endpoint | p95 | max | Notes |
| ---: | --- | --- | --- | --- |
| 1 | `/machines` *(blocking)* | UNMEASURED | UNMEASURED | |
| 2 | `/downtime-logs` *(blocking)* | UNMEASURED | UNMEASURED | |
| 3 | `/shifts` *(blocking)* | UNMEASURED | UNMEASURED | |
| 4 | `/analytics/machine-timeline` | UNMEASURED | UNMEASURED | |
| 5 | `/analytics/machine-state-summary` | UNMEASURED | UNMEASURED | |
| 6 | `/work-orders` | UNMEASURED | UNMEASURED | |
| 7 | `/analytics/work-orders` | UNMEASURED | UNMEASURED | |
| 8 | `/analytics/predictive-maintenance` | UNMEASURED | UNMEASURED | |
| 9 | `/production-plans` | UNMEASURED | UNMEASURED | |
| 10 | `/analytics/production-plans` | UNMEASURED | UNMEASURED | |
| 11 | `/escalations` | UNMEASURED | UNMEASURED | |
| 12 | `/analytics/escalations` | UNMEASURED | UNMEASURED | |
| 13 | `/inventory/items` | UNMEASURED | UNMEASURED | |
| 14 | `/inventory/transactions` | UNMEASURED | UNMEASURED | |
| 15 | `/analytics/inventory` | UNMEASURED | UNMEASURED | |
| 16 | `/quality/inspections` | UNMEASURED | UNMEASURED | |
| 17 | `/analytics/quality` | UNMEASURED | UNMEASURED | |
| 18 | `/analytics/executive-oee` | UNMEASURED | UNMEASURED | The "265ms to 13ms" claim. Now measurable |
| 19 | `/factory-layout/nodes` | UNMEASURED | UNMEASURED | |
| 20 | `/analytics/factory-command-center` | UNMEASURED | UNMEASURED | |
| 21 | `/customer-orders` | UNMEASURED | UNMEASURED | |
| 22 | `/analytics/customer-orders` | UNMEASURED | UNMEASURED | |
| 23 | `/suppliers` | UNMEASURED | UNMEASURED | |
| 24 | `/purchase-orders` | UNMEASURED | UNMEASURED | |
| 25 | `/analytics/purchasing` | UNMEASURED | UNMEASURED | |
| 26 | `/documents` | UNMEASURED | UNMEASURED | |
| 27 | `/analytics/documents` | UNMEASURED | UNMEASURED | |
| 28 | `/maintenance/tasks` | UNMEASURED | UNMEASURED | |
| 29 | `/analytics/maintenance` | UNMEASURED | UNMEASURED | |
| 30 | `/production-schedules` | UNMEASURED | UNMEASURED | |
| 31 | `/analytics/production-schedules` | UNMEASURED | UNMEASURED | |
| 32 | `/iot/telemetry` | UNMEASURED | UNMEASURED | Grows fastest of any table |
| 33 | `/analytics/iot-command` | UNMEASURED | UNMEASURED | |
| 34 | `/ai/recommendations` | UNMEASURED | UNMEASURED | |
| 35 | `/analytics/ai-insights` | UNMEASURED | UNMEASURED | |
| 36 | `/saas/tenants` | UNMEASURED | UNMEASURED | Admin-only |
| 37 | `/analytics/saas` | UNMEASURED | UNMEASURED | |
| 38 | `/cost-records` | UNMEASURED | UNMEASURED | |
| 39 | `/analytics/costing` | UNMEASURED | UNMEASURED | |
| 40 | `/operator/executions` | UNMEASURED | UNMEASURED | |
| 41 | `/analytics/operator-terminal` | UNMEASURED | UNMEASURED | |
| 42 | `/audit-logs` | UNMEASURED | UNMEASURED | Append-only, unbounded without retention |
| 43 | `/notifications` | UNMEASURED | UNMEASURED | |
| 44 | `/reports` | UNMEASURED | UNMEASURED | |
| 45 | `/analytics/system-health` | UNMEASURED | UNMEASURED | |
| 46 | `/analytics/final-executive-summary` | UNMEASURED | UNMEASURED | |

### 3. Read-model ramp

`PER_ENDPOINT=1 k6 run load/read-models.js` at rising `VUS`.

| Endpoint | p95 @ 1 VU | p95 @ 5 | p95 @ 10 | p95 @ 20 | First VU count with a 500 |
| --- | --- | --- | --- | --- | --- |
| `/weekly-report` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/briefing` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/handover` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/copilot/digest` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/scorecard` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/mission-control/pulse` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/machine-health` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| `/twin-overlay` | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |

`SET=all` adds the 32 pillar read-models; record those in the same shape when
the composites are understood.

**The number this table exists to produce:** the concurrency at which the first
pool-checkout timeout appears, and which endpoint hits it first.

### 4. Smoke

| Field | Value |
| --- | --- |
| Endpoints walked | 99 |
| All 2xx | UNMEASURED |
| Slowest endpoint, 1 VU | UNMEASURED |
| p95, 1 VU, warm | UNMEASURED |

---

## When a budget is blown

In rough order of effort against effect. Deliberately ordered so the cheap
structural fixes come before the expensive infrastructural ones — the current
design leaves a lot on the table before anyone needs new hardware.

1. **Raise the connection pool.** `create_engine(..., pool_size=..., max_overflow=...)`
   in `backend/database.py`. One line. The current 5+10 is SQLAlchemy's default,
   chosen by nobody. Check what the Postgres instance actually permits first
   (`SHOW max_connections`) — a pool larger than the server allows converts a
   queue into a hard error.
2. **Stop the round being 46 requests.** The largest single win available. A
   composite `/dashboard-bootstrap` endpoint returning what `fetchAll` assembles
   would cut a tab's steady-state load by an order of magnitude, and the
   read-model pattern in ADR-0007 is already the right shape for it. This is a
   product change, not a tuning knob, which is why it is not first — but it is
   the one that matters most.
3. **Move the simulation loop off the event loop.** `asyncio.to_thread` around
   the tick body, or out of the web process entirely. Removes the periodic stall
   that hits every concurrent request.
4. **Add caching.** The read-models are pure projections over recent history and
   several are recomputed identically for every tab on every tick. Even a 2-3
   second in-process TTL cache on the composites would collapse most of the
   duplicate work, and 2-3 seconds is invisible against a 3s poll.
5. **Lengthen the poll interval.** Trivially available (`usePolling(fetchAll, N)`),
   and the honest question is whether a plant dashboard needs 3-second
   granularity for stock levels and compliance documents. It does not.
6. **Then, and only then, scale out.** Requires moving the simulation loop, the
   MQTT subscriber, the rate-limit counters and the plan-gate cache out of
   process first — see the comment block in the `Dockerfile`. Adding `--workers`
   before that does not scale AMP, it corrupts it.

---

## Verifying the paths the scripts use

`load/endpoints.js` is transcribed from source, so it can silently drift. Two
checks, both cheap, both worth running when a route or a dashboard card changes.

**Does `DASHBOARD_ROUND` still match `fetchAll`?** One command, no k6 and no
server:

```
$ node load/check-drift.mjs
load/endpoints.js matches fetchAll: 46 endpoints, same order.
```

It exits non-zero and prints the added/stale paths when the two disagree. A
mismatch means the harness has stopped measuring the real dashboard, which is
worse than having no harness, because the output still looks authoritative.

**Does every scripted path exist?** Dump the live FastAPI route table and diff
the catalogue against it:

```python
import inspect, main
from fastapi.routing import APIRoute
live = {r.path for r in main.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
```

All 99 paths referenced by `load/*.js` were verified present when this harness
landed. Re-run it after any route rename — `smoke.js` will also catch it, but
this catches it without needing a running instance.
