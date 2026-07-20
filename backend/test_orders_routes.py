"""Orders & procurement route registration test (ADR-0009).

The order-to-procurement CRUD (customer orders, suppliers, purchase orders,
their analytics, CSV export, escalation generation) lives in
orders_routes.register(app), peeled out of main.py. Guards registration +
ownership. (The CSV export helper is exercised by test_orders_export.py.)

Run:  python backend/test_orders_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/customer-orders", "/customer-orders/export", "/customer-orders/{order_id}",
    "/analytics/customer-orders", "/customer-orders/generate-late-order-escalations",
    "/suppliers", "/suppliers/{supplier_id}",
    "/purchase-orders", "/purchase-orders/{po_id}", "/analytics/purchasing",
    "/purchase-orders/generate-overdue-escalations",
}


def test_procurement_paths_owned_by_orders_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"procurement paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"orders_routes"}}
    assert not wrong, f"procurement paths not owned solely by orders_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} procurement paths owned by orders_routes")


if __name__ == "__main__":
    test_procurement_paths_owned_by_orders_routes()
    print("ALL ORDERS ROUTE TESTS PASSED")
