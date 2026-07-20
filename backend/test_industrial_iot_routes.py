"""Industrial-IoT route registration test (ADR-0009).

IoT telemetry + industrial devices / signals / PLC mappings live in
industrial_iot_routes.register(app), peeled out of main.py. Guards registration
+ sole ownership by the module.

Run:  python backend/test_industrial_iot_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/iot/telemetry",
    "/industrial/devices",
    "/industrial/devices/{device_id}",
    "/industrial/signals",
    "/industrial/mappings",
}


def test_industrial_iot_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"industrial-iot paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"industrial_iot_routes"}}
    assert not wrong, f"industrial-iot paths not owned solely by industrial_iot_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} industrial-iot paths owned by industrial_iot_routes")


if __name__ == "__main__":
    test_industrial_iot_paths_owned_by_module()
    print("ALL INDUSTRIAL-IOT ROUTE TESTS PASSED")
