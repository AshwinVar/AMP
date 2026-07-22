"""Factory-ops route registration test (ADR-0009).

Escalations, factory layout, documents, maintenance, notifications CRUD live in
factory_ops_routes.register(app), peeled out of main.py. Guards registration +
ownership. /escalations/from-smart-alerts deliberately stays in main (it shares
main's generate_alerts helper).

Run:  python backend/test_factory_ops_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/escalations", "/escalations/{escalation_id}", "/escalations/generate-oee-recovery",
    "/factory-layout/nodes", "/factory-layout/nodes/{node_id}", "/factory-layout/auto-generate",
    "/documents", "/documents/{document_id}", "/documents/generate-review-escalations",
    "/maintenance/tasks", "/maintenance/tasks/{task_id}", "/maintenance/generate-overdue-escalations",
    "/notifications", "/notifications/{notification_id}", "/notifications/generate-system-notifications",
}


def test_factory_ops_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"factory-ops paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"factory_ops_routes"}}
    assert not wrong, f"factory-ops paths not owned solely by factory_ops_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} factory-ops paths owned by factory_ops_routes")


def test_from_smart_alerts_owned_by_core_routes():
    # /escalations/from-smart-alerts uses generate_alerts (now in analytics_engine),
    # not the factory-ops escalation CRUD. It was left in main and is now grouped
    # into core_routes with the other stragglers.
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/escalations/from-smart-alerts") == "core_routes"
    print("PASS /escalations/from-smart-alerts is owned by core_routes (not factory_ops)")


if __name__ == "__main__":
    test_factory_ops_paths_owned_by_module()
    test_from_smart_alerts_owned_by_core_routes()
    print("ALL FACTORY-OPS ROUTE TESTS PASSED")
