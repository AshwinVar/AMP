"""Platform route registration guard.

platform_routes owns the platform/ops surface — the truthful /health check, the
audit-log read/write API, and per-tenant licensing/branding config. It predates
the ADR-0009 guard-test discipline; this asserts every path is registered exactly
once and owned solely by the module. /health and /audit-logs especially: both
were once shadowed duplicates (the /health dedup #142, the /audit-logs
consolidation #166), so single-ownership here is the guard against that
regressing.

Run:  python backend/test_platform_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/health", "/audit-logs",
    "/tenant-config", "/tenant-configs", "/tenant-configs/{tenant_code}",
}


def test_platform_paths_registered_once_and_owned():
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"platform paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"platform_routes"}}
    assert not wrong, f"paths not owned solely by platform_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} platform paths owned by platform_routes")


def test_health_is_single_and_not_shadowed():
    # /health returns a truthful 200/503 by DB liveness; a second (always-200)
    # /health was removed in #142. Exactly one may exist, owned by platform_routes.
    health = [r for r in main.app.routes if getattr(r, "path", "") == "/health"]
    assert len(health) == 1, f"expected exactly one /health, found {len(health)}"
    assert health[0].endpoint.__module__ == "platform_routes"
    print("PASS /health is single and owned by platform_routes (no shadow)")


if __name__ == "__main__":
    test_platform_paths_registered_once_and_owned()
    test_health_is_single_and_not_shadowed()
    print("ALL PLATFORM ROUTE TESTS PASSED")
