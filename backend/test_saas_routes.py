"""SaaS-routes registration test (ADR-0008).

The tenant-lifecycle endpoints live in saas_routes.register(app), peeled out of
main.py. This guards the extraction: every expected SaaS path is registered and
owned by saas_routes. (The handler behaviour — onboarding, plan tiers, admin
provisioning, purge, registry scoping — is exercised by test_onboarding.py and
test_offboarding.py, which call saas_routes.<handler> directly.)

Run:  python backend/test_saas_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/saas/tenants", "/saas/tenants/{tenant_id}/admin",
    "/saas/tenants/{tenant_id}", "/analytics/saas",
}


def test_saas_paths_owned_by_saas_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"SaaS paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"saas_routes"}}
    assert not wrong, f"SaaS paths not owned solely by saas_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} SaaS paths owned by saas_routes")


if __name__ == "__main__":
    test_saas_paths_owned_by_saas_routes()
    print("ALL SAAS ROUTE TESTS PASSED")
