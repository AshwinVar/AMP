# AMP — monitoring

Two endpoints, two jobs.

| Endpoint | Who calls it | Contract |
|---|---|---|
| `GET /health` | uptime monitors, the Railway deploy probe | Public. **200** when the database answers, **503** when it doesn't. Body: `status`, `database`, `time`, `version` (short git sha). |
| `GET /system-health` | you, when something is wrong | Platform owner only (Admin in the `DEFAULT` workspace). **Always 200**, even when the verdict is `critical` — read the `status` field, not the HTTP code. |

`/system-health` is a read-model (ADR-0007): a pure projection recomputed on every
call over what the process and the database already know. It stores nothing, runs
no background job, and creates no tables — so there is nothing to keep in sync and
nothing to go stale. Source: `backend/monitoring.py`.

**Point your uptime monitor at `/health`, never at `/system-health`.** If the
diagnostic endpoint signalled through its status code, you would get paged for
table growth at 2am. `/health` is the liveness contract; `/system-health` is the
thing you open once it pages you.

---

## What is NOT monitored

Read this first, so you don't plan against capability that isn't here.

- **No metrics backend, no time series.** Nothing stores a history. Every number
  is *now*. You cannot ask "what was the pool doing an hour ago" — nobody wrote
  it down. Growth trends have to be eyeballed by reading the endpoint on
  different days, or by shipping the numbers somewhere that does keep history.
- **No alerting.** Nothing pushes. `/system-health` tells you what is wrong when
  you ask it; the only thing that will wake you up is the external uptime monitor
  watching `/health` (§ *Uptime monitoring* below) and Sentry emailing you about
  an unhandled exception.
- **No queue.** There is no message queue in this platform, so there is no queue
  depth, no consumer lag and no dead-letter count. If a doc or a dashboard ever
  shows you one, it is lying.
- **No per-request latency, no error rate, no throughput.** Those need either a
  metrics backend or log aggregation. Structured request logging is the seam this
  would hang off (`backend/logging_config.py`), not this module.
- **No backup freshness.** See § *Backups* — this is a deliberate hole, not an
  oversight.
- **No MQTT message rate.** See § *MQTT*.
- **Numbers are per-process.** The WebSocket count and the MQTT listener probe
  describe *this* web process. The deployment runs a single uvicorn worker today
  (`Procfile`, `railway.toml`), so today that is the whole fleet. The moment
  `--workers` is added, both become a fraction and you will need to ask each
  worker — which you cannot do through one load-balanced URL.

---

## The metrics

Every component carries its own `status` — `ok`, `warn`, `critical`, or
`unknown` — and the top-level `status` is the **worst known** one. `unknown`
never counts: an honest gap must not turn a healthy deployment red, and must not
mask a real failure. The thresholds that produced each verdict are echoed in the
response under `thresholds`, so you never have to read the source to find out
what "warn" meant.

### Database — `database`

| Field | Meaning |
|---|---|
| `reachable` | Did `SELECT 1` come back through a **fresh** pool checkout (the same path a request takes, including `pool_pre_ping`)? |
| `latency_ms` | That round trip. On a failed connect this is how long it took to fail, and is not scored. |
| `error_type` | The exception **class name** only. The driver's message is never included — it routinely quotes the connection string, password and all, and this payload gets pasted into tickets. |
| `pool.*` | `size`, `checked_out`, `checked_in`, `overflow`, `capacity` (= `size + max_overflow`), `in_use_ratio`. |

**Thresholds and why.**

- Latency `warn ≥ 250 ms`, `critical ≥ 1000 ms`. The app and its Postgres sit in
  the same Railway region, where this is single-digit milliseconds. The dashboard
  fans out roughly 47 requests per poll round, so 250 ms of pure connection round
  trip *before any query does work* is already a visibly slow UI and means
  something structural is wrong. At 1000 ms the product is unusable, not slow.
- Pool `warn ≥ 80%` occupancy, `critical at 100%`. At 100% a new request does not
  fail fast — it **blocks** for `pool_timeout` (30 s by default) and only then
  raises, so exhaustion presents as a hung app rather than as errors. 80% is the
  last point at which a traffic burst can still be absorbed without anyone
  waiting.

**Bad looks like:** `reachable: false` (everything is down; `/health` is already
503 and your uptime monitor should have told you first) · `latency_ms` in the
hundreds with an idle pool (network or instance problem, not load) ·
`in_use_ratio` at 1.0 with normal latency (connections are leaking or a slow
query is holding them).

**Do:**
1. `reachable: false` → check the Railway Postgres plugin is up and
   `DATABASE_URL` is still correct. Nothing else matters until this is green.
2. High latency, empty pool → check the Railway status page and whether the app
   and database are still in the same region.
3. Pool at capacity → find the slow query (`pg_stat_activity`), then look for a
   handler that opens a session without closing it. Raising `pool_size` in
   `backend/database.py` hides a leak rather than fixing one.

`pool.status` is `unknown` when the pool implementation cannot report itself —
an in-memory SQLite gets `SingletonThreadPool`, which has no `checkedout()`.
Production (Postgres) and a file-backed SQLite both get `QueuePool` and report
properly. Unknown means *unmeasured*, never *fine*.

### MQTT ingest — `mqtt`

| Field | Meaning |
|---|---|
| `configured` | Is `MQTT_BROKER` set in the environment? |
| `state` | `not_configured` · `listener_running` · `listener_stopped` · `unavailable` · `undeterminable` |
| `broker`, `topic` | Host:port and topic the listener was built with. Not credentials. |
| `listener_thread_alive` | Is the ingest thread still running in this process? |
| `last_event_at`, `last_event_age_seconds` | The newest `machine_events` row **with `source = "mqtt"`**. |
| `messages_ingested` | Always `null` — see below. |

**What the status is built from:** configuration and the listener thread, nothing
else.

- `MQTT_BROKER` unset → `ok` / `not_configured`. MQTT is optional; the same
  telemetry also arrives over HTTP (`/production-records`) and through the
  industrial gateway. Scoring an unused feature as critical trains everyone to
  ignore the field, and then the real outage gets ignored too.
- Configured **and** the thread is alive → `ok`.
- Configured and the thread is **gone** → `critical`. This is the failure this
  component exists to catch: `mqtt_service` catches a failed `connect()`, prints,
  and the thread ends — after which nothing is ingested until the process is
  restarted, silently, forever.

**What it deliberately does not claim.** `mqtt_service.start_mqtt_service()`
builds its paho client inside a local closure and stores it nowhere, so
`client.is_connected()` is unreachable from outside the module and there is no
ingest counter anywhere in the process. So:

- `messages_ingested` is `null`, permanently, with the reason inline in the
  payload. It is not a zero and must never be rendered as one.
- A **live** thread only proves `loop_forever()` has not returned. paho reconnects
  on its own, so the thread survives a broker restart — alive is **not**
  "connected to the broker". A **dead** thread is the strong signal, and it is the
  one that is scored.

**`last_event_age_seconds` is informational and never sets the status.**
`machine_events` records status *transitions* only, so a plant holding one state
through a whole shift legitimately writes none. Age past
`mqtt_quiet_after_minutes` (6 h — longer than any normal unbroken run of one
state) sets `last_event_quiet: true` as a prompt to go and look, not as an alarm.
The query filters on `source = "mqtt"` because the demo simulator, the IoT route,
the industrial gateway and manual edits all write to the same table; without the
filter a seeded demo tenant would masquerade as a live plant.

**Do:** `listener_stopped` on a configured deployment → check the broker is
reachable from Railway, then **restart the web service** (the listener only
starts at boot). If it dies again immediately, the broker credentials or the host
are wrong; the reason is printed in the Railway deploy logs at startup.

### WebSocket — `websocket`

`connections` is the live client count in this process, and `by_tenant` splits it
by the tenant each connection authenticated as (`(unauthenticated)` for
connections with no tenant). The split is what tells you whether the customer who
just phoned actually has anyone connected.

**Thresholds:** `warn ≥ 200`, `critical ≥ 500`. `ConnectionManager.broadcast`
walks every open connection for every payload, so cost is O(connections) per
machine update on the event loop — at 500 sockets one status change means 500
send attempts before the loop is free again.

**Zero is not a fault.** Nobody may be looking. This module has no idea whether
it is the middle of a shift or a Sunday night, and will not guess.

**Do:** at `warn`, plan — a second uvicorn worker, or moving fan-out to a broker.
At `critical`, the live feed itself is the bottleneck and the dashboard will feel
laggy for everyone. If the count is 0 while operators insist the dashboard is
open, the problem is the client's WS URL or the token, not this process.

### Table growth — `tables`

Row counts for the ten append-only tables that grow forever unless something
prunes them: `event_log`, `iot_telemetry`, `industrial_signals`,
`machine_events`, `production_records`, `downtime_logs`, `notifications`,
`ai_recommendations`, `agent_actions`, `audit_logs`. Sorted biggest first.

**Counts are platform-wide, across all tenants** — retention prunes a *table*, not
a tenant, so the tenant filter (ADR-0002) is deliberately lifted for these
queries. That is exactly why the endpoint is platform-owner only: it would
otherwise tell one customer how much data every other customer holds.

**Thresholds:** `warn ≥ 500,000` rows, `critical ≥ 2,000,000` rows *per table*.
These are a **proxy, and a deliberately generous one**. What actually matters is
growth without pruning, and this module stores nothing, so it can only see a
level and never a rate. Read a breach as "go and check the retention job is
running", not as a page. The reasoning behind the numbers: around 500k rows a
filter on a non-indexed column in one of these tables costs hundreds of
milliseconds on Railway's shared Postgres; a couple of million is where the
nightly `pg_dump` — and the restore that has to follow it — stops being quick.

**Cost:** `COUNT(*)` on PostgreSQL is a sequential scan, and this runs one per
table. It is by far the most expensive part of the projection. Read it on demand;
do not put it behind a 30-second poll. `GET /system-health?tables=false` skips
the counts entirely for the cheap liveness picture.

A table that cannot be read (a partially migrated database) reports
`rows: null`, `status: unknown` and its `error_type`; the other nine still count.

**Do:** when a table crosses a threshold, confirm the retention/pruning job is
deployed and actually running, then check its cutoff for that table. If there is
no retention job yet, that is the finding.

### Build — `build`

`sha` is the short git commit of the running build, taken from
`RAILWAY_GIT_COMMIT_SHA` (Railway sets it automatically) — the same value
`/health` returns as `version`. `environment` is `ENV`.

A **missing** sha is a `warn`, not an `ok`. The app serves fine, but during an
incident you cannot answer "which build is live?" and cannot confirm a deploy
actually cut over — a real hole in exactly the thing this endpoint reports on.

**Do:** if `sha` is null on Railway, the git metadata is not reaching the
container; set `GIT_COMMIT_SHA` in the service variables as a fallback. Compare
`sha` against the head of `master` after every deploy — if it hasn't moved, the
deploy didn't cut over.

### Sentry — `sentry`

`configured` is a **boolean**, deliberately. The DSN is a write credential —
anyone holding it can inject events into your project — and this response gets
screenshotted and pasted into tickets. The value never leaves the process, not as
a prefix, not as a length.

`client_active` asks the SDK whether a client is actually live, because
`main.py` wraps `sentry_sdk.init` in a `try/except` that prints and continues:
"DSN set but error monitoring silently off" is a reachable state, and this is how
you catch it. `null` means the SDK isn't installed or is a version without
`get_client()` — unknowable, not false.

| Combination | Status | Meaning |
|---|---|---|
| configured, client active | `ok` | Errors are being captured. |
| configured, client inactive | `warn` | `init` failed at boot. Check the deploy log for `[sentry] init skipped:`. |
| not configured | `warn` | Unhandled 500s exist only in Railway's rolling log buffer — not retained, not searchable after the fact. |

**Do:** set `SENTRY_DSN` in Railway (see `docs/Production-Setup.md` §3) and
redeploy. The code is already wired and gated on the DSN.

### Backups — `backup`

**Backups are not monitored from here, and this endpoint says so instead of
guessing.** `monitored: false`, `status: unknown`, and it never contributes to the
overall verdict.

The dump runs as a scheduled GitHub Actions job and lands in a workflow artifact.
The application has no access to the Actions API, and **nothing in the database
changes when a dump succeeds** — so there is no honest in-app signal to derive.
Any "last backup" timestamp rendered here would be invented, and an invented
green light on backups is far worse than no light at all: it is the one metric
people check once and then trust forever.

**Where the truth lives:** GitHub → Actions → the backup workflow → latest run,
and its artifact.

**Do:** check the workflow's run history has a green run within your RPO, and
that the artifact is a plausible size (a 2 KB dump is an empty database). Then
**restore it somewhere at least once**. A backup you have never restored is not a
backup.

---

## Wiring an external uptime monitor

Do this even though Railway already probes `/health` — Railway's probe restarts
the container, it does not tell *you*. UptimeRobot's free tier is enough;
BetterStack and Pingdom work the same way.

1. Create an **HTTP(s)** monitor pointing at
   `https://<your-api-host>/health` — the API host, not the Vercel frontend.
2. Interval: **5 minutes** (the free-tier floor). Timeout 30 s.
3. Alert condition: **any non-2xx**. `/health` returns 503 when the database is
   unreachable, so a dead database is a real alert and not a 200 with the word
   "degraded" buried in the body that nobody parses.
4. Optional but better: add a **keyword** condition on `"status": "ok"`. That
   catches a proxy or an edge cache returning 200 with a stale or replaced body.
5. Notifications: email **and** one push channel (SMS/WhatsApp/Slack). You want to
   hear it from the monitor, not from the customer.
6. Add a second monitor on the frontend origin so you can tell "the API is down"
   apart from "Vercel is down".

**Do not point the monitor at `/docs`.** It returns 200 whenever the web process
is up, including when the database behind it is dead — which is the single most
common way an uptime monitor ends up monitoring nothing.

**Do not point the monitor at `/system-health`.** It always returns 200 by design
and it requires a platform-owner token; even if you gave a monitor one, you would
be paging yourself over row counts.

Railway's own probe is already configured in `backend/railway.toml`
(`healthcheckPath = "/health"`), so a deploy that cannot reach its database will
not cut over and the last good build keeps serving.

---

## Reading it

```bash
# full picture (includes the COUNT(*) scans)
curl -H "Authorization: Bearer $FOUNDER_TOKEN" https://<api-host>/system-health

# cheap liveness picture, no table scans
curl -H "Authorization: Bearer $FOUNDER_TOKEN" "https://<api-host>/system-health?tables=false"

# the public probe — what your uptime monitor sees
curl -i https://<api-host>/health
```

A 403 means the token belongs to a client workspace. `/system-health` is
restricted to an Admin in the `DEFAULT` (founder) workspace because the table
counts and the ingest probe are unscoped, platform-wide facts.

A token carrying **no** `tenant` claim also gets a 403. The idiom used elsewhere
in this codebase is `get("tenant", DEFAULT_TENANT)`, which treats an absent claim
as the founder workspace — harmless where the fallback only widens a tenant
filter, wrong here, where the answer is every tenant's row counts. Both
`create_access_token` call sites always set the claim, so this cannot happen with
a token AMP issues today; it can happen with one issued before the claim existed
and still inside its expiry window. Absent is not `DEFAULT`, and absent is not
trusted.

---

## Extending it

Add a component to `build_system_health` in `backend/monitoring.py` and give it:

1. a `status` of `ok` / `warn` / `critical` / `unknown`;
2. a threshold **with the reasoning written next to it** — a number nobody can
   justify gets ignored the first time it fires;
3. an entry in the `thresholds` block so the verdict is readable without the
   source;
4. a behavioural test in `backend/test_monitoring.py` naming the failure it pins;
5. a row in this document saying what a bad value looks like and what to do.

And if a thing cannot be measured honestly from inside the application, say so in
the payload — the way `backup` and `messages_ingested` do — rather than shipping
a number that is technically present and quietly wrong.
