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

from sqlalchemy import create_engine, text
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


def test_generator_filters_behind_plans_in_sql_not_python():
    # production_plans is a growing table; only "Behind" plans can raise a delay
    # rec, so the generator must filter status SQL-side rather than hydrate the
    # whole table and test plan.status in Python (the rule-4 antipattern). Guards
    # against a regression back to `db.query(ProductionPlan).all()` + `if
    # plan.status == "Behind"`.
    src = inspect.getsource(recommendations_routes.generate_ai_recommendations)
    assert 'ProductionPlan.status == "Behind"' in src, "plan scan must filter status in SQL"
    assert "db.query(models.ProductionPlan).all()" not in src, (
        "plan scan must not hydrate the whole growing production_plans table"
    )
    print("PASS generator filters Behind plans SQL-side (no whole-table plan scan)")


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


def test_delay_rule_fires_only_for_behind_plans():
    # The production-delay rec is driven by the plan's point-in-time status flag:
    # exactly the plans marked "Behind" raise one, and no other status does. Three
    # plans in — one Behind, one Planned, one Completed — must yield exactly one
    # Production Delay Prediction, for the Behind plan only. This pins the SQL
    # status filter to the same set the old Python `if plan.status == "Behind"`
    # selected (no more, no fewer).
    from datetime import date

    db = _fresh_session()
    db.add(models.Machine(id=7, name="Line-A", status="Running", utilization=80))
    common = dict(machine_id=7, planned_quantity=100, actual_quantity=0,
                  plan_date=date(2026, 7, 1), shift_name="Day")
    db.add(models.ProductionPlan(plan_no="PP-BEHIND", work_order_id=1, status="Behind", **common))
    db.add(models.ProductionPlan(plan_no="PP-PLANNED", work_order_id=2, status="Planned", **common))
    db.add(models.ProductionPlan(plan_no="PP-DONE", work_order_id=3, status="Completed", **common))
    db.commit()

    recommendations_routes.generate_ai_recommendations(db=db, current_user={})

    delay = _by_type(db, "Production Delay Prediction")
    titles = {r.title for r in delay}
    assert titles == {"Delay risk on plan PP-BEHIND"}, titles
    assert delay[0].related_machine_id == 7, delay[0].related_machine_id
    print("PASS delay rule fires for the Behind plan only (Planned/Completed produce none)")


def test_delay_rule_is_edge_safe_with_no_behind_plans():
    # No Behind plans (only other statuses) -> no delay rec, no crash, empty set.
    from datetime import date

    db = _fresh_session()
    db.add(models.ProductionPlan(plan_no="PP-OK", work_order_id=1, machine_id=None,
                                 planned_quantity=50, actual_quantity=50,
                                 plan_date=date(2026, 7, 1), shift_name="Day",
                                 status="Completed"))
    db.commit()
    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    assert _by_type(db, "Production Delay Prediction") == []
    print("PASS delay rule is edge-safe when no plan is Behind")


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


def test_generator_survives_null_utilization_stock_and_quality_columns():
    # Machine.utilization, InventoryItem.current_stock/reorder_level and
    # QualityInspection.failed_quantity are all Column(Integer, default=0) WITHOUT
    # nullable=False, so a raw write / migration / cleared field can leave them
    # NULL. Before the guards, `None < 45`, `None <= None` and `int + None` each
    # raised TypeError and 500-ed /ai/generate-recommendations. Force real NULLs
    # (the ORM default coerces a Python None on insert, so drive them in via SQL)
    # and assert the generator runs and skips the NULL rows rather than crashing.
    db = _fresh_session()
    now = datetime.utcnow()
    # A healthy machine with a real reading so a survivable run still produces the
    # rows it should, alongside the NULL rows that must be skipped.
    db.add(models.Machine(id=1, name="Null-Util", status="Running", utilization=50))
    db.add(models.Machine(id=2, name="Low-Util", status="Running", utilization=30))
    db.add(models.InventoryItem(item_code="NULLSTK", item_name="Ghost", category="C",
                                supplier="S", unit="ea", current_stock=5,
                                reorder_level=2, location="L"))
    db.add(models.InventoryItem(item_code="LOWSTK", item_name="Bolt", category="C",
                                supplier="S", unit="ea", current_stock=1,
                                reorder_level=10, location="L"))
    db.add(models.QualityInspection(inspection_no="Q-NULL", inspector="A",
                                    inspected_quantity=100, failed_quantity=5,
                                    created_at=now))
    db.commit()
    # Clear the columns to a genuine NULL, as raw SQL / a migration would.
    db.execute(text("UPDATE machines SET utilization = NULL WHERE id = 1"))
    db.execute(text("UPDATE inventory_items SET current_stock = NULL, "
                    "reorder_level = NULL WHERE item_code = 'NULLSTK'"))
    db.execute(text("UPDATE quality_inspections SET failed_quantity = NULL "
                    "WHERE inspection_no = 'Q-NULL'"))
    db.commit()
    db.expire_all()

    # No TypeError — the endpoint returns cleanly.
    out = recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    assert isinstance(out, dict) and "created" in out, out

    # The NULL-utilization machine yields no utilization rec (no reading != low),
    # while the genuinely-low one (30%) still does.
    util = _by_type(db, "Utilization Optimization")
    util_titles = {r.title for r in util}
    assert "Low utilization on Null-Util" not in util_titles, util_titles
    assert "Low utilization on Low-Util" in util_titles, util_titles

    # The NULL-stock item is excluded (can't say it's low); the real low one fires.
    inv_titles = {r.title for r in _by_type(db, "Inventory Forecast")}
    assert "Inventory replenishment recommended for NULLSTK" not in inv_titles, inv_titles
    assert "Inventory replenishment recommended for LOWSTK" in inv_titles, inv_titles

    # failed_quantity NULL counts as 0 failures: 0/100 = 0% -> no quality rec.
    assert _by_type(db, "Quality Prediction") == []
    print("PASS generator survives NULL utilization / stock / failed_quantity and "
          "skips those rows instead of 500-ing")


def test_dedupe_blocks_against_a_null_status_open_recommendation():
    # The generator's dedup blocks a duplicate against any NON-closed rec with the
    # same title. status is Column(String, default="Open") WITHOUT nullable=False,
    # so a raw-SQL / migration / cleared-field row can hold a genuine NULL. Before
    # the fix the dedup used a bare `status != 'Closed'`, which is NULL — not TRUE —
    # for a NULL-status row, so it MISSED an existing open rec and raised a DUPLICATE
    # on every regenerate. Force a real NULL (the ORM default coerces a Python None
    # on insert, so drive it in via SQL) and assert the second regenerate adds
    # nothing.
    db = _fresh_session()
    # Shop-floor state that deterministically raises ONE inventory rec titled
    # "Inventory replenishment recommended for BOLT-1" (stock 0 <= reorder 10).
    db.add(models.InventoryItem(item_code="BOLT-1", item_name="Bolt", category="C",
                                supplier="S", unit="ea", current_stock=0,
                                reorder_level=10, location="L"))
    db.commit()

    title = "Inventory replenishment recommended for BOLT-1"

    # First pass creates exactly one open rec for the item.
    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    same_title = [r for r in _recs(db) if r.title == title]
    assert len(same_title) == 1, same_title

    # Simulate the raw-SQL / migration NULL that the ORM default can't retro-apply.
    db.execute(text("UPDATE ai_recommendations SET status = NULL WHERE title = :t"),
               {"t": title})
    db.commit()
    db.expire_all()

    # Second pass must NOT duplicate: the NULL-status rec is still open and blocks it.
    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    same_title = [r for r in _recs(db) if r.title == title]
    assert len(same_title) == 1, (
        f"NULL-status open rec should block the duplicate, got {len(same_title)}"
    )
    print("PASS dedup blocks a duplicate against a NULL-status open recommendation")


def test_dedupe_reopens_after_the_previous_one_is_closed():
    # The dedup only suppresses NON-closed recs — a genuinely Closed rec must let
    # the same finding be raised again (the `!= 'Closed'` semantics the fix
    # preserves; it only widens the filter to also catch NULL). Pins that widening
    # the guard did not accidentally start blocking against a Closed rec too.
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="BOLT-2", item_name="Bolt", category="C",
                                supplier="S", unit="ea", current_stock=0,
                                reorder_level=10, location="L"))
    db.commit()

    title = "Inventory replenishment recommended for BOLT-2"

    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    assert len([r for r in _recs(db) if r.title == title]) == 1

    # Close the first one out; the finding is now allowed to recur.
    db.execute(text("UPDATE ai_recommendations SET status = 'Closed' WHERE title = :t"),
               {"t": title})
    db.commit()
    db.expire_all()

    recommendations_routes.generate_ai_recommendations(db=db, current_user={})
    statuses = sorted(r.status for r in _recs(db) if r.title == title)
    assert statuses == ["Closed", "Open"], statuses
    print("PASS a Closed rec still lets the same finding be raised again")


if __name__ == "__main__":
    test_recommendations_paths_owned_by_module()
    test_generator_is_bounded_to_the_recent_window_in_sql()
    test_generator_filters_behind_plans_in_sql_not_python()
    test_downtime_rule_scores_recent_only_and_parses_hours()
    test_delay_rule_fires_only_for_behind_plans()
    test_delay_rule_is_edge_safe_with_no_behind_plans()
    test_breakdown_now_fires_even_with_no_recent_downtime()
    test_quality_fail_rate_uses_the_recent_window_denominator()
    test_generator_is_edge_safe_on_empty_and_zero_inspection()
    test_generator_survives_null_utilization_stock_and_quality_columns()
    test_dedupe_blocks_against_a_null_status_open_recommendation()
    test_dedupe_reopens_after_the_previous_one_is_closed()
    print("ALL RECOMMENDATION ROUTE TESTS PASSED")
