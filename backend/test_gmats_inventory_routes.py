"""GMATS-inventory route registration guard.

gmats_inventory_routes predates the ADR-0009 guard-test discipline. It owns the
GMATS tenant's enterprise-inventory surface — items (+ aliases / correct /
stock-in), the 4-bucket summary, resolve, MINs, proformas (cancel / invoice),
invoices, and CSV import. Assert every path is registered exactly once and owned
solely by the module.

Run:  python backend/test_gmats_inventory_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/gmats/items", "/gmats/items/{item_id}", "/gmats/items/{item_id}/aliases",
    "/gmats/items/{item_id}/correct", "/gmats/items/{item_id}/stock-in",
    "/gmats/summary", "/gmats/resolve", "/gmats/import-csv",
    "/gmats/min", "/gmats/min/{min_id}",
    "/gmats/proformas", "/gmats/proformas/{pid}/cancel", "/gmats/proformas/{pid}/invoice",
    "/gmats/invoices", "/gmats/invoices/{inv_id}",
}


def test_gmats_inventory_paths_registered_once_and_owned():
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"gmats-inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"gmats_inventory_routes"}}
    assert not wrong, f"paths not owned solely by gmats_inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} gmats-inventory paths owned by the module")


if __name__ == "__main__":
    test_gmats_inventory_paths_registered_once_and_owned()
    print("ALL GMATS-INVENTORY ROUTE TESTS PASSED")
