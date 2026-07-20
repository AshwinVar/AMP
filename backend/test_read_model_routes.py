"""Read-model route registration test (ADR-0007).

The read-model projection endpoints live in read_model_routes.register(app),
peeled out of main.py. This guards that extraction: every expected read-model
path is registered exactly once and owned by read_model_routes — so a future
edit can't silently drop one or reintroduce a shadowing duplicate in main
(the class of bug fixed in the /health dedup).

Run:  python backend/test_read_model_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/insights", "/machine-health", "/mission-control/pulse",
    "/downtime-summary", "/downtime-reason", "/quality-summary", "/quality-defect",
    "/production-summary", "/oee-summary", "/inventory-summary", "/flow-summary",
    "/shift-summary", "/losses-summary", "/briefing", "/delivery-summary",
    "/cost-summary", "/handover", "/scorecard", "/twin-overlay",
    "/maintenance-summary", "/compliance-summary", "/search", "/weekly-report",
    "/copilot/ask", "/copilot/digest",
}


def test_every_read_model_path_registered_once_from_the_module():
    owners = {}
    counts = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners[p] = r.endpoint.__module__

    missing = EXPECTED - set(counts)
    assert not missing, f"read-model paths not registered: {missing}"
    dups = {p: n for p, n in counts.items() if n != 1}
    assert not dups, f"read-model paths registered more than once (shadowing risk): {dups}"
    wrong = {p: m for p, m in owners.items() if m != "read_model_routes"}
    assert not wrong, f"read-model paths not owned by read_model_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} read-model paths registered once, from read_model_routes")


def test_mutating_and_stateful_endpoints_stayed_in_main():
    # These deliberately did NOT move: /briefing/escalate mutates (agent proposes
    # an escalation), /platform/status reads main-local sim globals.
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/briefing/escalate") == "main"
    assert owners.get("/platform/status") == "main"
    print("PASS mutating / stateful endpoints stayed in main")


if __name__ == "__main__":
    test_every_read_model_path_registered_once_from_the_module()
    test_mutating_and_stateful_endpoints_stayed_in_main()
    print("ALL READ-MODEL ROUTE TESTS PASSED")
