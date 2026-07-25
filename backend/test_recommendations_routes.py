"""AI-recommendation route registration + generator behaviour test (ADR-0009).

The copilot's suggestion queue (list / update / regenerate) lives in
recommendations_routes.register(app), peeled out of main.py. Guards registration
+ sole ownership by the module, plus the /ai/generate-recommendations rules pass:
its two history-based signals (accumulated downtime, quality fail rate) score the
RECENT window, bounded SQL-side, not lifetime accumulation.

Run:  python backend/test_recommendations_routes.py     (exit 0 = pass)
"""
import inspect
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
import recommendations_routes
from database import Base

EXPECTED = {
    "/ai/recommendations",
    "/ai/recommendations/{recommendation_id}",
    "/ai/generate-recommendations",
}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _recs(db):
    return db.query(models.AIRecommendation).all()


def _by_type(db, kind):
    return [r for r in _recs(db) if r.recommendation_type == kind]


def test_recommendations_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"recommendation paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"recommendations_routes"}}
    assert not wrong, f"recommendation paths not owned solely by recommendations_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} recommendation paths owned by recommendations_routes")


def test_generator_is_bounded_to_the_recent_window_in_sql():
    # Guards against a regression back to an unbounded .all() lifetime scan of the
    # growing downtime_logs / quality_inspections tables.
    src = inspect.getsource(recommendations_routes.generate_ai_recommendations)
    assert "DowntimeLog.created_at >= cutoff" in src, "downtime scan must be windowed in SQL"
    assert "QualityInspection.created_at >= cutoff" in src, "quality scan must be windowed in SQL"
    print("PASS generator windows its downtime + quality scans SQL-side")


def test_downtime_rule_scores_recent_only_and_parses_hours():
    # Machine 1: two RECENT downtime logs, one in hour format. 2 hrs 15 min = 135
    # and 1 hr = 60 -> 195 recent minutes, which is > 120 -> a maintenance rec.
    # If the hour format were misread ("2 hrs 15 min" -> 2), recent minutes would
    # be 2 + 60 = 62 and NO rec would fire — so the number pins the shared parser.
    db = _fresh_session()
    now = datetime.utcnow()
    old = now - timedelta(days=45)
    db.add(models.Machine(id=1, name="Press-1", status="Running", utilization=80))
    # Machine 2: only OLD downtime (well over 120) but Running now — windowed out.
    db.add(models.Machine(id=2, name="Press-2", status="Running", utilization=80))
    db.add(models.DowntimeLog(machine_id=1, reason="Breakdown", duration="2 hrs 15 min", created_at=now))
    db.add(models.DowntimeLog(machine_id=1, reason="Setup", duration="1 hr", created_at=now))
    db.add(models.DowntimeLog(machine_id=2, reason="Breakdown", duration="10 hrs", created_at=old))
    db.commit()

    recommendations_routes.generate_ai_recommendations(db=db, current_user={})

    maint = _by_type(db, "Predictive Maintenance")
    titles = {r.title for r in maint}
    assert "Maintenance risk detected on Press-1" in titles, titles
    # Press-2's only downtime is 45 days old -> excluded -> no rec for it.
    assert "Maintenance risk detected on Press-2" not in titles, titles
    p1 = next(r for r in maint if r.related_machine_id == 1)
    assert "195 minutes downtime in the last 30 days" in p1.message, p1.message
    print("PASS downtime rule scores the recent window and parses '2 hrs 15 min' as 135")


def test_breakdown_now_fires_even_with_no_recent_downtime():
    # Point-in-time breakdown state fires regardless of the (empty) downtime window.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Down-Now", status="Breakdown", utilization=80))
    db.commit()
    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    maint = _by_type(db, "Predictive Maintenance")
    assert any(r.related_machine_id == 1 for r in maint)
    msg = next(r for r in maint if r.related_machine_id == 1).message
    assert "0 minutes downtime in the last 30 days or is in breakdown state" in msg, msg
    print("PASS a current breakdown fires even with an empty recent-downtime window")


def test_quality_fail_rate_uses_the_recent_window_denominator():
    # Recent window: 12 failed / 100 inspected = 12% >= 10 -> a quality rec.
    # A large clean OLD batch (0/1000) would drag the LIFETIME rate to
    # 12/1100 ~= 1% and suppress the rec — so this pins the windowed denominator.
    db = _fresh_session()
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    db.add(models.QualityInspection(inspection_no="Q-NEW", inspector="A",
                                    inspected_quantity=100, failed_quantity=12, created_at=now))
    db.add(models.QualityInspection(inspection_no="Q-OLD", inspector="A",
                                    inspected_quantity=1000, failed_quantity=0, created_at=old))
    db.commit()

    recommendations_routes.generate_ai_recommendations(db=db, current_user={})

    q = _by_type(db, "Quality Prediction")
    assert len(q) == 1, q
    assert "Fail rate over the last 30 days is 12%" in q[0].message, q[0].message
    print("PASS quality fail rate reconciles to the recent window (12%), not the lifetime 1%")


def test_generator_is_edge_safe_on_empty_and_zero_inspection():
    # Empty DB: no crash, nothing created.
    db = _fresh_session()
    out = recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    assert out == {"created": 0}, out
    assert _recs(db) == []

    # A machine plus a zero-inspected-quantity row in-window: no divide-by-zero,
    # no quality rec.
    db2 = _fresh_session()
    db2.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db2.add(models.QualityInspection(inspection_no="Q-ZERO", inspector="A",
                                     inspected_quantity=0, failed_quantity=0,
                                     created_at=datetime.utcnow()))
    db2.commit()
    recommendations_routes.generate_ai_recommendations(db=db2, current_user={})
    assert _by_type(db2, "Quality Prediction") == []
    print("PASS generator is edge-safe on empty data and zero-inspection denominators")


if __name__ == "__main__":
    test_recommendations_paths_owned_by_module()
    test_generator_is_bounded_to_the_recent_window_in_sql()
    test_downtime_rule_scores_recent_only_and_parses_hours()
    test_breakdown_now_fires_even_with_no_recent_downtime()
    test_quality_fail_rate_uses_the_recent_window_denominator()
    test_generator_is_edge_safe_on_empty_and_zero_inspection()
    print("ALL RECOMMENDATION ROUTE TESTS PASSED")
