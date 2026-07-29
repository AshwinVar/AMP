"""Core route registration test (ADR-0009).

The irreducible endpoints that never fit a domain module — auth/bootstrap, the
AI-platform self-report, the BOM view, and the intelligence stragglers — now
live behind core_routes.register... (an APIRouter tagged "Core"), the last group
peeled off `app` in main.py. Guards registration + sole ownership, and the
invariant that main.py no longer defines ANY HTTP route itself (only the
lifecycle websocket remains).

Run:  python backend/test_core_routes.py     (exit 0 = pass)
"""
import main

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from core_routes import create_escalations_from_smart_alerts

EXPECTED = {
    "/", "/me", "/register", "/login", "/auth/refresh", "/auth/change-password",
    "/platform/status", "/bom", "/briefing/escalate",
    "/escalations/from-smart-alerts", "/reports/daily-summary.txt", "/ops-trends",
}


def test_core_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"core paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"core_routes"}}
    assert not wrong, f"core paths not owned solely by core_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} core paths owned by core_routes")


def test_main_defines_no_http_routes():
    # The whole point: after this peel-off, main.py assembles the app and owns
    # only lifecycle bits (sim loop, startup, the /ws/live websocket). No HTTP
    # route may be defined directly on `app` in main anymore.
    http_in_main = sorted({
        r.path for r in main.app.routes
        if hasattr(r, "methods") and getattr(r, "endpoint", None)
        and r.endpoint.__module__ == "main"
    })
    assert not http_in_main, f"main.py still defines HTTP routes: {http_in_main}"
    print("PASS main.py defines no HTTP routes (only the lifecycle websocket remains)")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_smart_alert_null_status_existing_escalation_is_deduped_not_duplicated():
    """create_escalations_from_smart_alerts must treat a NULL-status EXISTING
    escalation as still open, matching the "NULL is not terminal" convention every
    open-escalation reader uses (#295/#403). status is String default="Open" (not
    NOT NULL), so a raw-SQL / migration / cleared write can hold a real NULL; SQL's
    `status != 'Resolved'` is NULL — not TRUE — for it, so the bare predicate MISSED
    the open escalation and raised a DUPLICATE, inflating the very open-escalation
    count the readers report. Regression guard for the or_(status IS NULL, ...) fix."""
    db = _fresh_session()
    # A machine in breakdown makes generate_alerts emit a "Breakdown" alert, whose
    # escalation title is "Breakdown - <machine name>". utilization is kept >= 50 so
    # the low-utilization alert (a different, legitimately-new title) doesn't also
    # fire — this test isolates the Breakdown alert's dedup.
    db.add(models.Machine(name="CNC-1", status="Breakdown", utilization=60))
    title = "Breakdown - CNC-1"
    db.add(models.Escalation(
        title=title, severity="High", owner="Unassigned", department="Maintenance",
        status="Open", source="Smart Alert"))
    db.commit()
    db.execute(text("UPDATE escalations SET status = NULL WHERE title = :t"), {"t": title})
    db.commit()
    db.expire_all()

    out = create_escalations_from_smart_alerts(db=db, current_user={"tenant": "DEFAULT"})
    assert out == {"created": 0}, out                     # NULL-status one is still open
    assert db.query(models.Escalation).count() == 1       # no duplicate raised
    print("PASS smart-alert: a NULL-status open escalation is deduped, not duplicated")


if __name__ == "__main__":
    test_core_paths_owned_by_module()
    test_main_defines_no_http_routes()
    test_smart_alert_null_status_existing_escalation_is_deduped_not_duplicated()
    print("ALL CORE ROUTE TESTS PASSED")
