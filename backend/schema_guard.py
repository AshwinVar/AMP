"""Refuse to serve against a database this build was not written for (ADR-0018).

WHY THIS EXISTS
---------------
On 2026-08-09 every login on production returned 500 for a day. `models.User`
declared `is_active`; the production `users` table did not have it. The Alembic
migration that adds it had been written, reviewed and merged — and never run,
because nothing in the deploy ran Alembic. `create_all()` could not help: it
creates missing TABLES and has never altered an existing one.

The deploy now runs migrations (railway.toml `preDeployCommand`), which fixes
the cause. This module is the second line, and it exists because a deploy step
is a piece of configuration somebody can change, move or lose. It converts

    "the schema is old, so the product is subtly broken and nobody can tell"

into

    "the schema is old, so this instance refuses traffic and says which
     revision it wanted."

THE STATES, AND WHY TWO OF THEM DEPEND ON WHERE YOU ARE
-------------------------------------------------------
    ok          the database revision is exactly this build's head.  ALLOW
    behind      an ancestor of head — migrations are PENDING.        REFUSE
    ahead       the database carries a revision this build has never
                heard of, i.e. the APPLICATION is older than the
                schema.                                              ALLOW
    unmanaged   no alembic_version: Alembic does not own this
                database.                          ALLOW in dev, REFUSE in prod
    empty       no tables at all.                   ALLOW in dev, REFUSE in prod
    unreachable the database could not be read.                      REFUSE

`unmanaged` is the interesting one, and it is why this is not a one-line check.

    In DEVELOPMENT AND CI it is the normal state. Every test suite and every
    laptop builds its schema with `create_all()` in the same process that is
    about to use it, so the schema is at model shape BY CONSTRUCTION and there
    is nothing for a revision to disagree with. Refusing there would break 186
    suites to protect against a drift that cannot occur.

    IN PRODUCTION it is a defect, and it is the exact state production was in
    during #513. A database that has accumulated over a year is NOT at model
    shape: create_all() only ever created the tables that were missing, so every
    column added to an existing table since is simply absent. Alembic must own
    production, so an unmanaged production database refuses traffic and says so.

Production is detected the same way auth.py detects it for the JWT signing key —
RAILWAY_ENVIRONMENT, which Railway sets on every deploy, or an explicit
PRODUCTION=1. The asymmetry is deliberate and is the same one that module
argues: permissive where a mistake costs a developer a minute, fail-closed where
it costs users a day.

WHY 'ahead' IS ALLOWED EVERYWHERE
---------------------------------
Rolling the application back is the primary recovery lever when a release goes
wrong, and refusing to start against a newer schema would take that lever away
exactly when it is needed. It is safe only because migrations are required to be
backwards compatible (expand / migrate / contract — see docs/MIGRATIONS.md); the
allowance is what makes that rule load-bearing rather than advisory. It is
logged as a warning every time, because running an old build against a new
schema should never be silent.

`ahead` is deliberately not a refusal, and the asymmetry is the point. Rolling
the application back is the primary recovery lever when a release goes wrong,
and refusing to start against a newer schema would take that lever away exactly
when it is needed. It is safe only because migrations are required to be
backwards compatible (expand / migrate / contract — see docs/MIGRATIONS.md); the
allowance is what makes that rule load-bearing rather than advisory. It is
logged as a warning every time, because running an old build against a new
schema should never be silent.

WHAT THIS IS NOT
----------------
It is not a column-by-column comparison of models against the live schema. That
sounds stricter and is worse: it re-implements Alembic's autogenerate at boot,
on every instance, and disagrees with it at the margins (server defaults, index
naming, type affinity). The revision IS the contract — CI proves the revision
means what the models say (`.github/workflows/ci.yml`, migrations job), and this
proves production is at it.
"""
import logging
import os

log = logging.getLogger(__name__)

# Paths that must keep answering while the schema is incompatible. Everything
# else is refused, because everything else is ORM-backed and would either 500 or
# — worse — answer with a half-truth.
#
# `/health` is here on purpose: an operator diagnosing a refused deployment needs
# the liveness probe to keep working, and Railway's restart policy keys off the
# process, not off readiness.
ALWAYS_AVAILABLE = (
    "/health",
    "/readiness",
    "/docs",
    "/redoc",
    "/openapi.json",
)

_state = None


def is_production():
    """Detected exactly as auth.py detects it for the JWT signing key.

    RAILWAY_ENVIRONMENT is set by Railway on every deploy; PRODUCTION=1 is the
    explicit escape hatch for anywhere else. Read at call time rather than at
    import so a test can set it around a single check.
    """
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PRODUCTION"))


def head_revision():
    """The revision THIS BUILD requires, read from its own migration scripts."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _known_revisions():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    return {s.revision for s in ScriptDirectory.from_config(cfg).walk_revisions()}


def database_revision(engine):
    """The revision the DATABASE says it is at, or None if never stamped."""
    from alembic.migration import MigrationContext
    # Alembic logs "Context impl PostgresqlImpl" at INFO every time a migration
    # context is configured. Harmless once; this runs on every /readiness probe
    # while an instance is refusing, which is precisely when the log needs to be
    # readable. Quieted here rather than in logging_config so a real `alembic
    # upgrade` on the command line keeps its normal output.
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _has_application_tables(engine):
    from sqlalchemy import inspect
    names = set(inspect(engine).get_table_names())
    # Two tables that have existed since the first commit. Checked by name
    # rather than by counting, so a half-created database is not mistaken for a
    # populated one.
    return "users" in names and "machines" in names


def check(engine):
    """Evaluate schema compatibility. Never raises — a failure to evaluate is
    itself an incompatible state, not an exception for a caller to forget."""
    try:
        expected = head_revision()
    except Exception as e:                                   # pragma: no cover
        return {"ok": False, "state": "unknown", "expected": None,
                "current": None,
                "reason": f"could not read this build's migration scripts: {e}"}

    try:
        current = database_revision(engine)
    except Exception as e:
        # A database that cannot be read is not a schema problem, but it is
        # certainly not readiness. Reported distinctly so the two are told apart.
        return {"ok": False, "state": "unreachable", "expected": expected,
                "current": None,
                "reason": f"could not read the database revision: {type(e).__name__}"}

    if current == expected:
        return {"ok": True, "state": "ok", "expected": expected,
                "current": current, "reason": "schema is at the expected revision"}

    if current is None:
        populated = _has_application_tables(engine)
        state = "unmanaged" if populated else "empty"
        if is_production():
            return {"ok": False, "state": state, "expected": expected,
                    "current": None,
                    "reason": ("this is a production environment and the database "
                               "is not under Alembic management (no "
                               "alembic_version table). Alembic must own the "
                               "production schema — an unmanaged database is the "
                               "state that produced #513. "
                               "Run: python migrate.py")}
        return {"ok": True, "state": state, "expected": expected,
                "current": None,
                "reason": ("not an Alembic-managed database; create_all() owns "
                           "this schema, which is the normal arrangement for a "
                           "test or development database")}

    if current in _known_revisions():
        # A revision this build knows, but not its head: migrations are pending.
        # THIS IS THE #513 STATE.
        return {"ok": False, "state": "behind", "expected": expected,
                "current": current,
                "reason": (f"the database is at {current}, this build requires "
                           f"{expected} — migrations are pending. "
                           "Run: python migrate.py")}

    # A revision from the future: the application is older than the schema.
    # Allowed so a rollback is possible; see the module docstring.
    return {"ok": True, "state": "ahead", "expected": expected,
            "current": current,
            "reason": (f"the database is at {current}, which this build does not "
                       f"know (it requires {expected}). The application is OLDER "
                       "than the schema — expected during a rollback, and safe "
                       "only while migrations stay backwards compatible.")}


def evaluate(engine, force=False):
    """Cached compatibility state.

    Re-evaluated on every call while incompatible, and cached once good. That
    ordering is deliberate: an operator who runs `python migrate.py` against a
    refusing instance should see it start serving on the next readiness probe,
    without a restart — recovery should not require a deploy. Once compatible,
    the state is fixed for the life of the process, because a schema does not
    un-migrate underneath a running app and re-checking would put a query on the
    probe path forever.
    """
    global _state
    if _state is not None and _state.get("ok") and not force:
        return _state
    _state = check(engine)
    return _state


def reset():
    """Drop the cached state. For tests, and for nothing else."""
    global _state
    _state = None


def report(state):
    """One structured log line. Carries revisions and a reason — never a URL,
    a credential, or anything else that should not be in a log aggregator."""
    if state["ok"] and state["state"] == "ok":
        log.info("[SCHEMA] %s (revision %s)", state["state"], state["current"])
    elif state["ok"]:
        log.warning("[SCHEMA] %s — %s", state["state"], state["reason"])
    else:
        log.error("[SCHEMA] REFUSING TRAFFIC: %s — %s",
                  state["state"], state["reason"])


class SchemaGuardMiddleware:
    """503 every application route while the schema is incompatible.

    Pure ASGI rather than BaseHTTPMiddleware, matching TenantScopeMiddleware:
    BaseHTTPMiddleware buffers the request body and has a documented deadlock on
    it, and this sits in front of every request including uploads.

    It returns responses itself, so it must be added BEFORE CORSMiddleware —
    Starlette runs the last-added middleware first, and a 503 emitted outside the
    CORS wrapper reaches the browser as an opaque "Failed to fetch" instead of a
    readable status (the same trap documented on PlanGateMiddleware).
    """

    def __init__(self, app, engine):
        self.app = app
        self.engine = engine

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in ALWAYS_AVAILABLE or path == "/":
            await self.app(scope, receive, send)
            return

        state = evaluate(self.engine)
        if state["ok"]:
            await self.app(scope, receive, send)
            return

        import json
        body = json.dumps({
            "detail": "AMP is not ready: the database schema does not match this "
                      "build. No request is served until migrations are applied.",
            "schema": {k: state[k] for k in ("state", "expected", "current", "reason")},
        }).encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                # Not a cacheable answer: it stops being true the
                                # moment somebody runs the migration.
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
