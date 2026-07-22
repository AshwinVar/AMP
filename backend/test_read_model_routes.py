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
    "/copilot/ask", "/copilot/digest", "/recovery-summary", "/reliability-summary",
    "/schedule-summary", "/coverage-summary", "/inventory-part",
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


def test_mutating_and_stateful_endpoints_owned_by_core():
    # These are not read-model projections: /briefing/escalate mutates (an agent
    # proposes an escalation) and /platform/status reports the sim heartbeat. They
    # were never read_model_routes; they now live in core_routes, grouped with the
    # other main stragglers.
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/briefing/escalate") == "core_routes"
    assert owners.get("/platform/status") == "core_routes"
    print("PASS mutating / stateful endpoints owned by core_routes")


def test_read_model_and_copilot_authenticated_at_router_level():
    # Both modules are uniformly get_current_user, so the auth gate is hoisted
    # onto their APIRouter (dependencies=[Depends(get_current_user)]). Every route
    # they own — current and future — must carry it, so a new read endpoint can't
    # ship unauthenticated by omission.
    for mod in ("read_model_routes", "ai_copilot"):
        routes = [r for r in main.app.routes
                  if getattr(r, "endpoint", None) and r.endpoint.__module__ == mod]
        assert routes, f"no routes registered from {mod}"
        for r in routes:
            names = [getattr(d.call, "__name__", "") for d in r.dependant.dependencies]
            assert "get_current_user" in names, \
                f"{mod} {r.path} {r.methods} lost the router-level auth gate"
        print(f"PASS all {len(routes)} {mod} routes require authentication (router-level)")


if __name__ == "__main__":
    test_every_read_model_path_registered_once_from_the_module()
    test_mutating_and_stateful_endpoints_owned_by_core()
    test_read_model_and_copilot_authenticated_at_router_level()
    print("ALL READ-MODEL ROUTE TESTS PASSED")
