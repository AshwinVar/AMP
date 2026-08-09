"""System health — the observability the platform did not have (ADR-0007 shape).

Before this, "is AMP healthy?" had exactly one answer: ``GET /health``, a boolean
on whether the database answered ``SELECT 1``. That is enough for an uptime
monitor and nothing else. It could not tell you that the MQTT ingest thread died
at 03:00 and the plant has been dark since, that the connection pool is
permanently exhausted so every request waits 30 seconds on a checkout, that the
live feed has zero clients while operators insist the dashboard is open, or that
``event_log`` has grown to nine million rows and the nightly dump no longer
finishes. Every one of those is a live production failure that ``/health``
reports as "ok".

This module answers the operator's actual question — *what is degrading, and what
do I do about it?* — as a pure projection in the ADR-0007 style: it reads what
the running process and the database already know, adds no storage, no counters,
no background job and no table. It recomputes on every call, so it cannot go
stale and there is nothing to invalidate.

Seven components, each carrying its own status (ok / warn / critical / unknown)
and the threshold that produced it — the thresholds are echoed in the payload so
a reader never has to guess what "warn" meant:

  * ``database``   — reachability, ``SELECT 1`` round trip in ms, pool occupancy
  * ``mqtt``       — is the ingest listener running, how old the last ingested event is
  * ``websocket``  — live connection count and the per-tenant split
  * ``tables``     — row counts for the append-only tables a retention job must prune
  * ``build``      — the git sha of the running build (``platform_routes.BUILD_SHA``)
  * ``sentry``     — whether a DSN is configured (a boolean, never the DSN)
  * ``backup``     — deliberately NOT a metric; see below

WHAT THIS DELIBERATELY REFUSES TO CLAIM

* **Backup freshness is not knowable from inside the application.** Backups are
  taken by a scheduled GitHub Actions job that ``pg_dump``s the database and
  uploads the dump as a workflow artifact. The app has no access to the Actions
  API, and nothing in the database changes when a dump succeeds — so any in-app
  "last backup" figure would be invented, and an invented green light on backups
  is worse than no light at all. ``backup`` therefore reports
  ``monitored: false`` with a pointer to where the truth actually lives, and it
  never contributes to the overall status.
* **MQTT throughput is not obtainable here.** ``mqtt_service.start_mqtt_service``
  builds its paho client inside a local closure and stores it nowhere, so
  ``client.is_connected()`` is unreachable from outside, and no ingest counter
  exists anywhere in the process. What *is* obtainable is (a) whether the
  listener thread is still alive and (b) how old the newest MQTT-sourced
  ``machine_events`` row is. Both are exposed with their limits stated at the
  field itself, rather than dressed up as a message rate.
* **There is no metrics backend, no time series, no alerting and no queue** —
  there is no queue in this platform at all. This is a projection you poll or
  read on a page, not a monitoring system. ``docs/MONITORING.md`` says so
  plainly so nobody plans against capability that isn't here.

Platform-wide by design: the row counts and the MQTT probe deliberately lift the
request's tenant binding (ADR-0002 auto-scoping) because retention and ingest are
platform concerns, not tenant ones. That is precisely why the endpoint over this
projection must stay platform-owner only — see the route at the bottom.
"""
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

import live_ws
import models
import platform_routes
import tenancy
from auth import require_roles
from database import SessionLocal, engine

# ── Thresholds ────────────────────────────────────────────────
# Every number below is an operational judgement, not a library default. The
# reasoning is written down so the next person can argue with it rather than
# guess at it — a threshold nobody can justify gets ignored the first time it
# fires.

# Database round trip: SELECT 1 including pool checkout and pool_pre_ping's own
# probe. The app and its Postgres sit in the same Railway region, where that is
# single-digit milliseconds. The dashboard fans out roughly 47 requests per poll
# round, so 250 ms of pure connection round trip — before any query does work —
# is already a visibly slow UI, and means something structural is wrong
# (cross-region routing, a saturated pool, a struggling instance). At 1000 ms the
# product is not slow, it is unusable.
DB_LATENCY_WARN_MS = 250.0
DB_LATENCY_CRITICAL_MS = 1000.0

# Pool occupancy: checked-out connections as a share of capacity
# (pool_size + max_overflow). At 100% a new request does not fail fast — it
# BLOCKS for pool_timeout (30 s by default) and only then raises, so exhaustion
# presents as a hung application rather than as errors, which is why it needs its
# own alarm. 80% is the last point at which a burst can still be absorbed without
# anybody waiting.
POOL_WARN_RATIO = 0.80
POOL_CRITICAL_RATIO = 1.0

# Live WebSocket connections in THIS process. live_ws.ConnectionManager.broadcast
# walks every open connection for every payload, so the cost is O(connections)
# per machine update on the event loop: at 500 sockets one status change means
# 500 send attempts before the loop is free again. 200 is where to start planning
# (a second worker, a fan-out broker); 500 is where the live feed itself becomes
# the bottleneck.
WS_WARN_CONNECTIONS = 200
WS_CRITICAL_CONNECTIONS = 500

# Append-only table size. This is a PROXY and deliberately a generous one: what
# actually matters is growth without pruning, and this module stores nothing, so
# it can only see a level, never a rate. Read a breach as "go and check retention
# is running", not as a page. ~500k rows is where a filter on a non-indexed
# column in one of these tables costs hundreds of milliseconds on Railway's
# shared Postgres; a couple of million is where the nightly pg_dump — and the
# restore that has to follow it — stops being quick.
TABLE_WARN_ROWS = 500_000
TABLE_CRITICAL_ROWS = 2_000_000

# How long the newest MQTT-ingested event may be silent before this module
# ANNOTATES it as quiet. It deliberately does NOT drive the mqtt status:
# machine_events rows are written on a status TRANSITION only, so a plant running
# steadily through a whole shift legitimately produces none, and a threshold here
# would page somebody for a well-behaved factory. Six hours is longer than any
# normal unbroken run of one state, so "quiet longer than this" is worth a human
# glance without being an alarm.
MQTT_QUIET_AFTER_MINUTES = 360

# The append-only tables that grow forever unless something prunes them — the
# same set a retention job has to target. Kept as an explicit list here rather
# than imported from the retention module: this projection must keep working
# whether or not a pruning job is deployed, and a monitoring module that cannot
# load when its subject is missing is useless exactly when you need it. If the
# retention job's target list ever diverges from this one, that divergence is
# itself the bug — a table nothing prunes and nothing watches.
UNBOUNDED_TABLES = (
    ("event_log", models.EventLog),
    ("iot_telemetry", models.IoTTelemetry),
    ("industrial_signals", models.IndustrialSignal),
    ("machine_events", models.MachineEvent),
    ("production_records", models.ProductionRecord),
    ("downtime_logs", models.DowntimeLog),
    ("notifications", models.Notification),
    ("ai_recommendations", models.AIRecommendation),
    ("agent_actions", models.AgentAction),
    ("audit_logs", models.AuditLog),
)

_RANK = {"ok": 0, "warn": 1, "critical": 2}


def _worst(*statuses) -> str:
    """The overall verdict: the worst KNOWN component status.

    "unknown" is excluded on purpose. A gap we are honest about — backups, a pool
    that cannot report itself — must not read as a failure, and must not read as
    health either. It reads as nothing, and the component says why."""
    known = [s for s in statuses if s in _RANK]
    if not known:
        return "unknown"
    return max(known, key=lambda s: _RANK[s])


def _threshold_status(value, warn, critical) -> str:
    """ok / warn / critical for a metric where larger is worse. ``None`` — the
    value could not be measured — is "unknown", never a passing "ok"."""
    if value is None:
        return "unknown"
    if value >= critical:
        return "critical"
    if value >= warn:
        return "warn"
    return "ok"


@contextmanager
def _platform_scope():
    """Lift the request's tenant binding for the duration of a platform-wide probe.

    ADR-0002 auto-scopes every ORM SELECT to the caller's tenant. That is right
    for domain reads and wrong here: "how many rows does retention have to prune"
    and "when did MQTT last ingest anything" are questions about the deployment,
    not about one customer. Setting the tenant to ``None`` makes the scoping
    listener return early (it only filters when a tenant is bound), and the token
    is always restored — so a probe cannot leak an unscoped session back into the
    request that called it."""
    token = tenancy.set_current_tenant(None)
    try:
        yield
    finally:
        tenancy.reset_current_tenant(token)


# ── Database ──────────────────────────────────────────────────


def _pool_stats() -> dict:
    """Connection-pool occupancy, or a documented blank when the pool cannot
    report it.

    Not every pool implementation answers these questions. Production (Postgres)
    and a file-backed SQLite both get ``QueuePool``, which does; an IN-MEMORY
    SQLite gets ``SingletonThreadPool``, which has ``size()`` but no
    ``checkedout()`` — so the test suites' own engine is exactly the case that
    would ``AttributeError`` if this reached for the numbers blindly. Every read
    is capability-checked and degrades to ``None`` + status "unknown"."""
    pool = getattr(engine, "pool", None)

    def _stat(method_name):
        method = getattr(pool, method_name, None)
        if not callable(method):
            return None
        try:
            return int(method())
        except Exception:
            return None

    size = _stat("size")
    checked_out = _stat("checkedout")
    checked_in = _stat("checkedin")
    overflow = _stat("overflow")

    # max_overflow has no public accessor on QueuePool, so read it defensively:
    # a SQLAlchemy change should cost us the ratio, not raise inside a health
    # check. A negative value means "unlimited overflow" — there is then no
    # capacity to be a share OF, so the ratio is honestly unknown rather than 0.
    max_overflow = getattr(pool, "_max_overflow", None)
    capacity = None
    if size is not None and isinstance(max_overflow, int) and max_overflow >= 0:
        capacity = size + max_overflow

    ratio = None
    if capacity and checked_out is not None:
        ratio = round(checked_out / capacity, 3)

    return {
        "implementation": type(pool).__name__ if pool is not None else None,
        "size": size,
        "checked_out": checked_out,
        "checked_in": checked_in,
        "overflow": overflow,
        "capacity": capacity,
        "in_use_ratio": ratio,
        "status": _threshold_status(ratio, POOL_WARN_RATIO, POOL_CRITICAL_RATIO),
    }


def _database_health() -> dict:
    """Reachability, round-trip latency and pool occupancy.

    Probes through the ENGINE rather than the caller's session, deliberately: a
    fresh checkout exercises the path a real request takes (pool checkout plus
    ``pool_pre_ping``'s own round trip), whereas reusing an already-open session
    connection would time an empty round trip on a warm socket and report health
    the application does not actually have. Same engine, same honesty as
    ``/health`` in platform_routes — and module-level, so a test can substitute a
    dead one exactly as ``test_health.py`` does."""
    started = time.perf_counter()
    reachable, error = True, None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        reachable = False
        # The exception TYPE, never its message. Driver errors routinely quote
        # the connection string — host, user, sometimes the password — and this
        # payload is read by humans, pasted into tickets and screenshotted.
        error = type(exc).__name__
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    pool = _pool_stats()
    if not reachable:
        # An unreachable database is critical whatever the clock said: the
        # "latency" of a failed connect is the time it took to fail, which is not
        # a health signal and must not be scored as one.
        status = "critical"
    else:
        status = _worst(
            _threshold_status(latency_ms, DB_LATENCY_WARN_MS, DB_LATENCY_CRITICAL_MS),
            pool["status"],
        )

    return {
        "status": status,
        "reachable": reachable,
        "error_type": error,
        "latency_ms": latency_ms,
        "pool": pool,
    }


# ── MQTT ingest ───────────────────────────────────────────────


def _listener_thread_alive():
    """Is mqtt_service's ingest loop still running in this process?

    ``mqtt_service.start_mqtt_service()`` builds its paho client and calls
    ``loop_forever()`` inside a local closure and keeps neither, so there is no
    handle on which to call ``client.is_connected()``. The one thing the process
    still exposes is the daemon thread that closure runs on, so this looks for a
    live thread whose target function belongs to ``mqtt_service``.

    What "alive" proves is narrow: ``loop_forever()`` has not returned. paho
    reconnects by itself while the loop runs, so the thread survives a broker
    restart — alive is therefore NOT "connected to the broker", and this module
    never claims it is. What a DEAD thread proves is stronger and is the case
    worth alerting on: either the initial ``connect()`` raised (mqtt_service
    catches it, prints, and the thread ends) or the loop exited, and nothing will
    be ingested until the process restarts.

    Absence cannot distinguish "never started" from "started and died"; both mean
    the same thing operationally — no MQTT ingest — so both report ``False``.
    Reads ``Thread._target``, a private attribute, so the whole scan is guarded
    and returns ``None`` (unknown) rather than raising if a future CPython stops
    exposing it."""
    try:
        for thread in threading.enumerate():
            target = getattr(thread, "_target", None)
            if getattr(target, "__module__", None) == "mqtt_service" and thread.is_alive():
                return True
        return False
    except Exception:
        return None


def _last_mqtt_event(db):
    """The newest MQTT-ingested machine event, as a datetime, or ``None``.

    Filtered on ``source == "mqtt"`` because every other writer of
    ``machine_events`` stamps its own source (simulator, iot,
    industrial_gateway, manual) — without the filter a demo tenant's simulator
    would masquerade as a live plant, which is the most misleading thing this
    module could possibly do. Note the column's Python default is ``"mqtt"``, so
    a future writer that forgets to pass ``source=`` would be counted here as
    real ingest; that default is the trap, not this filter.

    Read through the ORM (not raw SQL) so SQLAlchemy applies the DateTime result
    processor — SQLite hands back a string otherwise and the age arithmetic would
    blow up in dev while working in production."""
    with _platform_scope():
        row = (db.query(models.MachineEvent)
               .filter(models.MachineEvent.source == "mqtt")
               .order_by(models.MachineEvent.created_at.desc())
               .first())
    return row.created_at if row is not None else None


def _mqtt_health(db, db_reachable: bool) -> dict:
    """Whether the MQTT ingest path is running, and how long since it last wrote.

    Status comes from configuration and the listener thread, never from the event
    age — see MQTT_QUIET_AFTER_MINUTES for why a quiet plant is not a fault."""
    # MQTT is optional: the same telemetry also arrives over HTTP and through the
    # industrial gateway. Absence of MQTT_BROKER in the environment means "this
    # deployment does not ingest over MQTT", which is a configuration fact, not a
    # failure — reporting it as critical would train everyone to ignore this
    # field. Checked against the raw environment because mqtt_service applies a
    # 127.0.0.1 default, which makes its constant unable to answer "was this
    # deliberately configured?".
    configured = bool((os.environ.get("MQTT_BROKER") or "").strip())

    broker, port, topic, importable = None, None, None, True
    try:
        # Imported lazily and inside a guard: this pulls in paho and a session
        # factory, and monitoring must never be the reason the application fails
        # to boot. Using mqtt_service's own constants rather than re-reading the
        # environment keeps one source of truth for the broker defaults.
        import mqtt_identity
        import mqtt_service
        broker, port = mqtt_service.MQTT_BROKER, mqtt_service.MQTT_PORT
        # The subscription is a LIST of filters now, not one topic: the tenant
        # and site are segments of it, so ingest listens on a wildcard plus an
        # optional legacy topic. Reported as the joined filters because "which
        # topics is this deployment actually listening on" is the operational
        # question, and a deployment that has not configured MQTT_LEGACY_TENANT
        # is deaf to the old single-tenant topic — visible here rather than
        # discovered when telemetry stops arriving.
        topic = ", ".join(mqtt_identity.topic_filters(
            mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT))
    except Exception:
        importable = False

    alive = _listener_thread_alive()

    last_at, age_seconds = None, None
    if db_reachable:
        try:
            last_at = _last_mqtt_event(db)
        except Exception:
            # A broken probe must not take the projection with it; the field goes
            # unknown and every other component still reports.
            try:
                db.rollback()
            except Exception:
                pass
    if last_at is not None:
        age_seconds = round((datetime.utcnow() - last_at).total_seconds(), 1)

    if not configured:
        status, state = "ok", "not_configured"
    elif not importable:
        # Configured to ingest but the module will not import (a missing paho, an
        # import-time error) — ingest cannot be running at all.
        status, state = "critical", "unavailable"
    elif alive is True:
        status, state = "ok", "listener_running"
    elif alive is False:
        status, state = "critical", "listener_stopped"
    else:
        status, state = "unknown", "undeterminable"

    return {
        "status": status,
        "state": state,
        "configured": configured,
        "broker": f"{broker}:{port}" if broker else None,   # host:port — not a credential
        "topic": topic,
        "listener_thread_alive": alive,
        "last_event_at": last_at.isoformat() if last_at else None,
        "last_event_age_seconds": age_seconds,
        # Spelled out in the payload, not only in the docs, because whoever reads
        # this at 3am will not have read the docs.
        "last_event_note": ("machine_events records status TRANSITIONS only, so a plant "
                            "holding one state produces none. Age is informational and "
                            "never sets the status."),
        "last_event_quiet": (age_seconds is not None
                             and age_seconds > MQTT_QUIET_AFTER_MINUTES * 60),
        "messages_ingested": None,
        "messages_ingested_note": ("Not obtainable: mqtt_service keeps no counter and holds "
                                   "its paho client in a local closure, so neither a message "
                                   "count nor client.is_connected() is reachable from here."),
    }


# ── WebSocket ─────────────────────────────────────────────────


def _websocket_health() -> dict:
    """Live connection count and the per-tenant split, straight off the in-process
    ConnectionManager.

    Per-process by definition — the manager is a module global holding real
    sockets, so a second uvicorn worker would keep its own. The deployment runs a
    single worker today (Procfile/railway.toml), which makes this the whole
    fleet; the moment ``--workers`` is added it becomes a fraction, and the docs
    say so rather than letting the number quietly change meaning."""
    manager = getattr(live_ws, "manager", None)
    raw = list(getattr(manager, "active_connections", None) or [])

    tenants = []
    for entry in raw:
        # ConnectionManager stores (websocket, tenant) pairs. Tolerating a bare
        # entry costs one line and stops a shape change turning a health check
        # into a 500.
        tenant = entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else None
        tenants.append(tenant or "(unauthenticated)")

    total = len(raw)
    by_tenant = [{"tenant": t, "connections": n}
                 for t, n in sorted(Counter(tenants).items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        # Zero connections is NOT a fault — nobody may be looking. Only volume is
        # scored; "should there be connections?" is a question about the business
        # day, which this module has no business guessing at.
        "status": _threshold_status(total, WS_WARN_CONNECTIONS, WS_CRITICAL_CONNECTIONS),
        "connections": total,
        "by_tenant": by_tenant,
        "scope": "this process only",
    }


# ── Table growth ──────────────────────────────────────────────


def _table_growth(db, db_reachable: bool) -> dict:
    """Row counts for the append-only tables, worst first.

    Platform-wide on purpose (see ``_platform_scope``): retention prunes a table,
    not a tenant. Note the cost — a ``COUNT(*)`` on PostgreSQL is a sequential
    scan, and this runs one per table, which makes it by far the most expensive
    part of the projection. That is why it is separable (``include_tables``) and
    why the docs tell you to read this on demand rather than poll it."""
    if not db_reachable:
        return {"status": "unknown", "counted": False,
                "reason": "database unreachable", "tables": []}

    rows, statuses = [], []
    with _platform_scope():
        for table_name, model in UNBOUNDED_TABLES:
            try:
                count = int(db.query(model).count())
            except Exception as exc:
                # One missing or unreadable table (a partially migrated database,
                # a permission gap) must not cost the other nine their counts.
                # The rollback matters on PostgreSQL: after a failed statement the
                # transaction is aborted and every later query fails too.
                try:
                    db.rollback()
                except Exception:
                    pass
                rows.append({"table": table_name, "rows": None,
                             "status": "unknown", "error_type": type(exc).__name__})
                continue
            status = _threshold_status(count, TABLE_WARN_ROWS, TABLE_CRITICAL_ROWS)
            statuses.append(status)
            rows.append({"table": table_name, "rows": count, "status": status})

    rows.sort(key=lambda r: -(r["rows"] if r["rows"] is not None else -1))
    return {
        "status": _worst(*statuses),
        "counted": True,
        "scope": "all tenants",
        "total_rows": sum(r["rows"] for r in rows if r["rows"] is not None),
        "tables": rows,
    }


# ── Build / Sentry / backup ───────────────────────────────────


def _build_info() -> dict:
    """Which build is actually serving. Read from platform_routes at call time
    rather than copied at import, so there is one BUILD_SHA in the process and a
    test can substitute it."""
    sha = getattr(platform_routes, "BUILD_SHA", None)
    return {
        # A missing sha warns rather than passes: the app serves fine, but during
        # an incident you cannot answer "which build is live?" and cannot confirm
        # a deploy cut over. That is a genuine hole in observability, which is
        # exactly what this endpoint exists to report.
        "status": "ok" if sha else "warn",
        "sha": sha,
        "environment": os.environ.get("ENV") or None,
    }


def _sentry_health() -> dict:
    """Whether unhandled errors are being captured — as a boolean.

    A boolean deliberately: the DSN is a credential (anyone holding it can write
    events into the project) and this payload gets pasted into tickets and
    screenshotted. Not the value, not a prefix, not its length."""
    configured = bool((os.environ.get("SENTRY_DSN") or "").strip())

    # main.py wraps sentry_sdk.init in a try/except that prints and continues, so
    # "DSN set but monitoring silently off" is a real, reachable state. Ask the
    # SDK whether a client is actually live rather than trusting the environment.
    active = None
    try:
        import sentry_sdk
        client = sentry_sdk.get_client()
        is_active = getattr(client, "is_active", None)
        active = bool(is_active()) if callable(is_active) else bool(getattr(client, "dsn", None))
    except Exception:
        active = None   # SDK absent, or a version without get_client — unknowable, not false

    if configured and active is not False:
        status, note = "ok", None
    elif configured:
        status, note = "warn", "SENTRY_DSN is set but the SDK reports no active client — init failed."
    else:
        # Not an outage, but without it an unhandled 500 exists only in Railway's
        # rolling log buffer: not retained, not searchable after the fact.
        status, note = "warn", "SENTRY_DSN is not set — unhandled errors are not captured anywhere."

    return {"status": status, "configured": configured, "client_active": active, "note": note}


def _backup_health() -> dict:
    """Backups are NOT monitored from here, and this says so instead of guessing.

    The dump runs in GitHub Actions and lands in a workflow artifact. The
    application cannot see the Actions API, and a successful dump changes nothing
    in the database, so there is no honest in-app signal to derive. Reporting
    "unknown" with directions is the truthful answer; a fabricated "last backup"
    timestamp would be believed, and believed backup monitoring that monitors
    nothing is worse than none."""
    return {
        "status": "unknown",
        "monitored": False,
        "affects_overall_status": False,
        "reason": ("Backups are taken by a scheduled GitHub Actions job and stored as workflow "
                   "artifacts. The application has no access to the Actions API and nothing in "
                   "the database records a successful dump, so any freshness value shown here "
                   "would be invented."),
        "where_to_check": "GitHub -> Actions -> the backup workflow -> latest run, and its artifact",
    }


# ── The read-model ────────────────────────────────────────────


def build_system_health(db, include_tables: bool = True) -> dict:
    """One object answering "what is degrading, and what do I do about it?".

    A pure projection (ADR-0007): composes the live process state and the
    database's own counts, stores nothing, and recomputes on every call. Every
    component carries its own status; the top-level ``status`` is the worst
    KNOWN one, so an honest gap (backups) neither fails the check nor greens it.

    ``include_tables=False`` skips the row counts — the only expensive part, one
    sequential scan per table on PostgreSQL — for callers that want the cheap
    liveness picture."""
    database = _database_health()
    db_reachable = database["reachable"]

    mqtt = _mqtt_health(db, db_reachable)
    websocket = _websocket_health()
    build = _build_info()
    sentry = _sentry_health()
    backup = _backup_health()
    tables = (_table_growth(db, db_reachable) if include_tables
              else {"status": "unknown", "counted": False,
                    "reason": "row counts skipped by the caller", "tables": []})

    return {
        "generated_at": datetime.utcnow().isoformat(),
        # backup is absent from this call by design — it is "unknown", and
        # _worst() ignores unknowns, but leaving it out makes the intent explicit
        # rather than incidental.
        "status": _worst(database["status"], mqtt["status"], websocket["status"],
                         tables["status"], build["status"], sentry["status"]),
        "database": database,
        "mqtt": mqtt,
        "websocket": websocket,
        "tables": tables,
        "build": build,
        "sentry": sentry,
        "backup": backup,
        # Echoed so a reader can see what produced each verdict without opening
        # the source — the same courtesy connectivity.py extends with
        # stale_after_minutes.
        "thresholds": {
            "db_latency_warn_ms": DB_LATENCY_WARN_MS,
            "db_latency_critical_ms": DB_LATENCY_CRITICAL_MS,
            "pool_warn_ratio": POOL_WARN_RATIO,
            "pool_critical_ratio": POOL_CRITICAL_RATIO,
            "ws_warn_connections": WS_WARN_CONNECTIONS,
            "ws_critical_connections": WS_CRITICAL_CONNECTIONS,
            "table_warn_rows": TABLE_WARN_ROWS,
            "table_critical_rows": TABLE_CRITICAL_ROWS,
            "mqtt_quiet_after_minutes": MQTT_QUIET_AFTER_MINUTES,
        },
    }


# ── Route ─────────────────────────────────────────────────────

router = APIRouter(tags=["Platform"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/system-health")
def system_health(tables: bool = True, db: Session = Depends(get_db),
                  current_user: dict = Depends(require_roles(["Admin"]))):
    """The operator's diagnostic view of the deployment.

    Platform owner only. The row counts and the MQTT probe are deliberately
    unscoped (retention and ingest are deployment concerns), so this would
    otherwise tell a client Admin how much data every other tenant holds — the
    same reasoning that gates /tenant-configs.

    Always answers 200, including when the verdict is "critical". ``/health``
    owns the status-code contract for uptime monitors — it returns 503 when the
    database is down and is what the Railway probe watches. If this endpoint also
    signalled through the status code, pointing a monitor at it would page
    somebody for table growth. Read the ``status`` field, not the HTTP code.

    ``?tables=false`` skips the row counts (one sequential scan per table on
    PostgreSQL) when you only want the liveness picture."""
    # ABSENT IS NOT DEFAULT. The idiom used elsewhere in this codebase is
    # `get("tenant", DEFAULT_TENANT)`, which reads a token with no tenant claim
    # as the platform owner. That is fine where the fallback only widens a
    # tenant filter; it is not fine here, where the answer is every tenant's row
    # counts. Both mint paths in core_routes.py always set the claim, so this is
    # unreachable from a token AMP issues today — but a token minted before the
    # claim existed is still valid until it expires, and a guard that hands the
    # platform-wide view to a claimless token is a guard that fails open.
    if current_user.get("tenant") != tenancy.DEFAULT_TENANT:
        raise HTTPException(status_code=403, detail="Platform owner only")
    return build_system_health(db, include_tables=tables)
