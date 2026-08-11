"""The schema-readiness gate (ADR-0018).

#513 was a total authentication outage caused by an application that was happy
to start against a database it could not use. This is the code that stops that,
and these are its behaviours:

  * a database BEHIND the build's head refuses traffic, in every environment;
  * an UNMANAGED database is fine on a laptop and refused in production;
  * a database AHEAD is allowed, so a rollback is still possible;
  * /health stops claiming "ok" when the schema is wrong — it said "ok" for a
    whole day during the outage;
  * /readiness is the probe that gates traffic, and it names both revisions.

The end-to-end proof on real PostgreSQL — old database, refusal, migration,
recovery, login — is verify_pg_deploy.py. This file pins the logic and the wiring
so the standard suite catches a regression without needing a database server.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_schema_guard.py
"""
import asyncio
import json
import os
import sys
import tempfile

from sqlalchemy import create_engine, text

import schema_guard

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


class _Env:
    """Set/clear production markers around a block, restoring exactly."""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _db(tag, populate=True, revision="__head__"):
    """A throwaway SQLite database in a chosen schema state."""
    path = os.path.join(tempfile.gettempdir(), f"amp_guard_{tag}.db")
    if os.path.exists(path):
        os.remove(path)
    engine = create_engine(f"sqlite:///{path}")
    if populate:
        import models  # noqa: F401
        from database import Base
        Base.metadata.create_all(bind=engine)
    if revision is not None:
        rev = schema_guard.head_revision() if revision == "__head__" else revision
        with engine.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            c.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": rev})
    return engine


# =====================================================================
def test_the_states():
    print("=" * 74)
    print("1. WHAT EACH SCHEMA STATE MEANS")
    print("=" * 74)
    head = schema_guard.head_revision()
    check("this build declares a head revision", bool(head), str(head))

    at_head = _db("head")
    s = schema_guard.check(at_head)
    check("a database AT head is ok", s["ok"] and s["state"] == "ok", str(s))
    check("...and reports the revision it found", s["current"] == head, str(s))

    # THE #513 STATE: a migration exists that the database has not run.
    behind = _db("behind", revision="0001_baseline")
    s = schema_guard.check(behind)
    check("a database BEHIND head is REFUSED", s["ok"] is False and s["state"] == "behind", str(s))
    check("...and names both revisions",
          s["current"] == "0001_baseline" and s["expected"] == head, str(s))
    check("...and says what to run", "migrate.py" in s["reason"], s["reason"])

    with _Env(PRODUCTION="1"):
        s = schema_guard.check(behind)
    check("...and is refused in production too — 'behind' is never tolerated",
          s["ok"] is False and s["state"] == "behind", str(s))

    # A revision from a build newer than this one: the rollback shape.
    ahead = _db("ahead", revision="0099_from_the_future")
    s = schema_guard.check(ahead)
    check("a database AHEAD is ALLOWED, so a rollback is possible", s["ok"], str(s))
    check("...and is reported as 'ahead', never as ok", s["state"] == "ahead", str(s))


def test_unmanaged_is_dev_only():
    print()
    print("=" * 74)
    print("2. AN UNMANAGED DATABASE: FINE ON A LAPTOP, REFUSED IN PRODUCTION")
    print("=" * 74)
    unmanaged = _db("unmanaged", revision=None)

    with _Env(PRODUCTION=None, RAILWAY_ENVIRONMENT=None):
        s = schema_guard.check(unmanaged)
    check("in DEV an unmanaged database is allowed", s["ok"] and s["state"] == "unmanaged", str(s))
    check("...because create_all built it in this same process",
          "create_all" in s["reason"], s["reason"])

    with _Env(PRODUCTION="1"):
        s = schema_guard.check(unmanaged)
    check("in PRODUCTION the same database is REFUSED", s["ok"] is False, str(s))
    check("...and the reason names the incident", "#513" in s["reason"], s["reason"])

    # Railway sets this on every deploy; it must count as production on its own.
    with _Env(PRODUCTION=None, RAILWAY_ENVIRONMENT="production"):
        s = schema_guard.check(unmanaged)
    check("RAILWAY_ENVIRONMENT alone is enough to mean production", s["ok"] is False, str(s))

    empty = _db("empty", populate=False, revision=None)
    with _Env(PRODUCTION=None, RAILWAY_ENVIRONMENT=None):
        s = schema_guard.check(empty)
    check("an EMPTY database is 'empty', not 'unmanaged'", s["state"] == "empty", str(s))


def test_the_cache_lets_a_refusing_instance_recover():
    print()
    print("=" * 74)
    print("3. A REFUSING INSTANCE RECOVERS WITHOUT A RESTART")
    print("=" * 74)
    head = schema_guard.head_revision()
    engine = _db("recover", revision="0001_baseline")

    schema_guard.reset()
    s = schema_guard.evaluate(engine)
    check("starts out refusing", s["ok"] is False, str(s))

    # An operator runs `python migrate.py` against the refusing instance.
    with engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": head})

    s = schema_guard.evaluate(engine)
    check("becomes ready on the next probe, with no restart", s["ok"], str(s))

    # ...and then stops re-querying, so the probe path is not a query forever.
    with engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '0001_baseline'"))
    s = schema_guard.evaluate(engine)
    check("once good, the verdict is cached", s["ok"], str(s))
    s = schema_guard.evaluate(engine, force=True)
    check("CONTROL: force=True re-reads and sees the change", s["ok"] is False, str(s))
    schema_guard.reset()


def _drive(engine, path, method="GET"):
    """Send one request through SchemaGuardMiddleware; return (status, body)."""
    sent = {}

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode(): v.decode()
                               for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent["body"] = message.get("body", b"")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = schema_guard.SchemaGuardMiddleware(inner_app, engine)
    asyncio.run(mw({"type": "http", "path": path, "method": method}, receive, send))
    return sent.get("status"), sent.get("body", b""), sent.get("headers", {})


def test_the_middleware_refuses_application_routes():
    print()
    print("=" * 74)
    print("4. WHILE THE SCHEMA IS WRONG, NOTHING IS SERVED")
    print("=" * 74)
    behind = _db("mw_behind", revision="0001_baseline")
    schema_guard.reset()

    status, body, headers = _drive(behind, "/login")
    check("an application route is refused with 503", status == 503, str(status))
    payload = json.loads(body or b"{}")
    check("...and the body explains why, with both revisions",
          payload.get("schema", {}).get("state") == "behind"
          and payload["schema"]["expected"] and payload["schema"]["current"],
          str(payload))
    # A refusal that a proxy or a browser caches would outlive the migration
    # that fixes it, and the instance would keep looking broken after it was not.
    check("...and is explicitly not cacheable",
          headers.get("cache-control") == "no-store", str(headers))

    for path in ("/health", "/readiness", "/docs", "/openapi.json"):
        status, _, _ = _drive(behind, path)
        check(f"{path} stays reachable for diagnosis", status == 200, str(status))

    # The gate must not fire when the schema is fine, or every request pays for
    # a guard that has nothing to guard.
    schema_guard.reset()
    at_head = _db("mw_ok")
    status, body, _ = _drive(at_head, "/login")
    check("CONTROL: with the schema at head the route is served normally",
          status == 200 and b'"ok":true' in body, str(status))
    schema_guard.reset()


def test_health_and_readiness_say_different_things():
    print()
    print("=" * 74)
    print("5. LIVENESS AND READINESS ARE DIFFERENT QUESTIONS")
    print("=" * 74)
    import platform_routes

    behind = _db("probe_behind", revision="0001_baseline")
    saved = platform_routes.engine
    try:
        platform_routes.engine = behind
        schema_guard.reset()

        health = platform_routes.health()
        body = json.loads(health.body)
        # THE #513 BEHAVIOUR THIS REPLACES: 200 with "status": "ok", for a day,
        # while nobody could log in.
        check("/health no longer claims 'ok' when the schema is wrong",
              body["status"] == "degraded", str(body))
        check("...and names the schema state", body["schema"] == "behind", str(body))
        check("...but its STATUS CODE stays about liveness (200), so a restart "
              "policy does not loop on a problem a reboot cannot fix",
              health.status_code == 200, str(health.status_code))

        ready = platform_routes.readiness()
        rbody = json.loads(ready.body)
        check("/readiness returns 503", ready.status_code == 503, str(ready.status_code))
        check("...and says it is not ready", rbody["ready"] is False, str(rbody))
        check("...and carries both revisions for the operator",
              rbody["schema"]["expected_revision"] and rbody["schema"]["current_revision"],
              str(rbody))
        # Operational metadata only. A revision is not a secret; a connection
        # string is, and must never appear here.
        blob = json.dumps(rbody).lower()
        for leak in ("password", "postgresql://", "sqlite://", "@localhost", "secret"):
            check(f"/readiness does not leak {leak!r}", leak not in blob)

        # And when it is healthy, both agree.
        schema_guard.reset()
        platform_routes.engine = _db("probe_ok")
        health = platform_routes.health()
        ready = platform_routes.readiness()
        check("CONTROL: at head, /health is ok and /readiness is 200",
              json.loads(health.body)["status"] == "ok" and ready.status_code == 200,
              f"{health.body} {ready.status_code}")
    finally:
        platform_routes.engine = saved
        schema_guard.reset()


def run_all():
    test_the_states()
    test_unmanaged_is_dev_only()
    test_the_cache_lets_a_refusing_instance_recover()
    test_the_middleware_refuses_application_routes()
    test_health_and_readiness_say_different_things()


def test_schema_guard():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("AMP REFUSES TO SERVE A SCHEMA IT WAS NOT WRITTEN FOR")
