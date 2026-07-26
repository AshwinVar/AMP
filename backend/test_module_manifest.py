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


if __name__ == "__main__":
    test_manifest_reproduces_the_plan_gate_route_map()
    test_pack_for_path_routes_unchanged()
    test_packs_for_tenant_marks_enablement_and_carries_views()
    test_modules_endpoint_reflects_tenant_subscription()
    print("MODULE MANIFEST OK: one source of truth for the plan-gate and the frontend nav")
