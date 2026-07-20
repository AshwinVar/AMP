"""Inventory route registration test (ADR-0009).

Stock items + the transaction ledger + the low-stock escalation generator live
in inventory_routes.register(app), peeled out of main.py. Guards registration +
sole ownership, and that the InventoryLow event publish survived the move.

Note: /inventory/enterprise/* and the GMATS inventory paths are owned by their
own modules (enterprise_inventory_routes, gmats_inventory_routes) and are not
asserted here — this guards only the base /inventory CRUD paths.

Run:  python backend/test_inventory_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/inventory/items",
    "/inventory/items/{item_id}",
    "/inventory/transactions",
    "/inventory/generate-low-stock-escalations",
}


def test_inventory_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"inventory_routes"}}
    assert not wrong, f"inventory paths not owned solely by inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} inventory paths owned by inventory_routes")


def test_low_stock_still_publishes_inventory_low():
    import inspect
    import inventory_routes
    src = inspect.getsource(inventory_routes)
    assert "InventoryLow(" in src, "InventoryLow publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS recording a transaction still publishes InventoryLow")


if __name__ == "__main__":
    test_inventory_paths_owned_by_module()
    test_low_stock_still_publishes_inventory_low()
    print("ALL INVENTORY ROUTE TESTS PASSED")
