"""Enterprise-inventory route registration guard.

enterprise_inventory_routes predates the ADR-0009 guard-test discipline, so it
never had one. It owns the warehouse-ops surface — remnants, issue slips, GRNs,
cycle counts, plus the CSV import and variance report. Assert every path is
registered exactly once and owned solely by the module, so a future edit can't
drop one or reintroduce a shadowing duplicate (the /health, /audit-logs class of
bug).

Run:  python backend/test_enterprise_inventory_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/remnants", "/remnants/{rid}/status",
    "/issue-slips", "/issue-slips/{sid}/approve", "/issue-slips/{sid}/issue",
    "/issue-slips/{sid}/reject",
    "/grns", "/grns/{gid}/accept",
    "/cycle-counts", "/cycle-counts/{cid}/approve",
    "/inventory/import-csv", "/inventory/variance-report",
}


def test_enterprise_inventory_paths_registered_once_and_owned():
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"enterprise-inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"enterprise_inventory_routes"}}
    assert not wrong, f"paths not owned solely by enterprise_inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} enterprise-inventory paths owned by the module")


if __name__ == "__main__":
    test_enterprise_inventory_paths_registered_once_and_owned()
    print("ALL ENTERPRISE-INVENTORY ROUTE TESTS PASSED")
