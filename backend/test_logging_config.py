"""Structured logging and request correlation (logging_config.py).

THE GAP THESE PIN. The platform audit found 146 bare ``print()`` calls and no
logging configuration whatsoever — no levels, no structure, no correlation.
Nobody could answer "what broke for that customer at 14:32?", because the
output of twelve concurrent requests, the MQTT listener and the 45-second
simulator tick arrive interleaved on one stdout stream with nothing tying a
line to the caller, the tenant or the request that produced it.

So these tests do not check that logging "works" in the abstract. Each one pins
a specific way the old situation failed, and the ones where a naive fix would
also pass carry a CONTROL case that separates the two:

  * output is machine-parseable JSON, not prose;
  * the level actually filters (CONTROL: the suppressed line is one that would
    otherwise have been emitted);
  * an id is generated when absent (CONTROL: two requests get different ids, so
    a hardcoded constant fails) and honoured when supplied;
  * the response carries the id back, which is what makes it quotable;
  * redaction hides secrets (CONTROL: an ordinary field is untouched, so a
    formatter that redacted everything fails);
  * logging from a background tick with no request context does not raise —
    the simulator loop must not be crashed by its own log call;
  * configure_logging() twice does not double every line (CONTROL: the root
    handler count, since the fix could otherwise be "it looks fine locally").

Run:  python backend/test_logging_config.py     (exit 0 = pass)
"""
import asyncio
import io
import json
import logging
import os

import logging_config


# ── Helpers ─────────────────────────────────────────────────────────

def _capture(level="INFO"):
    """Point the AMP handler at an in-memory stream and return the buffer."""
    buf = io.StringIO()
    logging_config.configure_logging(level=level, stream=buf)
    return buf


def _lines(buf):
    """Every captured record, parsed. Fails loudly on unparseable output —
    a line a log platform cannot ingest is the failure mode under test."""
    out = []
    for raw in buf.getvalue().splitlines():
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"log line is not valid JSON: {raw!r} ({exc})")
    return out


def _scope(path="/machines", headers=None):
    return {"type": "http", "path": path, "scheme": "http",
            "headers": headers or [], "client": ("1.2.3.4", 5000)}


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def _logging_app(scope, receive, send):
    """An 'endpoint' that logs, so the correlation can be observed end to end."""
    logging_config.get_logger("amp.test.endpoint").info("handled")
    await _ok_app(scope, receive, send)


def _drive(mw, scope):
    """Run one request through a middleware, returning (status, headers)."""
    captured = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(mw(scope, receive, send))
    return captured


# ── Structure ───────────────────────────────────────────────────────

def test_output_is_parseable_json_with_the_expected_keys():
    """A print() is unsearchable. Every line must be a JSON object carrying the
    fields an incident is triaged on: when, how bad, from where, and what."""
    buf = _capture()
    logging_config.get_logger("amp.test.shape").warning("machine went down")

    records = _lines(buf)
    assert len(records) == 1, records
    line = records[0]
    assert line["level"] == "WARNING", line
    assert line["logger"] == "amp.test.shape", line
    assert line["message"] == "machine went down", line
    # UTC, millisecond precision, Z-suffixed so it sorts and parses anywhere.
    assert line["timestamp"].endswith("Z"), line
    assert line["timestamp"][4] == "-" and "T" in line["timestamp"], line
    print("PASS log records are valid JSON with timestamp/level/logger/message")


def test_extra_fields_are_structured_not_stringified():
    """The point of structure is filtering on a field. A message that merely
    mentions the machine id cannot be filtered on; a field can."""
    buf = _capture()
    logging_config.get_logger("amp.test.extra").info(
        "downtime recorded", extra={"machine_id": "CNC-04", "minutes": 12})

    line = _lines(buf)[0]
    assert line["machine_id"] == "CNC-04", line
    assert line["minutes"] == 12, line          # stays a number, not "12"
    print("PASS extra= fields are emitted as typed JSON fields")


def test_an_extra_cannot_overwrite_a_core_field():
    """An alert rule keys on 'level'. A caller passing extra={'level': ...}
    must not be able to relabel their own error as something benign — and must
    not have their value silently dropped either."""
    buf = _capture()
    logging_config.get_logger("amp.test.collide").error("boom", extra={"level": "DEBUG"})

    line = _lines(buf)[0]
    assert line["level"] == "ERROR", line
    assert line["extra_level"] == "DEBUG", line
    print("PASS a colliding extra is preserved under a prefix, not merged over the core field")


def test_a_non_serialisable_value_still_produces_a_line():
    """Logging is what you have left when things are already going wrong. A
    field the JSON encoder cannot handle must degrade to its string form, never
    raise into the caller's error path."""
    class Opaque:
        def __repr__(self):
            return "<Opaque machine handle>"

    buf = _capture()
    logging_config.get_logger("amp.test.opaque").info("tick", extra={"handle": Opaque()})

    line = _lines(buf)[0]
    assert "Opaque" in line["handle"], line
    print("PASS an unserialisable field degrades to a string instead of raising")


def test_exceptions_carry_their_traceback():
    """The old prints lost the stack entirely — `print(f"[SIM TICK ERROR] {e}")`
    gives the message and nothing about where it came from."""
    buf = _capture()
    log = logging_config.get_logger("amp.test.exc")
    try:
        raise ValueError("bill of materials is empty")
    except ValueError:
        log.exception("tick failed")

    line = _lines(buf)[0]
    assert "ValueError: bill of materials is empty" in line["exception"], line
    assert "Traceback" in line["exception"], line
    print("PASS exceptions are logged with their traceback")


# ── Levels ──────────────────────────────────────────────────────────

def test_the_level_filters():
    """With no levels at all, the only choices were 'print everything' and
    'print nothing'. CONTROL: the same INFO call IS emitted at INFO, so a
    formatter that dropped every record would fail this test rather than pass
    the first half of it."""
    quiet = _capture(level="WARNING")
    log = logging_config.get_logger("amp.test.level")
    log.debug("debug detail")
    log.info("routine progress")
    log.warning("something is wrong")

    messages = [r["message"] for r in _lines(quiet)]
    assert messages == ["something is wrong"], messages

    loud = _capture(level="INFO")
    logging_config.get_logger("amp.test.level").info("routine progress")
    assert [r["message"] for r in _lines(loud)] == ["routine progress"]
    print("PASS the configured level filters, and the same record passes at a lower level")


def test_the_level_comes_from_the_environment():
    """Turning up detail during an incident must not need a code change; and a
    typo in the Railway variable must not stop the service booting."""
    original = os.environ.get("LOG_LEVEL")
    try:
        os.environ["LOG_LEVEL"] = "DEBUG"
        buf = _capture(level=None)
        logging_config.get_logger("amp.test.env").debug("verbose")
        assert [r["message"] for r in _lines(buf)] == ["verbose"]

        os.environ["LOG_LEVEL"] = "NOT_A_LEVEL"
        buf = _capture(level=None)
        assert logging.getLogger().level == logging.INFO, "a bad LOG_LEVEL must fall back to INFO"
        logging_config.get_logger("amp.test.env").info("still logging")
        assert [r["message"] for r in _lines(buf)] == ["still logging"]
    finally:
        if original is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = original
    print("PASS LOG_LEVEL is honoured, and an invalid value falls back to INFO")


# ── Request correlation ─────────────────────────────────────────────

def test_an_inbound_request_id_is_honoured():
    """A trace that starts at the proxy, the frontend or a retried call must
    stay one trace — otherwise correlation stops at every hop."""
    buf = _capture()
    result = _drive(logging_config.RequestContextMiddleware(_logging_app),
                    _scope(headers=[(b"x-request-id", b"trace-from-the-edge")]))

    assert result["headers"]["x-request-id"] == "trace-from-the-edge", result
    line = _lines(buf)[0]
    assert line["request_id"] == "trace-from-the-edge", line
    print("PASS an inbound x-request-id propagates into the logs and back out")


def test_an_id_is_generated_when_none_is_supplied():
    """Most callers send nothing, and those requests are exactly the ones that
    need an id. CONTROL: two requests must get DIFFERENT ids — a constant would
    satisfy 'an id is present' while correlating nothing."""
    buf = _capture()
    first = _drive(logging_config.RequestContextMiddleware(_logging_app), _scope())
    second = _drive(logging_config.RequestContextMiddleware(_logging_app), _scope())

    id_one = first["headers"]["x-request-id"]
    id_two = second["headers"]["x-request-id"]
    assert id_one and id_two, (first, second)
    assert id_one != id_two, "each request must get its own id"

    # Every request now also emits one access line (amp.access), so filter to
    # the application's own lines rather than counting everything on the stream.
    logged = [r["request_id"] for r in _lines(buf) if r.get("logger") != "amp.access"]
    assert logged == [id_one, id_two], logged
    print("PASS an id is generated per request and matches the one returned")


def test_every_request_emits_one_access_line_with_its_timing():
    """THE ACCESS LOG. uvicorn's own access line carries no request id, no tenant
    and no user, which makes "which endpoint got slow last Tuesday" unanswerable.
    One structured line per request, with method, path, status and duration, is
    what makes it answerable.

    CONTROL: the line must carry a REAL status and a numeric duration. A version
    that logged the request but not its outcome would satisfy "an access line
    exists" while being useless for the question it exists to answer.
    """
    buf = _capture()
    _drive(logging_config.RequestContextMiddleware(_logging_app), _scope(path="/machines"))

    access = [r for r in _lines(buf) if r.get("logger") == "amp.access"]
    assert len(access) == 1, access
    line = access[0]
    assert line["http_path"] == "/machines", line
    assert line["http_status"] == 200, line
    assert isinstance(line["duration_ms"], (int, float)), line
    assert line["request_id"], "the access line must carry the correlation id"
    print("PASS each request emits one access line with method, path, status and duration")


def test_the_access_line_records_a_failed_response_too():
    """A 500 or a 403 is the request you most want timing for. The line is
    emitted from a finally block precisely so a failing response still gets one."""
    async def failing_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    buf = _capture()
    _drive(logging_config.RequestContextMiddleware(failing_app), _scope(path="/health"))
    access = [r for r in _lines(buf) if r.get("logger") == "amp.access"]
    assert len(access) == 1 and access[0]["http_status"] == 503, access
    print("PASS a failing response still produces an access line")


def test_the_id_is_echoed_on_responses_that_never_reach_a_route():
    """The responses a customer actually reports are the plan gate's 403, the
    throttle's 429 and plain 404s. Those must be quotable too, which is why the
    middleware wraps send() instead of decorating a route."""
    async def rejecting_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"denied"})

    result = _drive(logging_config.RequestContextMiddleware(rejecting_app), _scope(path="/nope"))
    assert result["status"] == 403
    assert result["headers"].get("x-request-id"), result
    print("PASS the request id is echoed on non-route responses too")


def test_a_hostile_inbound_id_is_sanitised():
    """The header is attacker-controlled and lands in every line for that
    request. An unbounded one pads the log stream; a newline is the classic
    log-forging attempt."""
    hostile = b'evil\nlevel="CRITICAL" forged, ' + b"x" * 400
    result = _drive(logging_config.RequestContextMiddleware(_ok_app),
                    _scope(headers=[(b"x-request-id", hostile)]))

    returned = result["headers"]["x-request-id"]
    assert "\n" not in returned and " " not in returned and '"' not in returned, returned
    assert len(returned) <= 128, len(returned)

    # A header with nothing usable in it falls back to a generated id rather
    # than an empty one, so the request is still traceable.
    junk = _drive(logging_config.RequestContextMiddleware(_ok_app),
                  _scope(headers=[(b"x-request-id", b"!!! ???")]))
    assert junk["headers"]["x-request-id"], junk
    assert junk["headers"]["x-request-id"] != returned
    print("PASS a hostile inbound id is sanitised and an unusable one is replaced")


def test_the_id_survives_a_sync_endpoint_on_the_threadpool():
    """Almost every endpoint in this repo is `def`, not `async def`, so Starlette
    dispatches it to a worker thread. A thread-local would lose the id at exactly
    that boundary — correlating nothing for the majority of the API while still
    passing every other test here. A contextvar survives because the dispatch
    copies the context into the worker, and this pins that."""
    from starlette.concurrency import run_in_threadpool

    seen = {}
    buf = _capture()

    def sync_handler():
        logging_config.get_logger("amp.test.threadpool").info("ran off the loop")
        return logging_config.get_request_id()

    async def threadpool_app(scope, receive, send):
        seen["id"] = await run_in_threadpool(sync_handler)
        await _ok_app(scope, receive, send)

    result = _drive(logging_config.RequestContextMiddleware(threadpool_app), _scope())
    returned = result["headers"]["x-request-id"]
    assert seen["id"] == returned, (seen, returned)
    assert _lines(buf)[0]["request_id"] == returned, _lines(buf)
    print("PASS the request id reaches a sync endpoint running on the threadpool")


def test_the_id_does_not_leak_between_requests():
    """ASGI reuses the context for the next request on that task. A leaked id
    would file one customer's lines under another customer's request."""
    buf = _capture()
    _drive(logging_config.RequestContextMiddleware(_logging_app),
           _scope(headers=[(b"x-request-id", b"first")]))
    assert logging_config.get_request_id() is None, "the id must be reset after the request"

    logging_config.get_logger("amp.test.after").info("background work")
    after = _lines(buf)[-1]
    assert "request_id" not in after, after
    print("PASS the request id is unbound once the request finishes")


# ── Tenant, and the no-context case ─────────────────────────────────

def test_logging_outside_a_request_does_not_raise():
    """THE SIMULATOR CASE. The 45-second tick, the MQTT listener and startup
    seeding all run with no request bound. Reading the tenant there must return
    nothing rather than raise — a logging call that throws would take out the
    tick loop it was supposed to be reporting on."""
    buf = _capture()
    assert logging_config.get_request_id() is None
    logging_config.get_logger("amp.test.sim").info("sim tick complete", extra={"ticks": 3})

    line = _lines(buf)[0]
    assert "request_id" not in line, line
    assert "tenant" not in line, line
    assert line["ticks"] == 3, line
    print("PASS a background log call with no request context works and omits the fields")


def test_a_tenant_lookup_that_raises_does_not_break_the_log_line():
    """The formatter reads the tenant out of another module's contextvar. If
    that lookup ever throws — a half-initialised import during boot, a change
    on the tenancy side — the log call must still produce its line. Otherwise
    the first symptom of a small tenancy regression is that every logging call
    in the process starts raising, including the ones reporting the regression.

    Note this is NOT covered by the no-context test above: there the lookup
    returns None perfectly happily. Only forcing it to raise exercises the guard.
    """
    original = logging_config._tenant_lookup

    def exploding_lookup():
        raise RuntimeError("tenancy is not ready")

    buf = _capture()
    logging_config._tenant_lookup = exploding_lookup
    try:
        logging_config.get_logger("amp.test.tenantfail").error("machine offline")
    finally:
        logging_config._tenant_lookup = original

    line = _lines(buf)[0]
    assert line["message"] == "machine offline", line
    assert "tenant" not in line, line
    print("PASS a raising tenant lookup is swallowed and the line still lands")


def test_the_tenant_is_attached_when_one_is_bound():
    """CONTROL for the two tests above: the field is omitted because there is
    genuinely no context, not because the formatter never attaches it. This is
    the field that "what broke for THAT customer" is filtered on."""
    import tenancy

    buf = _capture()
    token = tenancy.set_current_tenant("GMATS")
    try:
        logging_config.get_logger("amp.test.tenant").info("stock issued")
    finally:
        tenancy.reset_current_tenant(token)
    logging_config.get_logger("amp.test.tenant").info("back to background")

    scoped, unscoped = _lines(buf)
    assert scoped["tenant"] == "GMATS", scoped
    assert "tenant" not in unscoped, unscoped
    print("PASS the bound tenant is attached, and absent once it is unbound")


# ── Redaction ───────────────────────────────────────────────────────

def test_secrets_are_redacted_and_ordinary_fields_are_not():
    """Logs are the least protected copy of production data — read by anyone
    with dashboard access, retained long after the incident. CONTROL: the
    non-sensitive fields must survive untouched, so a formatter that redacted
    everything (or nothing) fails."""
    buf = _capture()
    logging_config.get_logger("amp.test.redact").info("inbound call", extra={
        "password": "hunter2",
        "api_key": "sk-live-1234",
        "Authorization": "Bearer eyJhbGciOi",
        "refresh_token": "rt-9999",
        "client_secret": "shhh",
        "machine_id": "CNC-04",           # CONTROL
        "work_order": "WO-1188",          # CONTROL
        "quantity": 42,                   # CONTROL
    })

    line = _lines(buf)[0]
    for key in ("password", "api_key", "Authorization", "refresh_token", "client_secret"):
        assert line[key] == "[REDACTED]", (key, line[key])
    assert line["machine_id"] == "CNC-04", line
    assert line["work_order"] == "WO-1188", line
    assert line["quantity"] == 42, line

    # And nothing of the secret survives anywhere in the serialised line.
    raw = buf.getvalue()
    assert "hunter2" not in raw and "sk-live-1234" not in raw and "eyJhbGciOi" not in raw
    print("PASS sensitive keys are redacted while ordinary fields pass through")


def test_redaction_reaches_nested_structures():
    """Secrets arrive inside payloads, not as top-level kwargs — a handler
    logging the request body it just received is the realistic leak."""
    buf = _capture()
    logging_config.get_logger("amp.test.nested").warning("login rejected", extra={
        "body": {"username": "gmats", "password": "hunter2",
                 "meta": [{"api_key": "sk-live"}, {"role": "Supervisor"}]},
    })

    body = _lines(buf)[0]["body"]
    assert body["username"] == "gmats", body
    assert body["password"] == "[REDACTED]", body
    assert body["meta"][0]["api_key"] == "[REDACTED]", body
    assert body["meta"][1]["role"] == "Supervisor", body
    print("PASS redaction recurses into nested dicts and lists")


def test_key_matching_is_shape_insensitive():
    """The same secret is spelled a dozen ways across HTTP headers, JSON bodies
    and kwargs. Matching must not depend on the punctuation."""
    for key in ("API_KEY", "x-api-key", "apiKey", "SECRET", "accessToken", "Set-Cookie"):
        assert logging_config.is_sensitive_key(key), key
    for key in ("machine_id", "work_order", "utilization", "operator", "quantity"):
        assert not logging_config.is_sensitive_key(key), key
    print("PASS key matching normalises case and punctuation without over-matching")


# ── Idempotence ─────────────────────────────────────────────────────

def test_configure_logging_twice_does_not_duplicate_handlers():
    """Boot code in this repo runs at import AND again in the startup event. A
    second unguarded call would emit every line twice — doubling the log bill
    and making every 'how many times did this happen' answer wrong.

    CONTROL: the handler count is asserted, not just the output. Two handlers
    both writing to the same test buffer could otherwise be mistaken for one if
    only the parsed content were checked."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        buf = io.StringIO()
        logging_config.configure_logging(stream=buf)
        assert len(root.handlers) == 1, root.handlers

        logging_config.configure_logging(stream=buf)
        logging_config.configure_logging(stream=buf)
        assert len(root.handlers) == 1, root.handlers

        logging_config.get_logger("amp.test.idem").warning("boot complete")
        assert len(_lines(buf)) == 1, "the record must be emitted exactly once"
    finally:
        root.handlers = saved
    print("PASS configure_logging is idempotent (one handler, one line)")


def test_a_foreign_handler_is_left_alone():
    """Sentry and any future log shipper attach their own root handler.
    Guaranteeing 'exactly one handler' by clearing the list would silently
    switch error reporting off."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        foreign = logging.StreamHandler(io.StringIO())
        root.addHandler(foreign)
        logging_config.configure_logging(stream=io.StringIO())
        assert foreign in root.handlers, root.handlers
        assert len(root.handlers) == 2, root.handlers
    finally:
        root.handlers = saved
    print("PASS a handler this module did not install is preserved")


def test_websocket_scopes_pass_through_untouched():
    """The live dashboard's WS handshake has no HTTP response to decorate;
    touching it would corrupt the upgrade."""
    seen = {}

    async def ws_app(scope, receive, send):
        seen["type"] = scope["type"]

    asyncio.run(logging_config.RequestContextMiddleware(ws_app)({"type": "websocket"}, None, None))
    assert seen["type"] == "websocket"
    print("PASS websocket scopes bypass the request-context middleware")


if __name__ == "__main__":
    test_output_is_parseable_json_with_the_expected_keys()
    test_extra_fields_are_structured_not_stringified()
    test_an_extra_cannot_overwrite_a_core_field()
    test_a_non_serialisable_value_still_produces_a_line()
    test_exceptions_carry_their_traceback()
    test_the_level_filters()
    test_the_level_comes_from_the_environment()
    test_an_inbound_request_id_is_honoured()
    test_an_id_is_generated_when_none_is_supplied()
    test_every_request_emits_one_access_line_with_its_timing()
    test_the_access_line_records_a_failed_response_too()
    test_the_id_is_echoed_on_responses_that_never_reach_a_route()
    test_a_hostile_inbound_id_is_sanitised()
    test_the_id_survives_a_sync_endpoint_on_the_threadpool()
    test_the_id_does_not_leak_between_requests()
    test_logging_outside_a_request_does_not_raise()
    test_a_tenant_lookup_that_raises_does_not_break_the_log_line()
    test_the_tenant_is_attached_when_one_is_bound()
    test_secrets_are_redacted_and_ordinary_fields_are_not()
    test_redaction_reaches_nested_structures()
    test_key_matching_is_shape_insensitive()
    test_configure_logging_twice_does_not_duplicate_handlers()
    test_a_foreign_handler_is_left_alone()
    test_websocket_scopes_pass_through_untouched()
    print("ALL LOGGING CONFIG TESTS PASSED")
