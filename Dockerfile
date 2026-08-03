# AMP backend — production image.
#
# WHY THIS EXISTS
# AMP had no container image and no local environment that resembled production.
# Production is Postgres on Railway; every test in the repo runs on SQLite, and
# that divergence has already shipped bugs (dialect-specific DDL in the boot-time
# migrations, NULL/type coercion differences in read-models). There was also no
# way to stand up the MQTT broker that mqtt_service.py connects to on startup, so
# the ingest path was effectively untestable off the deployed box.
#
# This image is one half of the fix: a single artefact that runs the same command
# as Railway does, against a real Postgres, on any machine. docker-compose.yml is
# the other half — see docs/DOCKER.md.
#
# The image is NOT (yet) what Railway builds. Railway still uses NIXPACKS per
# backend/railway.toml. This image is the local-parity and staging vehicle; the
# start command below is kept byte-identical to backend/Procfile precisely so
# that switching Railway over later is a config change, not a behaviour change.

FROM python:3.11-slim

# PYTHONUNBUFFERED is not cosmetic here. The application logs entirely through
# bare print() — "[MIGRATE] ...", "[SEED] ...", "[SIM TICK ERROR] ..." — and a
# block-buffered stdout means those lines sit in an 8 KB buffer instead of
# reaching `docker logs`. A crash during boot would then produce a container that
# died with no output at all, which is the worst possible failure to debug.
#
# PYTHONDONTWRITEBYTECODE keeps __pycache__ out of the image, and (more usefully)
# stops the non-root user tripping over an unwritable bind mount when
# docker-compose.yml mounts ./backend over /app for the test service.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The account the server will run as (USER is switched below, once everything
# that needs root has happened). The uid is pinned explicitly rather than left to
# whatever useradd picks next: a bind mount carries host file ownership by
# NUMBER, so a uid that drifts between rebuilds turns into "permission denied" on
# a colleague's machine and not on yours. 10001 sits clear of Debian's
# system-uid range. Created early so it lands in a layer that almost never
# rebuilds.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin amp

WORKDIR /app

# Dependencies first, in their own layer, from their own COPY. Application code
# changes on every commit; requirements.txt changes a few times a year. Copying
# the whole tree before installing would re-run pip on every single code edit.
COPY backend/requirements.txt ./requirements.txt

# No apt-get layer at all, deliberately. Every pinned dependency ships a
# manylinux wheel for cp311: psycopg2-binary bundles libpq, cryptography (via
# python-jose[cryptography]) and bcrypt bundle their Rust/C extensions. Nothing
# is compiled from source, so there is no need for build-essential, no need for
# a multi-stage builder to throw it away again, and no apt package database to
# go stale inside the image. If a future dependency does need to compile, add a
# builder stage rather than leaving a toolchain in the runtime image.
RUN pip install --no-cache-dir -r requirements.txt

# Application code. --chown avoids a second full-tree layer just to fix
# ownership. What arrives here is filtered by /.dockerignore: no tests, no
# .env, no *.db, no docs, no venv.
COPY --chown=amp:amp backend/ ./

# Late so it cannot invalidate the dependency layer. platform_routes.BUILD_SHA
# reads GIT_COMMIT_SHA (falling back to RAILWAY_GIT_COMMIT_SHA) and serves the
# first 7 characters as `version` from /health — which is how you tell which
# build a running container actually is. Pass it at build time:
#   docker build --build-arg GIT_COMMIT_SHA=$(git rev-parse HEAD) -t amp-backend .
ARG GIT_COMMIT_SHA=""
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

USER amp

# Railway injects PORT. Everywhere else there is nothing to inject it, so give
# the same default the compose file and the docs use. EXPOSE is documentation
# for humans and `docker ps`; it publishes nothing on its own.
ENV PORT=8000
EXPOSE 8000

# Probe the real health endpoint, matching railway.toml's healthcheckPath and for
# the same reason recorded there: /health returns 200 only when the database also
# answers, and 503 when it does not, so a container that has lost its database is
# actually detectable. urlopen raises on a 503, which is exactly the semantics we
# want — no `curl` in the image, no extra apt layer, stdlib only.
#
# start-period is generous because boot is genuinely slow: main.py runs
# create_all, roughly forty idempotent ALTER/CREATE INDEX migrations, a
# completed_at backfill and the tenant/GMATS seeding before the first request is
# served. A short start-period would mark a perfectly healthy cold start as
# unhealthy and, under an orchestrator, restart it into a loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/health', timeout=4)" || exit 1

# Byte-identical to the command in backend/Procfile and to railway.toml's
# startCommand. Keep all three in step: if this drifts, the container stops being
# a faithful rehearsal of production and the whole point of the image is lost.
#
# Shell form (sh -c) rather than a bare exec array because $PORT must expand at
# run time — the same reason the Procfile writes $PORT. Debian's /bin/sh is dash,
# which execs a single simple command rather than forking it, so uvicorn still
# ends up as PID 1 and still receives SIGTERM directly on `docker stop`. Do not
# turn this into a multi-command string without adding an explicit `exec` to the
# last one, or shutdown becomes a ten-second timeout and a SIGKILL.
#
# ONE worker, on purpose. Do NOT add --workers or set WEB_CONCURRENCY (uvicorn
# reads that env var and silently forks): this process owns in-memory state that
# does not survive being duplicated — the 45-second simulation loop, the MQTT
# subscriber thread, the rate-limit counters in http_security and the plan-gate
# licence cache. Two workers means two simulation loops writing to the same
# tenants and two MQTT subscribers double-ingesting every message. Scale out by
# running more containers only after that state has been moved out of process.
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port $PORT"]
