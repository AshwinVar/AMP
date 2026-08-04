# load/ — k6 load scripts

AMP had no load tests at all until this directory existed. Every performance
claim in the history — "265ms to 13ms", "714ms to 28ms" — was a one-off timing
that nobody can reproduce, and the load that actually exists in production (an
open dashboard tab issuing 46 requests every three seconds) had never been
measured once.

These scripts make both reproducible. For what the numbers *mean* — what
saturates first on this architecture and why — read **[docs/PERFORMANCE.md](../docs/PERFORMANCE.md)**.
This file is the operating manual.

---

## Everything defaults to localhost, on purpose

`BASE_URL` defaults to `http://127.0.0.1:8000`, the same origin
`frontend/lib/api.ts` defaults to. Pointing these scripts at a shared instance
is not a benchmark — AMP runs a **single uvicorn worker** with no cache in
front of it, so a dashboard-poll run against a live tenant is an outage on
somebody's plant floor while a shift is running.

`config.js` enforces that rather than merely asking for it:

```
$ BASE_URL=https://amp.example.com k6 run load/smoke.js
ABORT: Refusing to load test https://amp.example.com: it is not localhost.
```

If you genuinely mean it, say so out loud:

```
BASE_URL=https://staging.example.com ALLOW_REMOTE=1 LOAD_USER=... LOAD_PASS=... k6 run load/smoke.js
```

The guard resolves the host properly, so `https://user:pass@evil.com` and
`http://127.0.0.1.evil.com` are both correctly treated as remote.

---

## Install k6

k6 is a single static binary — no Python environment, no dependency tree, and
one line in CI. That is why it was chosen over Locust.

| Platform | Command |
| --- | --- |
| Windows | `winget install k6.k6` (or `choco install k6`) |
| macOS | `brew install k6` |
| Linux | `sudo apt install k6` after adding the Grafana repo, or download the tarball |
| Anywhere | `docker run --rm -i grafana/k6 run - <load/smoke.js` |

Requires k6 v0.50 or newer (`exec.test.abort()`, `ramping-vus`). Check with
`k6 version`.

---

## Bring up a target

Run against the **Docker Compose stack**, not a bare `uvicorn` on SQLite. The
compose stack is PostgreSQL 16, the same major version Railway provisions, and
connection-pool contention — the thing most likely to be the bottleneck — does
not behave remotely like this on SQLite. A SQLite number is not a wrong number,
it is a number about a different system.

```
docker compose up --build          # http://localhost:8000/health
```

You also need an account on the target. There are no credentials in this
repository, not even for local runs, so `LOAD_USER` and `LOAD_PASS` are
mandatory and the scripts abort without them.

Use an **Admin** on a fully licensed tenant. A non-Admin gets 403 on the SaaS
registry endpoints, and a tenant whose licence omits a module pack gets 403 from
`backend/plan_gate.py` — in both cases `smoke.js` will fail and tell you which
it was.

---

## The three scripts

### `smoke.js` — does everything answer?

One VU, one iteration, every endpoint the other scripts touch, asserted 2xx.
Run this first and after every route change. It is the only one of the three
that belongs in CI.

```
LOAD_USER=admin LOAD_PASS=... k6 run load/smoke.js
```

It walks 99 unique paths and prints a per-group tally. A failure names the path
and explains the status in terms of this codebase (plan gate, role gate,
throttle) rather than leaving you to guess.

### `dashboard-poll.js` — the load that actually exists

The one that matters. Each VU is **one open dashboard tab**: the real fetchAll
set from `frontend/app/dashboard/page.tsx`, all 46 endpoints, every 3 seconds,
six connections at a time to mirror a browser's per-origin cap.

```
LOAD_USER=admin LOAD_PASS=... VUS=3 DURATION=3m k6 run load/dashboard-poll.js
```

| Env | Default | Meaning |
| --- | --- | --- |
| `VUS` | `3` | Concurrent open tabs |
| `DURATION` | `1m` | Hold time. Use **3m or more** — see below |
| `WITH_CARDS` | off | Also layer the 30s snapshot-card read-model polls, as a real tab does |
| `PER_ENDPOINT` | off | Print a p95 row per endpoint (46 extra rows) |

Run for at least three minutes. The backend's simulation loop
(`main.py::_simulation_loop`) ticks every 45 seconds and does synchronous
database work on the event loop, so a 30-second run can miss it entirely and a
one-minute run samples it once. Latency spikes that only appear at 45-second
intervals are exactly what a short run hides.

**Read `dashboard_round_duration` first.** It is the wall-clock time for one
complete 46-request round. Once its p95 crosses 3000ms the product has quietly
broken: `usePolling` starts skipping ticks, the tab stops refreshing at 3s, and
the operator reads stale numbers with nothing on screen saying they are stale.
Per-request p95 is a diagnostic; the round is the user-visible truth.

### `read-models.js` — where does it break first?

A ramp against the composite read-models (`/weekly-report`, `/briefing`,
`/handover`, `/copilot/digest`, ...) — the only endpoints that fan out across
several pillars to build one response.

```
LOAD_USER=admin LOAD_PASS=... VUS=15 DURATION=2m k6 run load/read-models.js
```

| Env | Default | Meaning |
| --- | --- | --- |
| `VUS` | `10` | Peak concurrency |
| `RAMP` | `30s` | Ramp up and ramp down duration |
| `DURATION` | `1m` | Hold time at peak |
| `SET` | `composite` | `all` adds the 32 pillar read-models |
| `THINK` | `1` | Seconds between sweeps. `0` for pure saturation |
| `PER_ENDPOINT` | off | Print a p95 row per endpoint |

This is a **saturation probe, not a fidelity model** — the real cards poll these
every 30 seconds, not back to back. Read its output as "which endpoint falls
over first, and roughly at what concurrency", never as "what users experience".
Ramp in rather than starting flat so a cliff shows up as a point on the ramp.

---

## Reading k6 output

```
     http_req_duration..............: avg=41.2ms min=3.1ms med=28ms max=1.9s p(90)=88ms p(95)=140ms
     ✓ http_req_failed...............: 0.00%  ✓ 0  ✗ 6210
     ✗ dashboard_round_duration......: avg=2.1s   ...  p(95)=3.4s
```

* **p95, not avg.** The average is dominated by the cheap list endpoints and
  will look healthy while a composite read-model is timing out.
* **`max` matters here more than usual.** Two things on this architecture cause
  rare, large spikes rather than gentle degradation: the 45-second simulation
  tick blocking the event loop, and connection-pool checkout queueing. Both show
  up in `max` and p99 long before they move the average.
* **`http_req_failed`** is the error rate. A `✗` on it means the error budget
  was blown; a `✓` with a non-zero count means requests failed but stayed inside
  budget.
* **A `✗` on any line fails the run and k6 exits non-zero.** That is what makes
  these usable as a gate.
* **`iteration_duration` is not the round.** For `dashboard-poll.js` it includes
  the sleep that pads the round out to 3 seconds. `dashboard_round_duration` is
  the metric without the padding.
* **Per-endpoint rows only appear with `PER_ENDPOINT=1`.** k6 aggregates
  `http_req_duration` across the whole run; the only supported way to get a
  per-endpoint breakdown printed is to declare a threshold on the tagged
  sub-metric, which is what that flag does.

Machine-readable output, for putting a run in the baseline table:

```
k6 run --summary-export=run.json load/dashboard-poll.js      # end-of-test summary
k6 run --out json=raw.json load/dashboard-poll.js            # every sample
```

---

## Custom metrics these scripts add

| Metric | Script | What it means |
| --- | --- | --- |
| `dashboard_round_duration` | dashboard-poll | Wall clock for one 46-request round. **The headline.** |
| `dashboard_round_overran` | dashboard-poll | Share of rounds that took longer than the 3s interval, i.e. rounds where a real tab would have skipped a tick |
| `dashboard_blocking_duration` | dashboard-poll | Time for the three endpoints the dashboard awaits before rendering anything — first paint |
| `dashboard_optional_non_2xx` | dashboard-poll | Share of the 43 tolerated endpoints that did not return 2xx. Degraded cards, not a broken page — counted so it cannot hide |
| `dashboard_round_requests` | dashboard-poll | Requests issued, as a running total |
| `read_model_sweep_duration` | read-models | Wall clock for one VU's pass over the composite set |

---

## Why one login for the whole test

`backend/http_security.py` throttles `/login` to `RATE_LIMIT_LOGIN` (default 10)
requests per 60 seconds **per client key**, and the client key is the left-most
`x-forwarded-for` hop or the socket peer — every k6 VU on one machine lands in
the same bucket. A script that logged in per VU would start returning 429 at the
eleventh VU and the run would silently be measuring the rate limiter instead of
the application.

So `config.js::login()` runs once in `setup()` and hands the same bearer token to
every VU. That is also what the real world looks like: N tabs open on one
operator's account.

If you see `ABORT: /login returned 429`, the throttle is still holding from an
earlier run. Wait a minute, or raise `RATE_LIMIT_LOGIN` on the target.

---

## Files

| File | Role |
| --- | --- |
| `config.js` | Target, guard, login, request shaping. Everything that must be identical across runs |
| `endpoints.js` | The endpoint catalogues, transcribed from `fetchAll` and the read-model router |
| `thresholds.js` | Latency and error budgets, and the per-endpoint threshold trick |
| `smoke.js` | 1 VU correctness walk |
| `dashboard-poll.js` | N open tabs at the real 3s cadence |
| `read-models.js` | Ramp against the composites |
| `check-drift.mjs` | Plain-Node guard that `endpoints.js` still matches `fetchAll` |
| `package.json` | Scopes this directory as ESM for Node. Not a package — nothing to install |

`endpoints.js` is transcribed from source, so it can drift. `DASHBOARD_ROUND`
is an exact, order-preserving copy of the `fetchAll` list in
`frontend/app/dashboard/page.tsx`; if someone adds a card and does not update
it, this harness quietly stops measuring the real thing while still printing
confident-looking numbers, which is worse than having no harness at all.

That is what `check-drift.mjs` is for. No k6, no server, no dependencies:

```
$ node load/check-drift.mjs
load/endpoints.js matches fetchAll: 46 endpoints, same order.
```

It exits non-zero with a diff when the two disagree, so it is cheap enough to be
a required CI check rather than something someone has to remember.

---

## Environment variables, all scripts

| Env | Default | Meaning |
| --- | --- | --- |
| `BASE_URL` | `http://127.0.0.1:8000` | Target origin. Non-local requires `ALLOW_REMOTE=1` |
| `ALLOW_REMOTE` | unset | `1` to permit a non-localhost target |
| `LOAD_USER` | — | **Required.** Username to authenticate as |
| `LOAD_PASS` | — | **Required.** Password |
| `TENANT` | unset | Sends `X-Tenant`, the founder tenant-preview header. Changes whose data volume you are measuring |
