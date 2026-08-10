"""Module manifest (modules.json) — single source of truth for the plug-and-play
plugin system. Pins that the plan-gate still derives exactly the gated routes it
enforced before (no route silently opened/closed) and that GET /modules annotates
each pack's enablement from the tenant's subscription.

Run:  python backend/test_module_manifest.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import module_manifest
import plan_gate
import platform_routes
import tenancy
from database import Base

# The gated (route -> pack) families the plan-gate enforced before it read the
# manifest. Pinned here so a manifest edit can't silently open/close an endpoint.
_EXPECTED_ROUTES = {
    ("/work-orders", "operations"), ("/production-plans", "operations"),
    ("/production-schedules", "operations"), ("/analytics/production-schedules", "operations"),
    ("/operator", "operations"), ("/analytics/operator-terminal", "operations"),
    ("/maintenance", "factory"), ("/quality", "factory"), ("/inventory", "factory"),
    ("/suppliers", "factory"), ("/purchase-orders", "factory"), ("/twin-overlay", "factory"),
    ("/machine-health", "factory"), ("/remnants", "factory"), ("/issue-slips", "factory"),
    ("/grns", "factory"), ("/cycle-counts", "factory"), ("/bom", "factory"),
    ("/iot", "intelligence"), ("/analytics/iot-command", "intelligence"),
    ("/industrial", "intelligence"), ("/ai", "intelligence"),
    ("/analytics/ai-insights", "intelligence"), ("/copilot", "intelligence"),
    ("/insights", "intelligence"), ("/agent-actions", "intelligence"),
    ("/agent-roster", "intelligence"), ("/agent-policy", "intelligence"),
}


def test_manifest_reproduces_the_plan_gate_route_map():
    assert set(module_manifest.path_packs()) == _EXPECTED_ROUTES
    assert set(plan_gate.PATH_PACKS) == _EXPECTED_ROUTES          # plan_gate reads the manifest
    assert module_manifest.always_open_packs() == {"core", "admin"}
    print("PASS manifest reproduces the plan-gate's gated route map exactly")


# The exact (view_key -> pack) map the dashboard nav renders. The frontend now
# builds its sidebar from GET /modules (module_manifest.packs_for_tenant), so a
# manifest edit that drops, duplicates, or re-packs a view would silently change
# every tenant's nav — pin it here so that can't happen unnoticed.
_EXPECTED_VIEWS = {
    "mission": "core", "overview": "core", "machines": "core", "downtime": "core",
    "shifts": "core", "analytics": "core", "trends": "core", "timeline": "core",
    # CORE, deliberately (ADR-0017). This is where a customer sees and withdraws
    # what a machine's manufacturer may read about their shop floor, and a
    # consent control behind a paywall is not a consent control. Moving it into
    # a gated pack would leave Starter tenants with equipment they cannot see
    # the sharing position of — so the pin is the thing that would catch it.
    "connected": "core",
    "workorders": "operations", "planning": "operations", "scheduling": "operations",
    "operator": "operations", "orders": "operations",
    "maintenance_ai": "factory", "cmms": "factory", "quality": "factory",
    "inventory": "factory", "purchasing": "factory", "digitaltwin": "factory",
    "machinehealth": "factory",
    "iot": "intelligence", "connectivity": "intelligence", "ai": "intelligence",
    "copilot": "intelligence", "inbox": "intelligence", "agentactivity": "intelligence",
    "roi": "intelligence", "executive": "intelligence", "escalations": "intelligence",
    "notifications": "intelligence",
    "documents": "admin", "saas": "admin", "users": "admin", "costing": "admin",
    "enterprise": "admin",
}


def test_manifest_views_reproduce_the_frontend_nav():
    seen = {}
    for pack in module_manifest.packs_for_tenant([]):   # every pack, views carried
        for v in pack["views"]:
            assert v["key"] not in seen, f"duplicate view key {v['key']}"
            assert v["label"] and v["icon"], v            # nav needs a label + icon
            seen[v["key"]] = pack["id"]
    assert seen == _EXPECTED_VIEWS, seen
    print("PASS manifest views reproduce the dashboard nav exactly (view -> pack pinned)")


def test_pack_for_path_routes_unchanged():
    for path, pack in [("/copilot", "intelligence"), ("/ai/ask", "intelligence"),
                       ("/work-orders", "operations"), ("/analytics/production-schedules", "operations"),
                       ("/maintenance/tasks", "factory"), ("/machines", None),
                       ("/overview", None), ("/tenant-config", None)]:
        assert plan_gate.pack_for_path(path) == pack, (path, plan_gate.pack_for_path(path))
    print("PASS pack_for_path resolves the same packs from the manifest")


def test_packs_for_tenant_marks_enablement_and_carries_views():
    packs = {p["id"]: p for p in module_manifest.packs_for_tenant(["core", "operations", "factory"])}
    assert packs["core"]["enabled"] and packs["operations"]["enabled"] and packs["factory"]["enabled"]
    assert not packs["intelligence"]["enabled"] and not packs["admin"]["enabled"]
    copilot = next(v for v in packs["intelligence"]["views"] if v["key"] == "copilot")
    assert copilot["label"] == "AI Copilot"          # view metadata carries through for the nav
    print("PASS packs_for_tenant marks enabled from the subscription + carries view metadata")


def test_modules_endpoint_reflects_tenant_subscription():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    cfg = platform_routes.get_or_create_config(db, "ACME")   # created with all packs by default
    cfg.plan = "growth"
    cfg.enabled_modules = "core,operations,factory"          # a Growth subscription
    db.commit()

    tok = tenancy.set_current_tenant("ACME")
    try:
        res = platform_routes.list_modules(db=db, current_user={"tenant": "ACME"})
    finally:
        tenancy.reset_current_tenant(tok)

    by = {p["id"]: p["enabled"] for p in res["packs"]}
    assert by == {"core": True, "operations": True, "factory": True,
                  "intelligence": False, "admin": False}, by
    assert res["plan"] == "growth" and res["tenant"] == "ACME"
    print("PASS GET /modules reflects the tenant's subscription (intelligence/admin off for Growth)")


def test_plan_bundles_and_valid_pack_ids():
    b = module_manifest.plan_bundles()
    assert b["starter"] == ["core"]
    assert b["growth"] == ["core", "operations", "factory"]
    assert set(b["enterprise"]) == {"core", "operations", "factory", "intelligence", "admin"}
    assert module_manifest.valid_pack_ids() == {"core", "operations", "factory", "intelligence", "admin"}
    print("PASS plan bundles + valid pack ids derive from the manifest")


def test_apply_plan_sets_the_bundle_and_is_founder_only():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    # a client Admin cannot license another company (founder/DEFAULT only)
    try:
        platform_routes.apply_plan("ACME", {"plan": "growth"}, db=db,
                                   current_user={"tenant": "ACME", "sub": "x"})
        assert False, "a client Admin applying a plan cross-tenant should 403"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403, e

    # founder applies Growth -> the tenant gets exactly that manifest bundle
    res = platform_routes.apply_plan("ACME", {"plan": "growth"}, db=db,
                                     current_user={"tenant": "DEFAULT", "sub": "founder"})
    assert res["plan"] == "growth"
    assert res["enabled_modules"] == ["core", "operations", "factory"]

    # an unknown plan is rejected, not silently stored
    try:
        platform_routes.apply_plan("ACME", {"plan": "unlimited"}, db=db,
                                   current_user={"tenant": "DEFAULT", "sub": "founder"})
        assert False, "unknown plan should 400"
    except Exception as e:
        assert getattr(e, "status_code", None) == 400, e
    print("PASS apply-plan sets the manifest bundle; founder-only; rejects unknown plans")


def test_toggle_pack_via_patch_sets_modules_and_is_founder_only():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    platform_routes.get_or_create_config(db, "ACME")

    # a client Admin cannot re-license another company through the cross-tenant path
    try:
        platform_routes.update_any_tenant("ACME", {"enabled_modules": ["core"]}, db=db,
                                          current_user={"tenant": "ACME", "sub": "x"})
        assert False, "a client Admin toggling another tenant's modules should 403"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403, e

    # founder toggles a single pack on -> stored as the enabled_modules CSV, and
    # the plan-gate's cached licence for that tenant is dropped so it applies now
    import plan_gate
    plan_gate._licence_cache["ACME"] = (frozenset({"core"}), 9e18)  # stale cache entry
    res = platform_routes.update_any_tenant(
        "ACME", {"enabled_modules": ["core", "operations", "factory", "intelligence", "admin"]},
        db=db, current_user={"tenant": "DEFAULT", "sub": "founder"})
    assert set(res["enabled_modules"]) == {"core", "operations", "factory", "intelligence", "admin"}
    assert "ACME" not in plan_gate._licence_cache, "plan-gate cache must be invalidated on a licence change"
    print("PASS toggle-pack PATCH sets enabled_modules, invalidates the gate, founder-only")


if __name__ == "__main__":
    test_manifest_reproduces_the_plan_gate_route_map()
    test_manifest_views_reproduce_the_frontend_nav()
    test_pack_for_path_routes_unchanged()
    test_packs_for_tenant_marks_enablement_and_carries_views()
    test_modules_endpoint_reflects_tenant_subscription()
    test_plan_bundles_and_valid_pack_ids()
    test_apply_plan_sets_the_bundle_and_is_founder_only()
    test_toggle_pack_via_patch_sets_modules_and_is_founder_only()
    print("MODULE MANIFEST OK: one source of truth for the plan-gate and the frontend nav")
