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
    # A pack in no plan. It gates exactly like the others; what makes it
    # per-customer is `plans: []`, asserted in its own test below.
    ("/analytics/plant-board", "plantboard"),
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
    # CORE, and deliberately not Factory. The plant board is hourly output
    # against target, material consumed and the shift's money rate — it is what
    # a plant watches, not an advanced analysis, and it is drawn from the same
    # production records every Starter tenant already has. Putting it behind a
    # pack would leave the smallest customers, who are exactly the ones with
    # nobody on the floor to read a report, with the counts and none of the
    # pictures.
    "plantboard": "plantboard",
    # CORE, deliberately (ADR-0017). This is where a customer sees and withdraws
    # what a machine's manufacturer may read about their shop floor, and a
    # consent control behind a paywall is not a consent control. Moving it into
    # a gated pack would leave Starter tenants with equipment they cannot see
    # the sharing position of — so the pin is the thing that would catch it.
    "connected": "core",
    # CORE too (ADR-0021). Service contracts are where a factory reviews what a
    # manufacturer proposes, grants SHARE_DOWNTIME by accepting, and disputes a
    # statement. Like consent itself, the right to refuse or dispute terms that
    # bind you cannot sit behind a paywall. /service-contracts is not in any
    # gated pack's routes, so the plan gate never blocks it (pinned below).
    "contracts": "core",
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
                       ("/overview", None), ("/tenant-config", None),
                       ("/service-contracts", None), ("/service-contracts/7/accept", None)]:
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
                  "intelligence": False, "admin": False,
                  # A per-customer pack is OFF for a tenant that was not
                  # explicitly granted it, however high their plan.
                  "plantboard": False}, by
    assert res["plan"] == "growth" and res["tenant"] == "ACME"
    print("PASS GET /modules reflects the tenant's subscription (intelligence/admin off for Growth)")


def test_a_per_customer_pack_is_granted_per_tenant_and_never_by_plan():
    """The mechanism for a feature built for ONE customer.

    The plant board was written for one plant. Before this it lived in `core`,
    which is ungated and in every plan, so every tenant AMP ever signs would
    have received it -- and its endpoint was not gated at all. A pack with
    `plans: []` inverts that: nothing provisions it, and it arrives only when
    its id is put in a tenant's enabled_modules by hand.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    # The highest plan there is, applied the way the SaaS admin applies it.
    top = platform_routes.get_or_create_config(db, "RICHCO")
    top.plan = "enterprise"
    top.enabled_modules = module_manifest.plan_modules("enterprise")
    db.commit()
    assert "plantboard" not in module_manifest.enabled_pack_ids(top.enabled_modules)

    licensed = module_manifest.enabled_pack_ids(top.enabled_modules)
    assert not module_manifest.pack_licensed("plantboard", licensed),         "an enterprise subscription must not reach a per-customer pack"

    # The one tenant it was built for, granted by hand.
    theirs = platform_routes.get_or_create_config(db, "SHRINIDHI")
    theirs.plan = "starter"
    theirs.enabled_modules = module_manifest.plan_modules("starter") + ",plantboard"
    db.commit()
    mine = module_manifest.enabled_pack_ids(theirs.enabled_modules)
    assert module_manifest.pack_licensed("plantboard", mine),         "the tenant it was granted to must reach it, even on the cheapest plan"

    # And the gate agrees, because both resolve through the same manifest.
    assert plan_gate.pack_for_path("/analytics/plant-board") == "plantboard"
    assert plan_gate.pack_for_path("/analytics/plant-board/month") == "plantboard"
    assert not module_manifest.pack_licensed("plantboard", frozenset()),         "a per-customer pack must not be always-open"
    print("PASS a per-customer pack is granted per tenant and never by plan")


def test_plan_bundles_and_valid_pack_ids():
    b = module_manifest.plan_bundles()
    assert b["starter"] == ["core"]
    assert b["growth"] == ["core", "operations", "factory"]
    assert set(b["enterprise"]) == {"core", "operations", "factory", "intelligence", "admin"}
    assert module_manifest.valid_pack_ids() == {
        "core", "operations", "factory", "intelligence", "admin", "plantboard"}
    # THE POINT OF A PER-CUSTOMER PACK: it is in no plan's bundle, so no
    # subscription -- not even enterprise or demo -- ever provisions it.
    for plan, bundle in b.items():
        assert "plantboard" not in bundle, (plan, bundle)
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
