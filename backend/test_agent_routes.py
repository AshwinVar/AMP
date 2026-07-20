"""Agent-routes registration test (ADR-0004 / ADR-0005).

The agent oversight endpoints live in agent_routes.register(app), peeled out of
main.py. This guards that extraction: every expected agent path is registered
exactly once (per method) and owned by agent_routes — so a future edit can't
silently drop one or reintroduce a shadowing duplicate in main.

Run:  python backend/test_agent_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/agent-actions", "/agent-actions/stats", "/agent-actions/impact",
    "/agent-roster", "/agent-policy", "/agent-roster/{agent_key}",
    "/agent-actions/trend", "/agent-actions/{action_id}/approve",
    "/agent-actions/{action_id}/reject",
}


def test_every_agent_path_owned_by_agent_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"agent paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"agent_routes"}}
    assert not wrong, f"agent paths not owned solely by agent_routes: {wrong}"
    # /agent-policy carries both GET and PUT — same module, still fine
    print(f"PASS all {len(EXPECTED)} agent paths owned by agent_routes")


def test_ops_trends_stayed_in_main():
    # /ops-trends is a cross-pillar trends read-model, not agent oversight — it
    # deliberately stayed in main (a candidate for a future read-model pass).
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/ops-trends") == "main"
    print("PASS /ops-trends stayed in main")


if __name__ == "__main__":
    test_every_agent_path_owned_by_agent_routes()
    test_ops_trends_stayed_in_main()
    print("ALL AGENT ROUTE TESTS PASSED")
