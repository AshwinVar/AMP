"""Server-side plan-gate tests.

Proves the API-level licence enforcement in isolation:
  * the path -> pack table (premium families gated, core woven-ins open);
  * a Starter tenant is 403'd on premium endpoints but passes core ones;
  * the founder previewing a Starter tenant is gated identically (fidelity);
  * a client token cannot escape its licence via a forged X-Tenant header;
  * unauthenticated requests pass through (the auth layer 401s them);
  * licence-read failures fail OPEN.

Run:  python backend/test_plan_gate.py     (exit 0 = pass)
"""
import asyncio
import time

import plan_gate
from plan_gate import PlanGateMiddleware, pack_for_path
from auth import create_access_token


def _seed_licence(tenant, packs):
    plan_gate._licence_cache[tenant] = (frozenset(packs), time.time() + 3600)


def _run(middleware, path, token=None, x_tenant=None):
    """Drive the ASGI middleware once; returns 'passed' or the response status."""
    headers = []
    if token:
        headers.append((b"authorization", b"Bearer " + token.encode()))
    if x_tenant:
        headers.append((b"x-tenant", x_tenant.encode()))
    scope = {"type": "http", "path": path, "headers": headers}
    result = {}

    async def inner_app(scope, receive, send):
        result["passed"] = True

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]

    asyncio.run(PlanGateMiddleware(inner_app)(scope, None, send))
    return "passed" if result.get("passed") else result.get("status")


def test_path_pack_table():
    assert pack_for_path("/work-orders") == "operations"
    assert pack_for_path("/work-orders/7") == "operations"
    assert pack_for_path("/copilot/ask") == "intelligence"
    assert pack_for_path("/analytics/iot-command") == "intelligence"
    assert pack_for_path("/inventory/items") == "factory"
    # core woven-ins stay open
    for open_path in ("/machines", "/briefing", "/scorecard", "/search",
                      "/inventory-summary", "/quality-summary", "/maintenance-summary",
                      "/delivery-summary", "/customer-orders/export", "/escalations/3"):
        assert pack_for_path(open_path) is None, open_path
    print("PASS path->pack table")


def test_starter_gated_enterprise_not():
    _seed_licence("APEX", {"core"})
    _seed_licence("BIGCO", {"core", "operations", "factory", "intelligence", "admin"})
    apex = create_access_token({"sub": "a", "role": "Admin", "tenant": "APEX"})
    bigco = create_access_token({"sub": "b", "role": "Admin", "tenant": "BIGCO"})

    assert _run(None, "/copilot/ask", token=apex) == 403
    assert _run(None, "/work-orders", token=apex) == 403
    assert _run(None, "/machines", token=apex) == "passed"
    assert _run(None, "/briefing", token=apex) == "passed"
    assert _run(None, "/copilot/ask", token=bigco) == "passed"
    print("PASS starter gated, enterprise not")


def test_founder_preview_gated_like_the_customer():
    _seed_licence("APEX", {"core"})
    _seed_licence("DEFAULT", {"core", "operations", "factory", "intelligence", "admin"})
    founder = create_access_token({"sub": "f", "role": "Admin", "tenant": "DEFAULT"})
    assert _run(None, "/copilot/ask", token=founder) == "passed"                      # own workspace
    assert _run(None, "/copilot/ask", token=founder, x_tenant="APEX") == 403          # preview fidelity
    print("PASS founder preview gated like the customer")


def test_client_cannot_escape_via_header():
    _seed_licence("APEX", {"core"})
    _seed_licence("DEFAULT", {"core", "operations", "factory", "intelligence", "admin"})
    apex = create_access_token({"sub": "a", "role": "Admin", "tenant": "APEX"})
    assert _run(None, "/copilot/ask", token=apex, x_tenant="DEFAULT") == 403
    print("PASS client token cannot escape its licence")


def test_unauthenticated_passes_through():
    assert _run(None, "/copilot/ask") == "passed"   # downstream auth 401s it
    print("PASS unauthenticated passes through to the auth layer")


def test_licence_read_failure_fails_open():
    plan_gate._licence_cache.clear()
    original = plan_gate.get_or_create_config
    plan_gate.get_or_create_config = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        ghost = create_access_token({"sub": "g", "role": "Admin", "tenant": "GHOST"})
        assert _run(None, "/copilot/ask", token=ghost) == "passed"
    finally:
        plan_gate.get_or_create_config = original
    print("PASS licence-read failure fails open")


if __name__ == "__main__":
    test_path_pack_table()
    test_starter_gated_enterprise_not()
    test_founder_preview_gated_like_the_customer()
    test_client_cannot_escape_via_header()
    test_unauthenticated_passes_through()
    test_licence_read_failure_fails_open()
    print("ALL PLAN-GATE TESTS PASSED")
