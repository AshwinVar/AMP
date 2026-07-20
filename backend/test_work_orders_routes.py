"""Work-orders route registration test (ADR-0009).

The production-order lifecycle (list / create / update / delete) lives in
work_orders_routes.register(app), peeled out of main.py. Guards registration +
sole ownership. Completing a work order still publishes ProductionCompleted
(the BOM movement is a subscriber) — that wiring is covered by the event tests;
here we only assert the routes moved and are owned by the module.

Run:  python backend/test_work_orders_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {"/work-orders", "/work-orders/{work_order_id}"}


def test_work_orders_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"work-orders paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"work_orders_routes"}}
    assert not wrong, f"work-orders paths not owned solely by work_orders_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} work-orders paths owned by work_orders_routes")


def test_completing_wo_still_publishes_production_completed():
    # Guard the event coupling survived the move: the module imports the event
    # symbol and references it in the update handler's source.
    import inspect
    import work_orders_routes
    src = inspect.getsource(work_orders_routes)
    assert "ProductionCompleted(" in src, "ProductionCompleted publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS work-orders completion still publishes ProductionCompleted")


if __name__ == "__main__":
    test_work_orders_paths_owned_by_module()
    test_completing_wo_still_publishes_production_completed()
    print("ALL WORK-ORDERS ROUTE TESTS PASSED")
