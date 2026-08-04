"""System-health read-model tests.

Observability was a /health boolean and an optional Sentry DSN, so every failure
below reported as "ok": a dead MQTT listener, an exhausted pool, a database whose
driver error carried the connection string into an ops payload, a simulator
masquerading as live plant ingest, and unbounded tables nobody was counting.
Each test names the failure it pins; where a test could pass both before and
after the fix, it carries a CONTROL case that separates the two.

Runs entirely against SQLite (in-memory for the projection, a temporary file for
the one case that needs a real QueuePool).

Run:  python backend/test_monitoring.py     (exit 0 = pass)
"""
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import models
import monitoring
import platform_routes
import tenancy
from database import Base

# The scoping listener is installed by main.py at boot. These suites never import
# main, so install it here — without it the "counts are platform-wide" control
# would pass vacuously, because nothing would be scoping anything.
tenancy.install_scoping()


def _fresh_session(engine):
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


@contextmanager
def _serving(engine):
    """Point the module at a test engine, exactly as test_health.py substitutes
    platform_routes.engine — the DB probe deliberately goes through the engine,
    not the caller's session, so it has to be swappable."""
    original = monitoring.engine
    monitoring.engine = engine
    try:
        yield
    finally:
        monitoring.engine = original


@contextmanager
def _env(**values):
    """Set/clear environment variables and always put them back."""
    previous = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _quiet_mqtt():
    """Pin MQTT out of the picture for tests that assert on the OVERALL verdict.

    database.py calls load_dotenv() at import, so a developer's .env puts a real
    MQTT_BROKER into os.environ while CI has none — without pinning it, a
    whole-payload assertion passes on one machine and fails on the other for
    reasons that have nothing to do with what is being tested."""
    with _env(MQTT_BROKER=None):
        yield


@contextmanager
def _connections(pairs):
    """Install (websocket, tenant) pairs on the live ConnectionManager."""
    manager = monitoring.live_ws.manager
    original = manager.active_connections
    manager.active_connections = list(pairs)
    try:
        yield
    finally:
        manager.active_connections = original


class _DeadEngine:
    """An engine whose every connect fails the way an unreachable Postgres does —
    with the connection string inside the message, which is why drivers' error
    text must never be echoed."""

    LEAK = "postgresql://amp_user:hunter2@db.internal:5432/amp"

    pool = None

    def connect(self):
        raise RuntimeError(f'could not connect to server: connection to "{self.LEAK}" refused')


# ── Database ──────────────────────────────────────────────────


def test_unreachable_database_is_critical_and_never_echoes_the_credentials():
    """Pins two failures at once: an unreachable database scoring as healthy
    because the failed connect returned quickly, and the driver's error text —
    which carries user, password and host — being copied into a payload that gets
    pasted into tickets. Only the exception TYPE may escape."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    with _quiet_mqtt(), _serving(_DeadEngine()):
        payload = monitoring.build_system_health(db)

    database = payload["database"]
    assert database["reachable"] is False
    assert database["status"] == "critical", "a database that will not answer is not 'ok' because it failed fast"
    assert database["error_type"] == "RuntimeError"
    serialised = json.dumps(payload)
    assert "hunter2" not in serialised and _DeadEngine.LEAK not in serialised, \
        "the driver's message reached the payload — that is a credential leak"
    assert payload["status"] == "critical", "the worst component must set the overall verdict"

    # CONTROL: the same call against a database that DOES answer must not report
    # critical, or the assertions above would hold for any input.
    with _quiet_mqtt(), _serving(engine):
        healthy = monitoring.build_system_health(db)
    assert healthy["database"]["reachable"] is True
    assert healthy["database"]["status"] == "ok"
    assert healthy["status"] != "critical"
    print("PASS unreachable DB is critical, and the connection string never leaves the process")


def test_a_dead_database_still_returns_every_other_component():
    """Pins: one failing probe aborting the whole read. A health endpoint that
    500s when something is unhealthy tells you nothing at the moment you most
    need it — the other components must still report."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    with _quiet_mqtt(), _serving(_DeadEngine()):
        payload = monitoring.build_system_health(db)

    for key in ("database", "mqtt", "websocket", "tables", "build", "sentry", "backup", "thresholds"):
        assert key in payload, f"{key} vanished because the database probe failed"
    # The DB-derived fields say "unknown", not zero: an uncountable table is not
    # an empty one, and a missing MQTT event is not silence from the plant.
    assert payload["tables"]["counted"] is False
    assert payload["tables"]["status"] == "unknown"
    assert payload["mqtt"]["last_event_at"] is None
    print("PASS a dead database degrades its own fields, not the whole projection")


def test_pool_stats_degrade_when_the_pool_cannot_report_itself():
    """Pins: reaching for pool.checkedout() blindly. An in-memory SQLite gets
    SingletonThreadPool, which has size() but no checkedout(), so a naive read
    AttributeErrors — inside the very endpoint you called to find out what is
    wrong."""
    memory = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with _serving(memory):
        stats = monitoring._pool_stats()
    assert stats["implementation"] == "SingletonThreadPool"
    assert stats["checked_out"] is None
    assert stats["in_use_ratio"] is None
    assert stats["status"] == "unknown", "an unmeasurable pool is unknown, never a passing 'ok'"

    # CONTROL: a pool that CAN report itself must produce real numbers and a real
    # verdict, otherwise "degrades gracefully" would be indistinguishable from
    # "never reports anything".
    workdir = tempfile.mkdtemp()
    filed = create_engine(f"sqlite:///{os.path.join(workdir, 'pool.db')}")
    try:
        conn = filed.connect()
        with _serving(filed):
            stats = monitoring._pool_stats()
        conn.close()
        assert stats["implementation"] == "QueuePool"
        assert isinstance(stats["checked_out"], int) and stats["checked_out"] >= 1
        assert isinstance(stats["capacity"], int) and stats["capacity"] > 0
        assert stats["status"] == "ok"
    finally:
        filed.dispose()
        shutil.rmtree(workdir, ignore_errors=True)
    print("PASS pool stats report real occupancy where they can and 'unknown' where they cannot")


# ── WebSocket ─────────────────────────────────────────────────


def test_websocket_reports_the_per_tenant_split():
    """Pins: a bare connection count. 'Three clients' does not tell an operator
    whether the customer who phoned has anyone connected — the tenant split
    does. Anonymous connections are named, not dropped."""
    with _connections([(object(), "DEFAULT"), (object(), "GMATS"),
                       (object(), "GMATS"), (object(), None)]):
        ws = monitoring._websocket_health()

    assert ws["connections"] == 4
    assert ws["by_tenant"][0] == {"tenant": "GMATS", "connections": 2}, "busiest tenant first"
    assert {"tenant": "DEFAULT", "connections": 1} in ws["by_tenant"]
    assert {"tenant": "(unauthenticated)", "connections": 1} in ws["by_tenant"]
    assert ws["status"] == "ok"

    # CONTROL: an idle process reports zero and stays ok — nobody looking at the
    # dashboard is not a fault, and scoring it as one would make the field noise.
    with _connections([]):
        idle = monitoring._websocket_health()
    assert idle["connections"] == 0 and idle["by_tenant"] == [] and idle["status"] == "ok"
    print("PASS WebSocket reports the per-tenant split, and idle is not a fault")


def test_websocket_status_escalates_at_the_documented_thresholds():
    """Pins: a threshold that exists in a comment but not in the code. broadcast()
    is O(connections) per payload, so the connection count is a real ceiling —
    the boundaries it is scored against must be the published ones."""
    warn_at = monitoring.WS_WARN_CONNECTIONS
    critical_at = monitoring.WS_CRITICAL_CONNECTIONS
    for count, expected in ((warn_at - 1, "ok"), (warn_at, "warn"), (critical_at, "critical")):
        with _connections([(object(), "DEFAULT")] * count):
            assert monitoring._websocket_health()["status"] == expected, \
                f"{count} connections should be {expected}"
    print(f"PASS WS status turns at the published {warn_at}/{critical_at} boundaries")


# ── MQTT ──────────────────────────────────────────────────────


def _mqtt_thread():
    """A live thread whose target belongs to mqtt_service — what
    start_mqtt_service leaves behind, and the only trace of it the process keeps."""
    stop = threading.Event()

    def run():
        stop.wait(10)

    run.__module__ = "mqtt_service"   # the attribute the probe actually matches on
    thread = threading.Thread(target=run, daemon=True)
    return thread, stop


def test_mqtt_not_configured_is_not_reported_as_broken():
    """Pins: 'MQTT is down' and 'this deployment does not use MQTT' looking
    identical. Telemetry also arrives over HTTP and the industrial gateway, so an
    unset broker is a configuration fact — scoring it critical trains everyone to
    ignore the field, and then the real outage is ignored too."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)

    with _env(MQTT_BROKER=None):
        mqtt = monitoring._mqtt_health(db, db_reachable=True)
    assert mqtt["configured"] is False
    assert mqtt["state"] == "not_configured" and mqtt["status"] == "ok"

    # CONTROL: configured, but no listener thread in this process — the real
    # outage. It must NOT come back ok, or the assertion above would be vacuous.
    with _env(MQTT_BROKER="broker.internal"):
        mqtt = monitoring._mqtt_health(db, db_reachable=True)
    assert mqtt["configured"] is True
    assert mqtt["listener_thread_alive"] is False
    assert mqtt["state"] == "listener_stopped" and mqtt["status"] == "critical", \
        "a configured broker with a dead ingest loop is the outage this module exists to catch"
    print("PASS unconfigured MQTT reads ok; a configured broker with a dead listener reads critical")


def test_mqtt_listener_thread_is_detected_while_it_runs():
    """Pins: the listener probe never finding anything, which would make the
    critical verdict above permanent and therefore worthless. mqtt_service keeps
    its paho client in a local closure, so the live thread is the only handle
    left — this proves the probe actually resolves it."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    thread, stop = _mqtt_thread()
    thread.start()
    try:
        with _env(MQTT_BROKER="broker.internal"):
            mqtt = monitoring._mqtt_health(db, db_reachable=True)
        assert mqtt["listener_thread_alive"] is True
        assert mqtt["state"] == "listener_running" and mqtt["status"] == "ok"
        # It reports the thread, and refuses to dress that up as broker
        # connectivity or a message rate it cannot see.
        assert mqtt["messages_ingested"] is None
        assert "not obtainable" in mqtt["messages_ingested_note"].lower()
    finally:
        stop.set()
        thread.join(timeout=5)

    # CONTROL: once the loop exits, the same probe must flip. A probe that only
    # ever says True is no better than one that only ever says False.
    with _env(MQTT_BROKER="broker.internal"):
        assert monitoring._mqtt_health(db, db_reachable=True)["listener_thread_alive"] is False
    print("PASS the listener probe tracks the ingest thread in both directions")


def test_mqtt_event_age_ignores_simulator_written_events():
    """Pins the most misleading thing this module could do: reporting the demo
    simulator's machine events as live plant ingest. machine_events is written by
    five sources (mqtt, simulator, iot, industrial_gateway, manual); only the
    first is evidence that the broker is delivering."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    now = datetime.utcnow()
    db.add(models.MachineEvent(machine_name="SMT-Reflow-01", new_status="Running",
                               source="simulator", created_at=now))
    db.add(models.MachineEvent(machine_name="IC-Test-01", new_status="Running",
                               source="mqtt", created_at=now - timedelta(hours=9)))
    db.commit()

    with _env(MQTT_BROKER="broker.internal"):
        mqtt = monitoring._mqtt_health(db, db_reachable=True)
    assert mqtt["last_event_age_seconds"] > 8 * 3600, \
        "the simulator's fresh event was counted as MQTT ingest"
    assert mqtt["last_event_quiet"] is True
    # Quiet ANNOTATES; it must not drive the verdict — a plant holding one state
    # all shift writes no transitions and is not broken.
    assert mqtt["status"] == "critical" and mqtt["state"] == "listener_stopped", \
        "the age must not be what sets the status"

    # CONTROL: with only simulator rows there is no MQTT evidence at all, and the
    # module says so instead of borrowing the simulator's timestamp.
    only_sim = _fresh_session(create_engine("sqlite://", connect_args={"check_same_thread": False}))
    only_sim.add(models.MachineEvent(machine_name="SMT-Reflow-01", new_status="Running",
                                     source="simulator", created_at=now))
    only_sim.commit()
    with _env(MQTT_BROKER="broker.internal"):
        blank = monitoring._mqtt_health(only_sim, db_reachable=True)
    assert blank["last_event_at"] is None and blank["last_event_age_seconds"] is None
    print("PASS MQTT event age counts MQTT-sourced rows only, and never sets the status")


# ── Table growth ──────────────────────────────────────────────


def test_table_counts_are_platform_wide_not_tenant_scoped():
    """Pins: counting rows through the tenant filter. Retention prunes a TABLE, so
    a founder looking at growth needs the deployment's real total — under
    ADR-0002 auto-scoping the same query silently returns one tenant's slice, and
    the number looks reassuringly small while the table fills."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    for tenant in ("DEFAULT", "DEFAULT", "GMATS"):
        db.add(models.MachineEvent(tenant_code=tenant, machine_name="M1",
                                   new_status="Running", source="mqtt"))
    db.commit()

    token = tenancy.set_current_tenant("GMATS")
    try:
        # CONTROL: the same session, the same model, without the lift — this is
        # what the count would have been, and it is wrong for this question.
        scoped = db.query(models.MachineEvent).count()
        growth = monitoring._table_growth(db, db_reachable=True)
        # The lift lasts exactly as long as the probe: the caller's binding is
        # still in place afterwards. Without this, one health check would quietly
        # unscope the rest of the request.
        assert tenancy.current_tenant() == "GMATS", "the probe left the session unscoped"
    finally:
        tenancy.reset_current_tenant(token)

    assert scoped == 1, "the scoping layer is not active — this test would prove nothing"
    counts = {row["table"]: row["rows"] for row in growth["tables"]}
    assert counts["machine_events"] == 3, "the count was filtered to the caller's tenant"
    assert growth["scope"] == "all tenants"
    print("PASS row counts are platform-wide, and the tenant scope is restored after")


def test_one_unreadable_table_does_not_cost_the_others_their_counts():
    """Pins: a partially migrated database returning nothing at all. A single
    missing table used to abort the loop — and on PostgreSQL the failed statement
    aborts the transaction, so every later count fails too unless it rolls back."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    db.add(models.MachineEvent(machine_name="M1", new_status="Running", source="mqtt"))
    db.commit()
    db.execute(text("DROP TABLE notifications"))
    db.commit()

    growth = monitoring._table_growth(db, db_reachable=True)
    rows = {row["table"]: row for row in growth["tables"]}
    assert rows["notifications"]["rows"] is None
    assert rows["notifications"]["status"] == "unknown"
    assert "error_type" in rows["notifications"]
    # The tables AFTER the failure in iteration order still report — that is the
    # rollback doing its job, and it is the half the naive version loses.
    assert rows["machine_events"]["rows"] == 1
    assert rows["audit_logs"]["rows"] == 0
    assert growth["counted"] is True
    print("PASS an unreadable table degrades to unknown; the other nine still count")


def test_skipping_table_counts_actually_skips_the_scans():
    """Pins: an include_tables flag that hides the numbers but still pays for
    them. Each count is a sequential scan on PostgreSQL; the cheap call has to be
    cheap, not merely quiet."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())

    try:
        with _serving(engine):
            payload = monitoring.build_system_health(db, include_tables=False)
            skipped = [s for s in statements if "count(" in s]
            statements.clear()
            monitoring.build_system_health(db, include_tables=True)
            counted = [s for s in statements if "count(" in s]
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert skipped == [], f"skipping still issued {len(skipped)} COUNT statements"
    assert payload["tables"]["counted"] is False and payload["tables"]["tables"] == []
    # CONTROL: the default call really does pay for the counts, so the assertion
    # above is measuring the flag and not an absence of tables.
    assert len(counted) == len(monitoring.UNBOUNDED_TABLES)
    print(f"PASS include_tables=False issues 0 COUNTs; the default issues {len(counted)}")


# ── Build, Sentry, backup, verdict ────────────────────────────


def test_missing_build_sha_warns_rather_than_passing_quietly():
    """Pins: a null build sha rendering as fine. Serving traffic without knowing
    which commit is live means an incident cannot be tied to a deploy — a real
    hole in exactly the thing this endpoint reports on."""
    original = platform_routes.BUILD_SHA
    try:
        platform_routes.BUILD_SHA = None
        assert monitoring._build_info()["status"] == "warn"
        # CONTROL: with a sha present it is ok, and the sha is passed through.
        platform_routes.BUILD_SHA = "c2a7e9b"
        info = monitoring._build_info()
        assert info["status"] == "ok" and info["sha"] == "c2a7e9b"
    finally:
        platform_routes.BUILD_SHA = original
    print("PASS an unknown build sha warns; a known one passes")


def test_sentry_is_reported_as_a_boolean_and_the_dsn_never_appears():
    """Pins: putting the DSN in an ops payload. A Sentry DSN is a write
    credential, and this response is screenshotted and pasted into tickets — only
    'configured: true' may leave the process.

    The status when a DSN IS set is deliberately not asserted: it depends on
    whether the SDK is installed and whether init succeeded, and the module
    reports that difference on purpose."""
    secret = "https://abc123deadbeef@o999.ingest.sentry.io/4507-DO-NOT-LEAK"
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)

    with _env(SENTRY_DSN=secret), _serving(engine):
        payload = monitoring.build_system_health(db, include_tables=False)
    assert payload["sentry"]["configured"] is True
    serialised = json.dumps(payload)
    assert secret not in serialised and "deadbeef" not in serialised, "the DSN leaked into the payload"

    # CONTROL: with no DSN it is a warn, not silence — unhandled 500s would exist
    # only in Railway's rolling log buffer, which is neither retained nor
    # searchable after the fact.
    with _env(SENTRY_DSN=None):
        sentry = monitoring._sentry_health()
    assert sentry["configured"] is False and sentry["status"] == "warn"
    assert "not set" in (sentry["note"] or "")
    print("PASS Sentry reports a boolean; the DSN never leaves the process")


def test_backup_is_reported_unmonitored_and_never_invents_freshness():
    """Pins the temptation this module was most likely to give in to: inventing a
    'last backup' figure. Dumps run in GitHub Actions and land in artifacts; the
    app cannot see them and nothing in the database changes when one succeeds, so
    any timestamp here would be fiction — and fiction that is believed."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    with _serving(engine):
        backup = monitoring.build_system_health(db, include_tables=False)["backup"]

    assert backup["monitored"] is False
    assert backup["status"] == "unknown"
    assert backup["affects_overall_status"] is False
    assert "GitHub" in backup["where_to_check"]
    # No field may look like a freshness measure — not a timestamp, not an age.
    invented = [k for k in backup
                if k.startswith("last") or k.endswith("_at")
                or "age" in k or "timestamp" in k or "fresh" in k]
    assert invented == [], f"backup invented a freshness field: {invented}"
    print("PASS backup reports an honest gap instead of a fabricated timestamp")


def test_an_unmonitored_gap_neither_fails_nor_greens_the_verdict():
    """Pins the arithmetic behind the headline. 'unknown' must not turn a healthy
    deployment red (nobody would trust it again) and must not mask a real
    failure (the whole point of the headline)."""
    assert monitoring._worst("ok", "unknown") == "ok"
    assert monitoring._worst("critical", "unknown") == "critical"
    assert monitoring._worst("unknown") == "unknown", "claiming health from no evidence"
    # CONTROL: a known degradation still wins, so the above is not just the
    # function returning its first argument.
    assert monitoring._worst("ok", "warn") == "warn"
    assert monitoring._worst("warn", "critical", "ok") == "critical"
    print("PASS the verdict is the worst KNOWN status; gaps are neither pass nor fail")


def test_the_thresholds_that_produced_each_verdict_are_published():
    """Pins: a status with no visible boundary. 'warn' is unactionable if the
    reader cannot see what it was measured against without opening the source."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    with _serving(engine):
        thresholds = monitoring.build_system_health(db, include_tables=False)["thresholds"]

    for key in ("db_latency_warn_ms", "db_latency_critical_ms", "pool_warn_ratio",
                "pool_critical_ratio", "ws_warn_connections", "ws_critical_connections",
                "table_warn_rows", "table_critical_rows", "mqtt_quiet_after_minutes"):
        assert key in thresholds, f"{key} is applied but never published"
    assert thresholds["ws_warn_connections"] == monitoring.WS_WARN_CONNECTIONS
    assert thresholds["db_latency_warn_ms"] < thresholds["db_latency_critical_ms"]
    assert thresholds["table_warn_rows"] < thresholds["table_critical_rows"]
    print("PASS every applied threshold is published alongside the verdict")


def test_the_route_is_platform_owner_only():
    """Pins: a client Admin reading the whole deployment. The row counts are
    unscoped by design, so this endpoint would otherwise tell one customer how
    much data every other customer holds — the same reason /tenant-configs is
    gated."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    try:
        monitoring.system_health(tables=False, db=db, current_user={"sub": "gmats_admin",
                                                                   "role": "Admin",
                                                                   "tenant": "GMATS"})
        raise AssertionError("a client Admin was served the platform-wide view")
    except Exception as exc:  # HTTPException
        assert getattr(exc, "status_code", None) == 403, exc

    # CONTROL: the platform owner IS served, so the gate is a gate and not a wall.
    with _serving(engine):
        payload = monitoring.system_health(tables=False, db=db,
                                           current_user={"sub": "ashwin", "role": "Admin",
                                                         "tenant": "DEFAULT"})
    assert "status" in payload and "database" in payload
    print("PASS /system-health serves the platform owner and 403s a client Admin")


def test_a_token_with_no_tenant_claim_is_refused():
    """Pins the FAIL-CLOSED default, which a mutation caught: writing the guard
    as `get("tenant", DEFAULT_TENANT) != DEFAULT_TENANT` — the idiom used
    elsewhere in this codebase — reads a claimless token as the platform owner
    and hands it every tenant's row counts.

    Not reachable from a token AMP mints today: both create_access_token call
    sites in core_routes.py always set "tenant". It is reachable from one issued
    before the claim existed and still inside its expiry window, which is
    exactly the window in which nobody is looking. A guard on an endpoint that
    deliberately lifts tenant scoping should not be the one place that trusts an
    absent claim."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db = _fresh_session(engine)
    for label, user in (("no tenant key at all", {"sub": "legacy", "role": "Admin"}),
                        ("an explicit null tenant", {"sub": "legacy", "role": "Admin",
                                                     "tenant": None})):
        try:
            monitoring.system_health(tables=False, db=db, current_user=user)
            raise AssertionError("served the platform-wide view to %s" % label)
        except Exception as exc:  # HTTPException
            assert getattr(exc, "status_code", None) == 403, "%s: %r" % (label, exc)
    print("PASS a token carrying no tenant claim is refused, not read as DEFAULT")


if __name__ == "__main__":
    test_unreachable_database_is_critical_and_never_echoes_the_credentials()
    test_a_dead_database_still_returns_every_other_component()
    test_pool_stats_degrade_when_the_pool_cannot_report_itself()
    test_websocket_reports_the_per_tenant_split()
    test_websocket_status_escalates_at_the_documented_thresholds()
    test_mqtt_not_configured_is_not_reported_as_broken()
    test_mqtt_listener_thread_is_detected_while_it_runs()
    test_mqtt_event_age_ignores_simulator_written_events()
    test_table_counts_are_platform_wide_not_tenant_scoped()
    test_one_unreadable_table_does_not_cost_the_others_their_counts()
    test_skipping_table_counts_actually_skips_the_scans()
    test_missing_build_sha_warns_rather_than_passing_quietly()
    test_sentry_is_reported_as_a_boolean_and_the_dsn_never_appears()
    test_backup_is_reported_unmonitored_and_never_invents_freshness()
    test_an_unmonitored_gap_neither_fails_nor_greens_the_verdict()
    test_the_thresholds_that_produced_each_verdict_are_published()
    test_the_route_is_platform_owner_only()
    test_a_token_with_no_tenant_claim_is_refused()
    print("ALL MONITORING TESTS PASSED")
