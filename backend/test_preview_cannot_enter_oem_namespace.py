"""The founder's preview header cannot bind the OEM sentinel namespace.

THE DEFECT
----------
An OEM request binds a SENTINEL tenant, `OEM:<code>`, that no factory can hold;
that is what makes factory data invisible to a manufacturer by construction
(ADR-0017). tenancy.assert_tenant_code_available refuses any tenant code
containing ':' and is enforced at /saas/tenants, which the docs call "the one
place a tenant code enters the system".

It is not the only place. The company-switcher preview binds whatever the
X-Tenant header names, for a founder-workspace Admin, with no check. A request
with `X-Tenant: OEM:ACME` bound ACME's sentinel as the tenant, so anything it
wrote (a user, a machine, a work order) landed in the namespace ACME's OEM
sessions read: the collision the reserved namespace exists to prevent,
through the one door that did not check it. The switcher only lists real
tenants, so it takes a crafted request, from the founder; the rule should
still hold at every door.

THE RULE
--------
effective_tenant never binds a reserved code from the header: a reserved preview
falls back to the token's own tenant, as every refused preview does.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_preview_cannot_enter_oem_namespace.py
"""
import asyncio

import auth
import tenancy

FOUNDER = {"sub": "founder", "tenant": "DEFAULT", "role": "Admin"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _through_the_middleware(header_tenant):
    """Drive the real ASGI middleware with a founder token and an X-Tenant
    header, and report the tenant the request was bound to."""
    seen = {}

    async def app(scope, receive, send):
        seen["tenant"] = tenancy.current_tenant()

    mw = tenancy.TenantScopeMiddleware(app)
    token = auth.create_access_token(dict(FOUNDER))
    headers = [(b"authorization", f"Bearer {token}".encode()), (b"x-tenant", header_tenant.encode())]

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    asyncio.run(mw({"type": "http", "headers": headers}, receive, send))
    return seen.get("tenant")


def _app_status(path, header_tenant):
    """GET `path` through the whole application (every middleware, in order),
    as the founder, and return the response status."""
    import main
    token = auth.create_access_token(dict(FOUNDER))
    headers = [(b"authorization", f"Bearer {token}".encode()), (b"origin", b"https://flow-mes.vercel.app")]
    if header_tenant is not None:
        headers.append((b"x-tenant", header_tenant.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    out = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]

    asyncio.run(main.app(scope, receive, send))
    return out.get("status")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. A RESERVED CODE IN THE HEADER IS NOT BOUND")
    for code in ("OEM:ACME", "oem:acme", "WEIRD:CODE"):
        got = tenancy.effective_tenant("DEFAULT", code, "Admin", FOUNDER)
        check(f"effective_tenant with X-Tenant {code!r} stays on the founder's own tenant",
              got == "DEFAULT", repr(got))
        got = _through_the_middleware(code)
        check(f"...and so does a real request through the middleware", got == "DEFAULT", repr(got))

    section("2. CONTROL: AN ORDINARY PREVIEW STILL WORKS")
    check("effective_tenant previews ACME", tenancy.effective_tenant("DEFAULT", "ACME", "Admin", FOUNDER) == "ACME")
    check("...through the middleware too", _through_the_middleware("ACME") == "ACME")

    section("3. THROUGH THE REAL APP: A RESERVED PREVIEW IS AN EXPLICIT 403")
    for code in ("OEM:ACME", "oem:acme"):
        status = _app_status("/openapi.json", code)
        check(f"X-Tenant {code!r} on any route is refused (403), before the route runs",
              status == 403, str(status))
    check("CONTROL: an ordinary preview header passes the guard (200)",
          _app_status("/openapi.json", "ACME") == 200)
    check("CONTROL: and so does no header", _app_status("/openapi.json", None) == 200)

    section("4. ONE RULE AT BOTH DOORS")
    check("the header check is the registry's own rule",
          all(tenancy.is_reserved_tenant_code(c) for c in ("OEM:ACME", "oem:acme", "WEIRD:CODE"))
          and not tenancy.is_reserved_tenant_code("ACME"))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_preview_cannot_enter_oem_namespace():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
