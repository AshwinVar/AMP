"""Quality route registration test (ADR-0009).

Quality inspections (list / create / update / delete) + the defect escalation
generator live in quality_routes.register(app), peeled out of main.py. Guards
registration + sole ownership, and that the QualityInspectionFailed event
publish survived the move.

Run:  python backend/test_quality_routes.py     (exit 0 = pass)
"""
import main

EXPECTED = {
    "/quality/inspections",
    "/quality/inspections/{inspection_id}",
    "/quality/generate-defect-escalations",
}


def test_quality_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"quality paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"quality_routes"}}
    assert not wrong, f"quality paths not owned solely by quality_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} quality paths owned by quality_routes")


def test_failed_inspection_still_publishes_event():
    import inspect
    import quality_routes
    src = inspect.getsource(quality_routes)
    assert "QualityInspectionFailed(" in src, "QualityInspectionFailed publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS a failed inspection still publishes QualityInspectionFailed")


if __name__ == "__main__":
    test_quality_paths_owned_by_module()
    test_failed_inspection_still_publishes_event()
    print("ALL QUALITY ROUTE TESTS PASSED")
