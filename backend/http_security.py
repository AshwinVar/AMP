"""Browser-facing security headers and abuse throttling.

Two pure-ASGI middlewares, written in the same idiom as plan_gate and
tenancy so the stack stays dependency-free (no slowapi, no starlette
BaseHTTPMiddleware — both add a request-scoped task wrapper this app does
not need).

WHY HEADERS. The audit found the app emitted no security headers at all
while storing its JWT in localStorage. That combination means any script
injected into the page can read the token and act as the user for up to
four hours. A CSP does not fix localStorage, but it is what stops the
injected script from running or from exfiltrating what it reads.

WHY THROTTLING. /login had no attempt limit — bcrypt's cost was the only
thing between an attacker and an unlimited password guess. And once an LLM
key is configured, /ai/ask is a paid upstream call any authenticated user
can make in a loop; a spend cap has to live somewhere and the cheapest
place is the front door.

Both are deliberately conservative: the throttle only covers the endpoints
that are genuinely expensive or genuinely attackable, and the CSP is
report-friendly rather than maximal, because the frontend is Next.js and a
strict script-src would need nonce plumbing through the app shell.
"""
import os
import time
from collections import defaultdict, deque
from threading import Lock

# ── Security headers ────────────────────────────────────────────────

# 'unsafe-inline' on style-src is required: Next.js emits inline <style>
# for critical CSS and Tailwind's runtime injects styles. Removing it
# breaks rendering, so it is an accepted, documented weakening — the
# XSS vector that matters here is script execution, which stays locked.
#
# script-src omits 'unsafe-inline' but keeps 'unsafe-eval' OFF. The API
# serves JSON and docs, not the app shell (that is Vercel's), so this CSP
# protects /docs and any HTML the API itself returns rather than the SPA.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "      # Swagger UI bootstraps inline
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "                  # clickjacking, modern form
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

_HEADERS = {
    b"content-security-policy": _CSP.encode(),
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",                # legacy twin of frame-ancestors
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"cross-origin-opener-policy": b"same-origin",
    b"permissions-policy": b"geolocation=(), microphone=(), camera=(), payment=()",
}

# HSTS is only emitted when the request arrived over TLS. Sending it on a
# plain-HTTP local dev request would pin localhost to https in the
# developer's browser for a year — a genuinely painful, hard-to-undo
# footgun. Railway terminates TLS and forwards x-forwarded-proto.
_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


class SecurityHeadersMiddleware:
    """Attach security headers to every response, including error responses.

    Pure ASGI so the headers survive paths that never reach a route — a
    CORS preflight, a 404, a middleware rejection from the plan gate. A
    BaseHTTPMiddleware version would miss some of those.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        forwarded = ""
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-proto":
                forwarded = v.decode("latin-1")
                break
        secure = scope.get("scheme") == "https" or forwarded == "https"

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                for key, value in _HEADERS.items():
                    if key not in present:
                        headers.append((key, value))
                if secure and _HSTS[0] not in present:
                    headers.append(_HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── Rate limiting ───────────────────────────────────────────────────

def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# (max_requests, window_seconds) per path prefix, longest-prefix wins.
# Login is the brute-force target; the AI routes are the cost target.
RATE_LIMITS = {
    "/login": (_env_int("RATE_LIMIT_LOGIN", 10), 60),
    # The manufacturer's front door is a brute-force target for the same reason
    # the factory's is, and it reaches a DIFFERENT principal table -- so it needs
    # its own entry rather than inheriting one by prefix. ("/oem" is not a prefix
    # of "/login".)
    "/oem/login": (_env_int("RATE_LIMIT_LOGIN", 10), 60),
    "/oem/change-password": (_env_int("RATE_LIMIT_LOGIN", 10), 60),
    "/register": (_env_int("RATE_LIMIT_LOGIN", 10), 60),
    "/auth/change-password": (_env_int("RATE_LIMIT_LOGIN", 10), 60),
    "/ai/ask": (_env_int("RATE_LIMIT_AI", 20), 60),
    "/ai/report": (_env_int("RATE_LIMIT_AI", 20), 60),
    "/copilot/ask": (_env_int("RATE_LIMIT_AI", 20), 60),
}


class _SlidingWindow:
    """Per-key timestamp deques. Bounded by construction: entries older than
    the window are dropped on every touch, and idle keys are swept, so the
    dict cannot grow without bound from spoofed client addresses."""

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = Lock()
        self._last_sweep = 0.0

    def hit(self, key, limit, window, now):
        with self._lock:
            if now - self._last_sweep > 300:
                self._sweep(now, window)
            q = self._hits[key]
            cutoff = now - window
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                return False, int(q[0] + window - now) + 1
            q.append(now)
            return True, 0

    def _sweep(self, now, window):
        self._last_sweep = now
        stale = [k for k, q in self._hits.items() if not q or q[-1] <= now - max(window, 3600)]
        for k in stale:
            del self._hits[k]

    def reset(self):
        with self._lock:
            self._hits.clear()
            self._last_sweep = 0.0


_window = _SlidingWindow()


def _client_key(scope):
    """Identify the caller. Prefers the left-most x-forwarded-for hop, which
    on Railway is the real client; falls back to the socket peer.

    x-forwarded-for is spoofable by anyone who can reach the app directly.
    That is acceptable here: spoofing it spreads an attacker's attempts
    across buckets, which is a denial of THEIR OWN throttle, not a bypass of
    anyone else's — and the sweep keeps the dict bounded regardless.
    """
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            first = v.decode("latin-1").split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def _limit_for(path):
    match = None
    for prefix, conf in RATE_LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            if match is None or len(prefix) > len(match[0]):
                match = (prefix, conf)
    return match[1] if match else None


class RateLimitMiddleware:
    """Throttle the abusable endpoints. Everything else passes untouched.

    Deliberately in-process: AMP runs a single uvicorn worker, so a shared
    store would add a dependency and a failure mode for no gain today. The
    limit is per-instance — documented here so that whoever adds a second
    replica knows the effective limit becomes N x the configured value and
    that this is the moment to move the window into Postgres or Redis.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        conf = _limit_for(scope.get("path", ""))
        if conf is None:
            await self.app(scope, receive, send)
            return

        limit, window = conf
        allowed, retry_after = _window.hit(
            f"{_client_key(scope)}:{scope['path']}", limit, window, time.monotonic()
        )
        if allowed:
            await self.app(scope, receive, send)
            return

        # 429 with Retry-After. The body is deliberately generic — it must not
        # reveal whether the throttled username exists.
        body = b'{"detail":"Too many requests. Please wait and try again."}'
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
