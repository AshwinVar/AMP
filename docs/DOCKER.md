# Running AMP in Docker

## Why this exists

AMP is deployed on Railway against **PostgreSQL 16**. Every automated test in
this repo runs against **SQLite**. That divergence is not theoretical — it has
already produced production bugs, because the code that differs most between the
two dialects is precisely the code no unit test exercises: the boot path in
`main.py`, which runs `create_all`, around forty idempotent `ALTER TABLE` /
`CREATE INDEX IF NOT EXISTS` migrations, a `completed_at` backfill, and the
tenant-column migration in `tenancy.py`, before it seeds anything.

Separately, `mqtt_service.start_mqtt_service()` runs on every startup and
connects to `MQTT_BROKER` (default `127.0.0.1:1883`). No developer machine has
ever had a broker there, so the MQTT ingest path — machine status, production
counts, breakdown downtime logs, the live WebSocket broadcast — has never once
run outside the deployed environment. The connect fails, the exception is caught
and printed, and half the product is silently absent locally.

This stack fixes both. Three files, all at the repo root:

| File | Purpose |
|---|---|
| `Dockerfile` | The backend as a production-shaped image. Runs the *same* command as Railway. |
| `docker-compose.yml` | Postgres 16 + Mosquitto + the backend, plus a test runner behind a profile. |
| `.dockerignore` | Keeps the venv, `node_modules`, `.git`, tests, `*.db` and docs out of the build. |

---

## Prerequisites

- Docker Engine 24+ with the Compose plugin.
- **Docker Compose v2.23.1 or newer.** The compose file uses the inline
  `configs: content:` form to create the test database. Check with
  `docker compose version`; if you are older, see
  [Troubleshooting](#compose-rejects-the-configs-block).
- Nothing else. No local Python, no local Postgres, no local broker.

---

## Quick start

```bash
cd /path/to/AMP
docker compose up --build
```

First build takes a couple of minutes (dependency layer); subsequent builds are
seconds unless `backend/requirements.txt` changed.

When it is up:

```bash
curl -s localhost:8000/health
# {"status":"ok","database":"ok","time":"...","version":null}
```

`"database":"ok"` is the whole point — the API is answering **from PostgreSQL**.

Then log in the way the post-deploy smoke test does (see
`docs/Production-Setup.md` §7):

```bash
curl -s -X POST localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"gmats","password":"gmats@2026"}'
```

Stop with `Ctrl+C`. `docker compose down` removes the containers and keeps your
data; `docker compose down -v` also drops the database volume and gives you a
clean factory next time.

### What is running

| Service | Image | Port | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 | Named volume `amp_pgdata`. Healthchecked. |
| `mosquitto` | `eclipse-mosquitto:2` | 1883 | Anonymous listener, local only. |
| `backend` | built from `Dockerfile` | 8000 | Waits for Postgres to be *healthy*, not merely started. |
| `tests` | built from `Dockerfile` | — | Profile `tests`. Not started by `up`. |
| `frontend` | — | — | Commented out; there is no `frontend/Dockerfile` yet. |

The frontend still runs on the host: `cd frontend && npm run dev`. It talks to
`http://localhost:8000`, which is already in the backend's `ALLOWED_ORIGINS`.

---

## Running the test suite against real PostgreSQL

This is the reason the stack exists.

```bash
docker compose up -d postgres          # or `docker compose up -d` for everything
docker compose run --rm tests
```

That runs the same loop `.github/workflows/ci.yml` runs — every `test_*.py` in
`backend/`, every suite executed even if an earlier one fails, non-zero exit if
any failed — with `DATABASE_URL` pointing at a real Postgres 16.

A single suite:

```bash
docker compose run --rm tests python test_health.py
```

An interactive shell in the same environment:

```bash
docker compose run --rm tests sh
```

### Be precise about what this proves

Do not oversell this. Of the ~163 suites in `backend/`:

- **About 140 build their own engine** with
  `create_engine("sqlite://", connect_args={"check_same_thread": False})`.
  Those suites do **not** change behaviour here. They still run on SQLite,
  in-memory, exactly as they do in CI. Pointing `DATABASE_URL` at Postgres does
  not convert them, and no amount of Docker will.
- **The remaining two dozen** — the ones that import `main` or use
  `database.SessionLocal` — genuinely run against PostgreSQL here.
  `grep -L sqlite backend/test_*.py` lists them.
- **Importing `main` at all** runs the entire boot path against PostgreSQL:
  `create_all` for every model, every `_ensure_column` / `_ensure_index` /
  `ensure_tenant_columns` migration, `_backfill_completed_at`, and the tenant and
  GMATS seeding. This is the part that has actually bitten us, and it is now
  covered on every test run rather than on every deploy.

So: this closes the dialect gap for **schema, migrations, boot and seeding**, and
for the suites that use the shared engine. It does not magically make the
in-memory suites dialect-aware. Converting those is a separate, larger piece of
work — the honest next step is to move them onto a shared fixture that reads
`DATABASE_URL`, one suite at a time.

**Expect the first run to fail.** Failures here are the parity gap becoming
visible, which is the point. Read them as findings, not as a broken setup.

### Where the test data lives

The `tests` service uses a **separate database**, `amp_test`, created by the
inline init script in the compose file when the Postgres volume is first
initialised. Running the suites therefore never reshapes the `amp` database you
were developing against — which matters, because importing `main` seeds and
migrates, and several suites write rows.

The `tests` service also bind-mounts `./backend` over `/app`, because the image
deliberately excludes `test_*.py` (see `.dockerignore`). Two consequences:

1. Edits to a test take effect immediately, with no rebuild.
2. Your real `backend/.env` is visible inside the container. It is harmless:
   `load_dotenv()` does not override variables that are already set, so the
   compose `DATABASE_URL` wins. If anyone ever changes that call to
   `override=True`, this service breaks and this is the first place to look.

---

## Seeding

Startup seeding is automatic and idempotent — `main.py`'s startup event seeds the
per-tenant config, the `gmats` client login, one demo PLC per industrial
protocol, and enough production records and machine events for OEE and the
timeline to have something to show. Just bring the stack up.

### Rebuilding the demo factory (SMT → IC plant)

`backend/reset_factory.py` wipes the DEFAULT tenant's machines and everything
that hangs off them, then seeds the two-line instrument-cluster factory. Run it
directly against the running stack:

```bash
docker compose run --rm backend python reset_factory.py
```

That is the direct route and the one to prefer locally: it runs, prints, and
exits.

The other route is the `RESEED_FACTORY` flag `main.py` reads on its startup
event — the mechanism used on Railway, where there is no shell. To rehearse *that*
path rather than the script:

```bash
docker compose run --rm -e RESEED_FACTORY=$(date +%s) backend
# watch for "[RESEED] DEFAULT rebuilt ...", then Ctrl+C
```

Note this starts a *second* backend process (without published ports, since
`run` does not publish them), does the reseed during startup, and then goes on
serving — so you stop it yourself once the log line appears.

`RESEED_FACTORY` is **single-shot per value**: each value is recorded in the
append-only `event_log` and consumed exactly once, so a forgotten variable cannot
reseed on every boot. That guard exists because a forgotten variable wiped
production roughly 41 times on 2026-07-18. Use a fresh value (a timestamp) each
time you genuinely want a rebuild, and never put it in `docker-compose.yml`.

### Talking to the broker

With the stack up, publish a machine reading from the host and watch it land:

```bash
mosquitto_pub -h localhost -p 1883 -t flowmes/DEFAULT/-/machines \
  -m '{"machine":"SMT-1","status":"Running","utilization":72,
       "total_count":100,"good_count":97,"rejected_count":3}'

docker compose logs -f backend    # look for "DB UPDATED -> SMT-1"
```

No local `mosquitto_pub`? Use the broker container's own client:

```bash
docker compose exec mosquitto mosquitto_pub -t flowmes/DEFAULT/-/machines -m '{"machine":"SMT-1","status":"Breakdown"}'
```

This is the first time that path has been runnable off the deployed box. It is
worth exercising the awkward payloads deliberately — a non-numeric count, a
negative count, `Infinity` — since `mqtt_service._non_negative_int` exists
entirely because edge gateways send all three.

---

## Using the image on its own

The image is a faithful rehearsal of the Railway process, not a compose-only
artefact:

```bash
docker build --build-arg GIT_COMMIT_SHA=$(git rev-parse HEAD) -t amp-backend .

docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql://user:pass@host:5432/db' \
  -e SECRET_KEY='...' \
  -e ALLOWED_ORIGINS='https://app.marx8.com' \
  amp-backend
```

`GIT_COMMIT_SHA` is optional but recommended: `platform_routes.BUILD_SHA` serves
its first seven characters as `version` from `/health`, which is how you tell
which build a container actually is.

Notes:

- **The start command is byte-identical to `backend/Procfile` and to
  `railway.toml`'s `startCommand`.** Keep all three in step. If the Dockerfile
  drifts, the image stops being a rehearsal of production and the exercise loses
  its value.
- **`PORT` is honoured** the same way the Procfile honours it, defaulting to
  8000 when nothing injects it.
- **One worker, deliberately.** Do not add `--workers` and do not set
  `WEB_CONCURRENCY` (uvicorn reads that variable and forks silently). This
  process owns in-memory state that does not survive duplication: the 45-second
  simulation loop, the MQTT subscriber thread, the rate-limit counters in
  `http_security`, and the plan-gate licence cache. Two workers means two
  simulation loops writing to the same tenants and two MQTT subscribers
  double-ingesting every message.
- **Railway still builds with NIXPACKS** (`backend/railway.toml`). This image is
  the local-parity and staging vehicle. Switching Railway to it later should be a
  configuration change and nothing else — which is exactly why the start command
  is kept identical.

---

## Troubleshooting

### `backend` exits immediately with a connection error

It almost certainly started before Postgres was accepting TCP connections.
`depends_on: condition: service_healthy` should prevent this; confirm the
healthcheck is actually passing:

```bash
docker compose ps
docker compose logs postgres
```

`main.py` opens the database at **import** time (`Base.metadata.create_all`), so
there is no retry loop to save it — it dies during import. That is intentional:
`database.py` has no fallback because a missing or unreachable `DATABASE_URL`
must fail loudly.

### `/health` returns 503

Working as designed. `/health` reports health in the **status code**: 200 only
when the database also answers, 503 otherwise. The container's `HEALTHCHECK`
relies on that, and so does `railway.toml`'s probe. Look at `docker compose logs
postgres`.

### The container is `unhealthy` during a cold start

The healthcheck has a 90-second `start-period` because boot genuinely takes a
while — `create_all`, forty-odd migrations, the backfill and the seeding all run
before the first request is served. If it is still unhealthy after that, read
`docker compose logs backend`: the `[MIGRATE]` lines will tell you which
statement Postgres rejected.

### Compose rejects the `configs` block

Inline `configs: content:` needs Compose v2.23.1+ (November 2023). On an older
version, delete the `configs:` key from the `postgres` service and the top-level
`configs:` block, then create the test database by hand once:

```bash
docker compose exec postgres createdb -U amp -O amp amp_test
```

### `mosquitto` shows as unhealthy but the broker works

The healthcheck shells out to `mosquitto_sub`, which is bundled in the official
image. If a future image drops the clients package the check fails while the
broker itself is fine. Nothing depends on that check — it is diagnostic only —
so you can ignore it or delete the `healthcheck` block from that service.

### `mosquitto` cannot find `/mosquitto-no-auth.conf`

That file is shipped inside the official 2.x image so a broker can be run
without writing a config; the compose `command:` points at it. If a future image
drops it, the equivalent is a two-line config (`listener 1883` and
`allow_anonymous true`) mounted at `/mosquitto/config/mosquitto.conf`. Anonymous
access is acceptable **locally only** — never point a deployed environment at an
open broker.

### Port 5432, 1883 or 8000 already in use

Something local is already bound. Change the **left** side of the port mapping
only (`"5433:5432"`); the right side is the in-container port that other
services address by service name and must not change.

### The build is slow or the image is large

Check `.dockerignore` is being honoured — the build context should be a few MB,
not hundreds. `backend/venv/`, `frontend/node_modules/`, `tree.txt` (1.6 MB),
`backend/ci.db` and `backend/flowmes.db` are all excluded. If a build says it is
transferring hundreds of megabytes of context, a pattern has stopped matching.

### Changes to backend code do not show up

The default `backend` service runs the **image**, not your working tree, so a
code change needs `docker compose up --build backend`. If you want live reload,
uncomment the paired `volumes:` and `command:` block in the `backend` service —
both together, not one of them.

---

## Not covered by this change

**There is still no staging environment.** This gives you a production-shaped
environment on your own machine, which is the cheaper half of the problem. A
real staging deployment — a second Railway environment fed from `dev`, with its
own database and its own smoke test — is separate work, and the image here is
the natural thing to deploy to it.
