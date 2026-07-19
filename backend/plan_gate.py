"""Server-side plan gating — the licence enforced at the API, not just the UI.

The frontend padlocks module packs the tenant's plan doesn't include (PR #121),
but until now a Starter tenant could still call premium endpoints directly.
This middleware closes that: each request path maps to a module pack, and the
effective tenant's licence (TenantConfig.enabled_modules) must include it.

Principles:
  * Same chokepoint philosophy as tenant scoping (ADR-0002): one table + one
    middleware, not per-endpoint decorators that can be forgotten.
  * Gate by the EFFECTIVE tenant (the founder previewing a Starter tenant is
    blocked exactly like the customer — preview fidelity).
  * core and admin packs are never gated, mirroring the frontend rule that no
    tenant is locked out of basics or account management.
  * Fail OPEN: if the licence can't be read (DB hiccup), let the request pass —
    availability beats enforcement for a plan gate.
"""
import time

from database import SessionLocal
from platform_routes import get_or_create_config
from tenancy import DEFAULT_TENANT, effective_tenant, tenant_from_token

# Request-path prefix -> module pack. Longest prefix wins. Anything unmapped is
# treated as core (ungated). Deliberately conservative: only unambiguous
# premium families are listed, and endpoints woven into the core experience
# (briefing, escalations, search, scorecard) stay open.
PATH_PACKS = [
    # Operations Pack — work orders, planning, scheduling, operator, dispatch
    ("/work-orders", "operations"),
    ("/production-plans", "operations"),
    ("/production-schedules", "operations"),
    ("/analytics/production-schedules", "operations"),
    ("/operator", "operations"),
    ("/analytics/operator-terminal", "operations"),
    # NOTE: /customer-orders and /delivery-summary stay open — the Overview's
    # delivery snapshot (core) reads and exports through them.
    # Factory Pack — maintenance, quality, inventory, purchasing, twin, health
    ("/maintenance", "factory"),
    ("/quality", "factory"),
    ("/inventory", "factory"),
    ("/suppliers", "factory"),
    ("/purchase-orders", "factory"),
    ("/twin-overlay", "factory"),
    ("/machine-health", "factory"),
    ("/remnants", "factory"),
    ("/issue-slips", "factory"),
    ("/grns", "factory"),
    ("/cycle-counts", "factory"),
    ("/bom", "factory"),
    # Intelligence Pack — IoT, connectivity, AI insights/copilot, agents
    ("/iot", "intelligence"),
    ("/analytics/iot-command", "intelligence"),
    ("/industrial", "intelligence"),
    ("/ai", "intelligence"),
    ("/analytics/ai-insights", "intelligence"),
    ("/copilot", "intelligence"),
    ("/insights", "intelligence"),
    ("/agent-actions", "intelligence"),
    ("/agent-roster", "intelligence"),
    ("/agent-policy", "intelligence"),
]
_ALWAYS_OPEN_PACKS = {"core", "admin"}

_PACK_LABELS = {
    "operations": "Operations Pack",
    "factory": "Factory Pack",
    "intelligence": "Intelligence Pack",
}

_CACHE_TTL_SECONDS = 60
_licence_cache = {}   # tenant_code -> (frozenset(packs), expires_at)


def pack_for_path(path):
    """The module pack a request path belongs to, or None if ungated."""
    best = None
    for prefix, pack in PATH_PACKS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, pack)
    return best[1] if best else None


def invalidate(tenant_code):
    """Drop a tenant's cached licence (called when its plan changes)."""
    _licence_cache.pop(tenant_code, None)


def licensed_packs(tenant_code):
    """The tenant's licensed packs, cached briefly. Fail-open: returns None
    (meaning 'allow everything') if the licence can't be read."""
    now = time.time()
    hit = _licence_cache.get(tenant_code)
    if hit and hit[1] > now:
        return hit[0]
    try:
        db = SessionLocal()
        try:
            cfg = get_or_create_config(db, tenant_code)
            packs = frozenset(m for m in (cfg.enabled_modules or "").split(",") if m)
        finally:
            db.close()
    except Exception:
        return None
    _licence_cache[tenant_code] = (packs, now + _CACHE_TTL_SECONDS)
    return packs


class PlanGateMiddleware:
    """Pure-ASGI gate. Self-contained: derives the effective tenant from the
    request headers itself, so it doesn't depend on middleware ordering."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        pack = pack_for_path(scope.get("path", ""))
        if pack is None or pack in _ALWAYS_OPEN_PACKS:
            await self.app(scope, receive, send)
            return

        token = None
        header_tenant = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                parts = value.decode("latin-1").split(" ", 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1].strip()
            elif key == b"x-tenant":
                header_tenant = value.decode("latin-1").strip() or None
        claim = tenant_from_token(token)
        if claim is None:
            # Unauthenticated: let the auth layer produce its usual 401.
            await self.app(scope, receive, send)
            return

        tenant = effective_tenant(claim, header_tenant) or DEFAULT_TENANT
        packs = licensed_packs(tenant)
        if packs is None or pack in packs:
            await self.app(scope, receive, send)
            return

        label = _PACK_LABELS.get(pack, pack)
        body = (f'{{"detail": "The {label} is not included in your current plan. '
                f'Contact your provider to upgrade."}}').encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("latin-1"))]})
        await send({"type": "http.response.body", "body": body})
