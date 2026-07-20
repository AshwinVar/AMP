"""Machine & telemetry route registration test (ADR-0009).

The MES-core CRUD (machines, downtime, shifts, production records, machine
events) lives in machines_routes.register(app), peeled out of main.py. Guards
that every expected path is registered and owned by machines_routes — including
the two behaviour-carrying endpoints (status change writes a MachineEvent;
downtime POST publishes DowntimeStarted).

Run:  python backend/test_machines_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/machines", "/machines/{machine_id}", "/machines/{machine_id}/status",
    "/downtime-logs", "/shifts", "/production-records", "/machine-events",
}


def test_telemetry_paths_owned_by_machines_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"telemetry paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"machines_routes"}}
    assert not wrong, f"telemetry paths not owned solely by machines_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} telemetry paths owned by machines_routes")


if __name__ == "__main__":
    test_telemetry_paths_owned_by_machines_routes()
    print("ALL MACHINE ROUTE TESTS PASSED")
