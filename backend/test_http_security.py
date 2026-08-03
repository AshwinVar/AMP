"""Security headers + rate limiting (http_security.py).

THE GAPS THESE CLOSE. The platform audit found two holes that need no bug to
exploit, only patience:

1. The API emitted NO security headers at all — no CSP, no nosniff, no
   frame-ancestors — while the frontend keeps its JWT in localStorage. Any
   injected script could read the token and act as that user for four hours.
2. /login had no attempt limit. bcrypt's cost was the entire defence against
   an unlimited password guess, and /ai/ask (a paid upstream call once a key
   is set) had no quota at all.

These tests drive the middlewares directly through the ASGI protocol rather
than through a route, because the whole point of both is that they apply on
paths that never reach a route — preflights, 404s, and the plan gate's own
rejections.

Run:  python backend/test_http_security.py     (exit 0 = pass)
"""
import asyncio

import http_security
from http_security import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    _client_key,
    _limit_for,
)


def _scope(path="/", scheme="http", headers=None, client=("1.2.3.4", 5000)):
    return {
        "type": "http",
        "path": path,
        "scheme": scheme,
        "headers": headers or [],
        "client": client,
    }


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b"{}"})


def _drive(mw, scope):
    """Run one request through a middleware, returning (status, headers dict)."""
    captured = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        else:
            captured.setdefault("body", b"")
            captured["body"] += message.get("body", b"")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(mw(scope, receive, send))
    return captured


# ── Security headers ────────────────────────────────────────────────

def test_every_response_carries_the_headers():
    r = _drive(SecurityHeadersMiddleware(_ok_app), _scope())
    h = r["headers"]
    assert "content-security-policy" in h, h
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    print("PASS security headers present on a normal response")


def test_headers_survive_a_response_the_app_never_routed():
    """A 404 or a middleware rejection must still be hardened. This is why the
    middleware is pure ASGI and wraps send() rather than decorating a route."""
    async def rejecting_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"denied"})

    r = _drive(SecurityHeadersMiddleware(rejecting_app), _scope(path="/nope"))
    assert r["status"] == 403
    assert "content-security-policy" in r["headers"]
    print("PASS headers attach to non-route responses too")


def test_hsts_only_over_tls():
    """A max-age=31536000 HSTS on a plain-HTTP dev request would pin localhost
    to https in the developer's browser for a year, and it is painful to undo.
    So it is emitted only when the request actually arrived over TLS."""
    plain = _drive(SecurityHeadersMiddleware(_ok_app), _scope(scheme="http"))
    assert "strict-transport-security" not in plain["headers"]

    direct_tls = _drive(SecurityHeadersMiddleware(_ok_app), _scope(scheme="https"))
    assert "strict-transport-security" in direct_tls["headers"]

    # Railway terminates TLS upstream and forwards the real scheme.
    proxied = _drive(SecurityHeadersMiddleware(_ok_app),
                     _scope(headers=[(b"x-forwarded-proto", b"https")]))
    assert "strict-transport-security" in proxied["headers"], proxied["headers"]
    print("PASS HSTS is sent over TLS (direct and proxied) and withheld on plain HTTP")


def test_an_app_supplied_header_is_not_overwritten():
    """If a route deliberately sets its own CSP (or X-Frame-Options to allow an
    embed), the blanket default must not stomp it."""
    async def opinionated(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"x-frame-options", b"SAMEORIGIN")]})
        await send({"type": "http.response.body", "body": b""})

    r = _drive(SecurityHeadersMiddleware(opinionated), _scope())
    assert r["headers"]["x-frame-options"] == "SAMEORIGIN", r["headers"]
    print("PASS a route's own header wins over the default")


def test_websocket_scopes_pass_through_untouched():
    """The WS handshake has no HTTP response to decorate; touching it would
    corrupt the upgrade."""
    seen = {}

    async def ws_app(scope, receive, send):
        seen["type"] = scope["type"]

    asyncio.run(SecurityHeadersMiddleware(ws_app)({"type": "websocket"}, None, None))
    assert seen["type"] == "websocket"
    print("PASS websocket scopes bypass the header middleware")


# ── Rate limiting ───────────────────────────────────────────────────

def test_login_throttles_after_the_limit():
    http_security._window.reset()
    mw = RateLimitMiddleware(_ok_app)
    limit = http_security.RATE_LIMITS["/login"][0]

    for i in range(limit):
        r = _drive(mw, _scope(path="/login"))
        assert r["status"] == 200, f"request {i + 1} of {limit} should pass, got {r['status']}"

    blocked = _drive(mw, _scope(path="/login"))
    assert blocked["status"] == 429, blocked
    assert "retry-after" in blocked["headers"]
    assert int(blocked["headers"]["retry-after"]) > 0
    # The body must not reveal whether the attempted username exists.
    assert b"username" not in blocked["body"].lower()
    print("PASS /login throttles after %d attempts with Retry-After" % limit)


def test_the_throttle_is_per_client_not_global():
    """THE CONTROL. Without this, a fix that simply counted all requests would
    pass the test above while letting one attacker lock out every other user —
    a denial of service dressed up as a security feature."""
    http_security._window.reset()
    mw = RateLimitMiddleware(_ok_app)
    limit = http_security.RATE_LIMITS["/login"][0]

    attacker = _scope(path="/login", headers=[(b"x-forwarded-for", b"9.9.9.9")])
    for _ in range(limit + 3):
        _drive(mw, attacker)
    assert _drive(mw, attacker)["status"] == 429

    innocent = _scope(path="/login", headers=[(b"x-forwarded-for", b"7.7.7.7")])
    assert _drive(mw, innocent)["status"] == 200, "a second client must not inherit the throttle"
    print("PASS the throttle is keyed per client, not global")


def test_the_throttle_is_per_endpoint():
    """Exhausting /login must not lock the user out of /ai/ask."""
    http_security._window.reset()
    mw = RateLimitMiddleware(_ok_app)
    for _ in range(http_security.RATE_LIMITS["/login"][0] + 1):
        _drive(mw, _scope(path="/login"))
    assert _drive(mw, _scope(path="/login"))["status"] == 429
    assert _drive(mw, _scope(path="/ai/ask"))["status"] == 200
    print("PASS throttle buckets are per endpoint")


def test_unlisted_paths_are_never_throttled():
    """The shop floor PATCHes machine status constantly; throttling the whole
    API would break the product. Only the abusable endpoints are covered."""
    http_security._window.reset()
    mw = RateLimitMiddleware(_ok_app)
    for _ in range(200):
        r = _drive(mw, _scope(path="/machines"))
    assert r["status"] == 200
    print("PASS ordinary endpoints pass unthrottled")


def test_prefix_matching_is_boundary_safe():
    """/login must not also throttle a hypothetical /login-history, and
    /ai/ask/stream must inherit /ai/ask's bucket."""
    assert _limit_for("/login") is not None
    assert _limit_for("/login/") is not None
    assert _limit_for("/loginhistory") is None, "prefix match must respect the path boundary"
    assert _limit_for("/ai/ask/stream") is not None
    assert _limit_for("/machines") is None
    print("PASS path-prefix matching respects segment boundaries")


def test_client_key_prefers_the_real_client_over_the_proxy():
    assert _client_key(_scope(headers=[(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")])) == "203.0.113.7"
    assert _client_key(_scope(client=("8.8.8.8", 1))) == "8.8.8.8"
    assert _client_key(_scope(client=None)) == "unknown"
    print("PASS client key uses the left-most forwarded hop, then the peer")


def test_the_window_slides():
    """An expired window must free the caller — a throttle that never releases
    is an outage."""
    http_security._window.reset()
    w = http_security._SlidingWindow()
    now = 1000.0
    for _ in range(3):
        assert w.hit("k", 3, 60, now)[0]
    assert not w.hit("k", 3, 60, now)[0]
    assert w.hit("k", 3, 60, now + 61)[0], "the window must release after it elapses"
    print("PASS the sliding window releases once the period elapses")


if __name__ == "__main__":
    test_every_response_carries_the_headers()
    test_headers_survive_a_response_the_app_never_routed()
    test_hsts_only_over_tls()
    test_an_app_supplied_header_is_not_overwritten()
    test_websocket_scopes_pass_through_untouched()
    test_login_throttles_after_the_limit()
    test_the_throttle_is_per_client_not_global()
    test_the_throttle_is_per_endpoint()
    test_unlisted_paths_are_never_throttled()
    test_prefix_matching_is_boundary_safe()
    test_client_key_prefers_the_real_client_over_the_proxy()
    test_the_window_slides()
    print("ALL HTTP SECURITY TESTS PASSED")
