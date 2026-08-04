"""Structured logging and request correlation.

THE GAP THIS CLOSES. The platform audit found the backend had no logging
framework at all: 146 bare ``print()`` calls, no levels, no structure, no way
to tie two lines together. Railway captures stdout and that was the entire
observability story. When a customer says "it broke around 14:32", there is
nothing to search — the prints from twelve concurrent requests, the MQTT
listener and the 45-second simulator tick are interleaved in one flat stream
with no way to tell which line belongs to which caller, or which tenant.

WHAT THIS PROVIDES.

  * ``configure_logging()`` — one JSON line per record on stdout, so Railway's
    log ingestion (and anything downstream of it) can filter on level, tenant
    or request id instead of grepping prose. Level comes from ``LOG_LEVEL``.
  * ``RequestContextMiddleware`` — assigns every HTTP request an id, honours an
    inbound ``x-request-id`` so a trace survives a proxy hop or a frontend
    retry, and echoes it back on the response. Support can then ask for the id
    off a failed call and pull exactly that request's lines.
  * ``redact()`` — applied to every structured field before it is written, so a
    handler that passes a whole request payload cannot spill a token into the
    log stream. Logs are the least protected copy of production data: they are
    read by anyone with dashboard access and retained long after the incident.

DEPENDENCIES: none. Standard-library ``logging``, ``json``, ``contextvars`` and
``uuid`` only — a logging layer must be the most boot-proof module in the
process, so it takes on nothing that can fail to install or import.

HOW TO USE IT.

    from logging_config import get_logger
    log = get_logger(__name__)
    log.info("work order completed", extra={"work_order_id": wo.id})

Put facts in ``extra=``, not in the message string. Only ``extra=`` fields are
structured (searchable) and only ``extra=`` fields go through redaction — the
formatter cannot safely guess which half of an already-interpolated sentence
was a secret. ``redact()`` is exported for callers who want to sanitise a
payload before doing anything else with it.

The request id and the tenant are read from context, never passed in — the
formatter attaches them when they exist and silently omits them when they do
not, so the simulator loop, the MQTT ingestion and one-shot operator scripts
log through the same path as a request handler without special-casing.
"""
import contextvars
import time
import json
import logging
import os
import string
import sys
import uuid
from datetime import datetime, timezone

# ── Request correlation ─────────────────────────────────────────────

# A contextvar rather than a thread-local: the app is async, one thread serves
# many concurrent requests, and pure-ASGI middleware shares the endpoint's task
# so the value set here is visible all the way down (same reasoning as the
# tenant contextvar in tenancy.py).
_request_id = contextvars.ContextVar("amp_request_id", default=None)
# The authenticated caller, bound by RequestContextMiddleware from the JWT.
# Deliberately the `sub` claim (a username), not a database id: a support report
# says "Priya cannot approve cycle counts", and grepping for the name she logs in
# with is the shortest path from that sentence to her failing request.
_user = contextvars.ContextVar("amp_user", default=None)

# The inbound header is attacker-controlled and ends up in every log line for
# that request, so it is treated as untrusted input: restricted alphabet and a
# hard length cap. JSON encoding already escapes newlines, so this is defence
# in depth against log forging rather than the only line of defence — but an
# unbounded id would also let a caller pad every one of their lines with 8KB
# of junk, which is a cheap way to make the log stream unusable.
_ID_ALPHABET = frozenset(string.ascii_letters + string.digits + "-._")
_MAX_ID_LENGTH = 128


def new_request_id():
    """A fresh correlation id. Hex uuid4 — no dashes, so it survives being
    pasted into a log filter, a URL or a support ticket unmangled."""
    return uuid.uuid4().hex


def sanitise_request_id(raw):
    """The usable part of a caller-supplied id, or None if nothing survives."""
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw.strip() if ch in _ID_ALPHABET)
    return cleaned[:_MAX_ID_LENGTH] or None


def get_request_id():
    """The current request's id, or None outside a request (background tasks)."""
    return _request_id.get()


def set_request_id(request_id):
    """Bind an id to the current context; returns a reset token."""
    return _request_id.set(request_id)


def reset_request_id(token):
    _request_id.reset(token)


_ACCESS_LOGGER = None


def _access_logger():
    """Cached: getLogger does a dict lookup plus a lock on every call, and this
    runs once per request on the hot path."""
    global _ACCESS_LOGGER
    if _ACCESS_LOGGER is None:
        _ACCESS_LOGGER = logging.getLogger("amp.access")
    return _ACCESS_LOGGER


def get_user():
    """The authenticated caller for this request, or None."""
    return _user.get()


def set_user(user):
    return _user.set(user)


def reset_user(token):
    _user.reset(token)


# ── Tenant lookup ───────────────────────────────────────────────────

# Resolved on first use rather than imported at module scope. tenancy pulls in
# models -> database, and database.py deliberately has NO DATABASE_URL fallback
# (a missing URL must fail loudly in production). Importing it here would mean a
# module whose whole job is to report failures could not itself be imported by a
# migration script, a test or a cron job that has no database configured.
_tenant_lookup = None


def _current_tenant():
    """The request's tenant, or None. Never raises: a formatter that can throw
    turns every log call into a second failure, usually while handling the
    first. Outside a request (simulator tick, MQTT ingest, seeding) there is no
    bound tenant and the field is simply absent."""
    global _tenant_lookup
    if _tenant_lookup is None:
        try:
            from tenancy import current_tenant
            _tenant_lookup = current_tenant
        except Exception:
            _tenant_lookup = lambda: None
    try:
        return _tenant_lookup()
    except Exception:
        return None


# ── Redaction ───────────────────────────────────────────────────────

REDACTED = "[REDACTED]"

# Matched as substrings of the normalised key (lowercased, punctuation
# stripped), so "API_KEY", "x-api-key" and "apiKey" all match one entry. The
# list errs towards over-redaction: a field wrongly hidden costs one debugging
# session, a token wrongly logged costs a credential rotation.
_SENSITIVE_KEY_PARTS = (
    "token", "password", "passwd", "secret", "authorization",
    "apikey", "credential", "cookie",
)

# Structures nest (a request body inside an error context inside an extra), but
# not deeply. The cap stops a cyclic or pathological object from turning one log
# call into unbounded recursion.
_MAX_REDACT_DEPTH = 6


def is_sensitive_key(key):
    """True if a field with this name must never reach the log stream."""
    normalised = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def redact(value, _depth=0):
    """Copy of ``value`` with every sensitively-named field replaced.

    Keys are judged, not values — there is no reliable way to recognise a
    secret by its shape, but the code that logs it always names it."""
    if _depth > _MAX_REDACT_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            k: REDACTED if is_sensitive_key(k) else redact(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in value]
    return value


# ── JSON formatter ──────────────────────────────────────────────────

# Everything LogRecord sets on itself, plus the two attributes Formatter adds.
# Derived from a real record rather than hardcoded so a new interpreter version
# adding a field (3.12 added taskName) does not start leaking it as an "extra".
_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None))
) | {"message", "asctime", "taskName"}

# The fields every line is guaranteed to carry. An extra named the same thing
# is kept but prefixed, so a caller cannot overwrite the level or the timestamp
# that an alert rule keys on — and cannot have their value silently dropped.
_CORE_FIELDS = ("timestamp", "level", "logger", "message", "request_id", "tenant", "user")


class JsonFormatter(logging.Formatter):
    """One record, one line of JSON, on stdout.

    Railway (and every log shipper after it) reads stdout line by line, so the
    output has to be single-line and self-describing. Structure is what buys
    the thing the audit asked for: "what broke for that customer at 14:32" is a
    filter on tenant plus a time range, not a grep through prose.
    """

    def format(self, record):
        try:
            return json.dumps(self._payload(record), default=str)
        except Exception as exc:
            # A log line that cannot be built must not propagate an exception
            # into the caller's error path — it is nearly always already
            # handling something worse. Emit a parseable line that says so.
            return json.dumps({
                "timestamp": self._timestamp(record),
                "level": "ERROR",
                "logger": "logging_config",
                "message": f"log record from {record.name!r} could not be serialised: {exc!r}",
            })

    def _timestamp(self, record):
        """ISO 8601 in UTC, millisecond precision, Z-suffixed. Explicitly UTC
        because the container's clock zone is not something to reason about at
        3am, and Z sorts and parses everywhere."""
        moment = datetime.fromtimestamp(record.created, timezone.utc)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _payload(self, record):
        payload = {
            "timestamp": self._timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Correlation fields are omitted rather than written as null when
        # absent: a background tick genuinely has no request, and an explicit
        # null invites a dashboard to group every tick under "unknown request".
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        tenant = _current_tenant()
        if tenant:
            payload["tenant"] = tenant
        user = get_user()
        if user:
            payload["user"] = user

        for key, value in record.__dict__.items():
            if key in _RECORD_FIELDS or key.startswith("_"):
                continue
            name = f"extra_{key}" if key in _CORE_FIELDS else key
            payload[name] = REDACTED if is_sensitive_key(key) else redact(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return payload


# ── Configuration ───────────────────────────────────────────────────

# Marks the handler this module owns. An attribute rather than an isinstance
# check so the handler stays a plain StreamHandler and detection survives a
# module reload in tests.
_AMP_HANDLER_FLAG = "_amp_json_handler"

_DEFAULT_LEVEL = "INFO"

# uvicorn installs its own plain-text handlers on these loggers when it starts,
# which is AFTER this module is first imported. Left alone they produce a
# second, unstructured copy of every access and error line — the exact mess
# this module exists to end. Clearing their handlers and letting them propagate
# routes them through the root JSON handler instead. Done on every
# configure_logging() call, which is why boot calls it again at startup: by
# then uvicorn has configured itself and there is something to take over.
_ADOPTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _resolve_level(level):
    """The effective level: explicit argument, else LOG_LEVEL, else INFO.
    An unrecognised value falls back to INFO rather than raising — a typo in a
    Railway variable must not stop the service booting, and silently logging
    nothing would be worse than logging slightly too much."""
    if level is None:
        level = os.environ.get("LOG_LEVEL") or _DEFAULT_LEVEL
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def _amp_handler():
    """This module's handler on the root logger, if it is already installed."""
    for handler in logging.getLogger().handlers:
        if getattr(handler, _AMP_HANDLER_FLAG, False):
            return handler
    return None


def configure_logging(level=None, stream=None):
    """Install JSON logging on the root logger. Idempotent.

    Idempotence is not a nicety here: boot code in this repo runs at import AND
    again in the startup event, and a second unguarded call would double every
    line — which quietly doubles the log bill and makes every "how many times
    did this happen" answer wrong.

    Handlers this module did not install are left in place. Sentry and any
    future log shipper attach their own; ripping them out to guarantee a single
    handler would silently disable error reporting.

    ``stream`` defaults to stdout (what Railway ingests) and exists so tests can
    capture output; passing it on a later call re-points the existing handler
    rather than adding a second one.
    """
    root = logging.getLogger()
    handler = _amp_handler()
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        setattr(handler, _AMP_HANDLER_FLAG, True)
        root.addHandler(handler)
    elif stream is not None:
        handler.setStream(stream)

    # Filtering happens once, on the root logger. The handler stays at NOTSET so
    # there is a single place to reason about what is emitted.
    root.setLevel(_resolve_level(level))
    handler.setLevel(logging.NOTSET)

    for name in _ADOPTED_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers = []
        adopted.propagate = True

    return handler


def get_logger(name=None):
    """A logger for a module: ``log = get_logger(__name__)``.

    Deliberately does not configure anything — configuration is a boot-time
    decision made once in main.py, and a module-level call that reconfigured
    logging as a side effect of being imported would make the level depend on
    import order.
    """
    return logging.getLogger(name)


# ── Middleware ──────────────────────────────────────────────────────

class RequestContextMiddleware:
    """Give every HTTP request an id, and give it back to the caller.

    Pure ASGI, matching tenancy.TenantScopeMiddleware and http_security — not
    BaseHTTPMiddleware, which runs the request in a separate task (contextvars
    set there would not reach the endpoint) and buffers request bodies.

    Wrapping ``send`` rather than decorating a route means the id is echoed on
    responses that never reach a route: the plan gate's 403, the throttle's 429,
    a 404, a validation error. Those are precisely the responses a customer
    reports, so those are the ones that most need an id to quote.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        inbound = None
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                inbound = sanitise_request_id(value.decode("latin-1"))
                break
        request_id = inbound or new_request_id()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        # Bind the caller from the JWT so every line of this request carries it.
        # decode_token_optional never raises: an unauthenticated or expired
        # request still gets logged, just without a user — which is exactly the
        # request you want to see when someone reports being logged out.
        user = None
        try:
            for key, value in scope.get("headers") or []:
                if key == b"authorization":
                    raw = value.decode("latin-1")
                    if raw.lower().startswith("bearer "):
                        from auth import decode_token_optional
                        claims = decode_token_optional(raw[7:].strip())
                        if claims:
                            user = claims.get("sub")
                    break
        except Exception:
            # Never let logging enrichment break a request. A missing user field
            # costs a little context; a 500 from the logger costs the request.
            user = None

        token = set_request_id(request_id)
        user_token = set_user(user)
        started = time.perf_counter()
        status_holder = {"code": None}

        async def send_timed(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send_with_id(message)

        try:
            await self.app(scope, receive, send_timed)
        finally:
            # One line per request with the timing. This is the access log: it
            # replaces uvicorn's plain-text one (which carries no request id, no
            # tenant and no user) and it is what makes "which endpoint got slow
            # last Tuesday" answerable at all.
            try:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                status = status_holder["code"]
                _access_logger().info(
                    "%s %s %s %sms" % (scope.get("method", "?"), scope.get("path", "?"),
                                       status if status is not None else "-", elapsed_ms),
                    extra={"http_method": scope.get("method"),
                           "http_path": scope.get("path"),
                           "http_status": status,
                           "duration_ms": elapsed_ms},
                )
            except Exception:
                pass
            reset_user(user_token)
            # Reset even on failure: the ASGI server reuses the context for the
            # next request on that task, and a leaked id would attribute one
            # customer's log lines to another customer's request.
            reset_request_id(token)
