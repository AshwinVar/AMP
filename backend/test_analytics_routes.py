"""Analytics/alerts route registration test (ADR-0009).

The dashboard read-model / intelligence surface (OEE + alerts summary, per-page
command centres, executive rollups, predictive maintenance, machine health)
lives in analytics_routes.register(app), peeled out of main.py. Guards
registration + sole ownership of the 27 moved paths.

analytics_summary is module-level (main's /reports/daily-summary.txt imports it
directly), so it's asserted importable both from analytics_routes and re-exported
on main. Compute is imported from the shared engines — the module must not carry
its own copy of the relocated helpers.

Run:  python backend/test_analytics_routes.py     (exit 0 = pass)
"""
import inspect
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
import core_routes
import analytics_routes
import models
from analytics_engine import pooled_oee
from database import Base

EXPECTED = {
    "/oee/summary",
    "/alerts", "/alerts/smart",
    "/machine-health/{machine_id}",
    "/analytics/summary",
    "/analytics/machine-timeline", "/analytics/machine-state-summary",
    "/analytics/oee-trends", "/analytics/shift-kpis", "/analytics/management",
    "/analytics/predictive-maintenance",
    "/analytics/work-orders", "/analytics/production-plans",
    "/analytics/escalations", "/analytics/inventory", "/analytics/quality",
    "/analytics/executive-oee", "/analytics/factory-command-center",
    "/analytics/documents", "/analytics/maintenance",
    "/analytics/production-schedules", "/analytics/iot-command",
    "/analytics/ai-insights", "/analytics/operator-terminal",
    "/analytics/system-health", "/analytics/final-executive-summary",
    "/analytics/industrial-gateway",
}


def test_analytics_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"analytics paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"analytics_routes"}}
    assert not wrong, f"analytics paths not owned solely by analytics_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} analytics paths owned by analytics_routes")


def test_analytics_summary_is_module_level_and_shared():
    import analytics_routes
    import core_routes
    assert callable(getattr(analytics_routes, "analytics_summary", None)), \
        "analytics_summary must be a module-level function (core_routes' daily-summary.txt imports it)"
    # core_routes owns /reports/daily-summary.txt and imports the same function.
    assert getattr(core_routes, "analytics_summary", None) is analytics_routes.analytics_summary, \
        "core_routes must import the same analytics_summary it delegates daily-summary.txt to"
    dsr = inspect.getsource(core_routes.daily_summary_report)
    assert "analytics_summary(" in dsr, "daily-summary.txt should still call analytics_summary"
    print("PASS analytics_summary is module-level, shared with main's daily-summary.txt")


def test_module_has_no_relocated_helper_copies():
    import analytics_routes
    src = inspect.getsource(analytics_routes)
    for helper in ("def generate_alerts", "def calculate_fallback_oee",
                   "def parse_duration_to_minutes", "def calculate_oee_from_record"):
        assert helper not in src, f"{helper} must be imported from analytics_engine, not redefined"
    print("PASS analytics_routes imports its compute from analytics_engine (no local copies)")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_executive_oee_plant_rollup_is_pooled_not_a_per_machine_mean():
    # A high-volume machine running poorly + a tiny perfect run. Averaging the two
    # machines' OEE (mean of ratios) over-weights the tiny perfect run; pooling
    # weights by volume — and it must match /oee-summary's pooled definition.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Running", utilization=80))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    db.commit()

    out = analytics_routes.get_executive_oee(db=db, current_user={})
    expected = pooled_oee(db.query(models.ProductionRecord).all())
    assert out["plant_oee"] == expected["oee"]
    assert out["plant_availability"] == expected["availability"]
    assert out["plant_performance"] == expected["performance"]
    assert out["plant_quality"] == expected["quality"]

    # and it is emphatically NOT the mean of the per-machine ranking (Big 20 +
    # Tiny 100) / 2 = 60 — the number the endpoint used to report.
    ranking = out["machine_ranking"]
    mean_oee = round(sum(r["oee"] for r in ranking) / len(ranking))
    assert out["plant_oee"] != mean_oee and out["plant_oee"] == expected["oee"] < mean_oee, (out["plant_oee"], mean_oee)
    print(f"PASS executive-oee plant rollup is pooled ({out['plant_oee']}%), not the per-machine mean ({mean_oee}%)")


def test_executive_oee_no_production_is_zero_not_fabricated():
    # No production records -> pooled has no data -> plant OEE is 0, not a number
    # invented from per-machine fallback constants.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db.commit()
    out = analytics_routes.get_executive_oee(db=db, current_user={})
    assert out["plant_oee"] == 0 and out["plant_availability"] == 0
    print("PASS executive-oee reports 0 plant OEE on no production (no fabricated number)")


def _work_order(**kw):
    kw.setdefault("part_number", "PN")
    kw.setdefault("batch_number", "BN")
    return models.WorkOrder(**kw)


def test_work_order_analytics_null_actual_and_reconciled_totals():
    # actual_quantity is Column(Integer, default=0) without nullable=False, so a
    # NULL slips in via raw SQL / a cleared update. The old sum(...) did int + None
    # and 500'd; COALESCE(SUM,0) must treat the NULL as 0. Numbers are derived by
    # hand, not read back from the endpoint.
    db = _fresh_session()
    db.add(_work_order(id=1, work_order_no="W1", status="Running", target_quantity=100, actual_quantity=50))
    db.add(_work_order(id=2, work_order_no="W2", status="Planned", target_quantity=200, actual_quantity=None))
    db.add(_work_order(id=3, work_order_no="W3", status="Completed", target_quantity=300, actual_quantity=300))
    db.add(_work_order(id=4, work_order_no="W4", status="Delayed", target_quantity=50, actual_quantity=10))
    db.commit()

    out = analytics_routes.get_work_order_analytics(db=db, current_user={})
    assert out["total_work_orders"] == 4, out
    assert out["planned"] == 1 and out["running"] == 1
    assert out["completed"] == 1 and out["delayed"] == 1
    # target: 100+200+300+50 = 650 (target_quantity is NOT NULL, all counted)
    assert out["total_target"] == 650, out
    # actual: 50 + 0(NULL->0) + 300 + 10 = 360 — NOT a crash, NOT dropping the row
    assert out["total_actual"] == 360, out
    # achievement = round(360/650*100) = round(55.38) = 55
    assert out["achievement"] == 55, out
    # the four status buckets reconcile with the count of the four named statuses
    assert out["planned"] + out["running"] + out["completed"] + out["delayed"] == 4
    print("PASS work-order analytics: NULL actual -> 0, totals reconcile (650 target / 360 actual / 55%)")


def test_work_order_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_work_order_analytics(db=db, current_user={})
    assert out["total_work_orders"] == 0
    assert out["total_target"] == 0 and out["total_actual"] == 0
    # zero denominator -> 0, not a ZeroDivisionError
    assert out["achievement"] == 0
    print("PASS work-order analytics: empty table -> zeros, no divide-by-zero")


def test_production_plan_analytics_null_actual_and_reconciled_totals():
    db = _fresh_session()
    db.add(models.ProductionPlan(id=1, plan_no="P1", status="Running", planned_quantity=100,
                                 actual_quantity=80, plan_date=date(2026, 1, 1), shift_name="A"))
    db.add(models.ProductionPlan(id=2, plan_no="P2", status="Behind", planned_quantity=200,
                                 actual_quantity=None, plan_date=date(2026, 1, 1), shift_name="B"))
    db.add(models.ProductionPlan(id=3, plan_no="P3", status="Completed", planned_quantity=100,
                                 actual_quantity=100, plan_date=date(2026, 1, 1), shift_name="C"))
    db.commit()

    out = analytics_routes.get_production_plan_analytics(db=db, current_user={})
    assert out["total_plans"] == 3, out
    # planned: 100+200+100 = 400 (planned_quantity is NOT NULL)
    assert out["planned_quantity"] == 400, out
    # actual: 80 + 0(NULL->0) + 100 = 180
    assert out["actual_quantity"] == 180, out
    # achievement = round(180/400*100) = 45
    assert out["achievement"] == 45, out
    assert out["running"] == 1 and out["completed"] == 1 and out["behind"] == 1
    assert out["planned"] == 0
    print("PASS production-plan analytics: NULL actual -> 0, totals reconcile (400 planned / 180 actual / 45%)")


def test_production_plan_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_production_plan_analytics(db=db, current_user={})
    assert out["total_plans"] == 0
    assert out["planned_quantity"] == 0 and out["actual_quantity"] == 0
    assert out["achievement"] == 0
    print("PASS production-plan analytics: empty table -> zeros, no divide-by-zero")


def _null_utilization(db, *machine_ids):
    # Force a genuine SQL NULL. The ORM applies the column default=0 when you pass
    # utilization=None on insert, so a real NULL — the state that arises from raw
    # SQL / a migration / a cleared update in production — must be written directly.
    for mid in machine_ids:
        db.execute(text("UPDATE machines SET utilization = NULL WHERE id = :id"), {"id": mid})
    db.commit()
    db.expire_all()


def test_analytics_summary_null_utilization_averages_only_readings():
    # utilization is Column(Integer, default=0) without nullable=False -> a machine
    # can have a NULL reading. The old sum(m.utilization ...) did int + None and
    # 500'd the whole dashboard summary. Only machines WITH a reading are averaged,
    # so one unset row neither crashes nor drags the mean toward 0. No production
    # records -> the OEE fallback also runs (over machines with a reading). Numbers
    # are derived by hand.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Unset", status="Running", utilization=0))
    db.add(models.Machine(id=2, name="Good", status="Running", utilization=80))
    db.add(models.Machine(id=3, name="Low", status="Running", utilization=30))
    db.commit()
    _null_utilization(db, 1)

    out = analytics_routes.analytics_summary(db=db, current_user={})
    # avg over readings only: (80 + 30) / 2 = 55 — NOT (80+30+0)/3 = 37, NOT a crash.
    assert out["avg_utilization"] == 55, out["avg_utilization"]
    # no production -> fallback OEE = mean of calculate_fallback_oee over readings:
    # round(80*0.855)=68, round(30*0.855)=26 -> round((68+26)/2)=47.
    assert out["avg_oee"] == 47, out["avg_oee"]
    # alerts (generate_alerts, DB-backed) don't crash and don't invent a low-util
    # alert for the unset machine; the genuine 30% one still fires (<50 -> Medium).
    alert_pairs = {(a["machine"], a["type"]) for a in out["alerts"]}
    assert ("Unset", "Low Utilization") not in alert_pairs, alert_pairs
    assert ("Low", "Low Utilization") in alert_pairs, alert_pairs
    print("PASS analytics-summary averages only machines with a utilization reading (NULL-safe, 55% / OEE 47%)")


def test_analytics_summary_all_null_utilization_is_zero_not_a_crash():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="A", status="Running", utilization=0))
    db.add(models.Machine(id=2, name="B", status="Idle", utilization=0))
    db.commit()
    _null_utilization(db, 1, 2)
    out = analytics_routes.analytics_summary(db=db, current_user={})
    # no readings at all -> 0, not a divide-by-zero and not a None-sum crash.
    assert out["avg_utilization"] == 0, out["avg_utilization"]
    assert out["avg_oee"] == 0, out["avg_oee"]
    assert not any(a["type"] == "Low Utilization" for a in out["alerts"]), out["alerts"]
    print("PASS analytics-summary: all-NULL utilization -> 0, no crash, no fabricated alert")


def test_analytics_summary_shift_efficiency_is_pooled_not_mean_of_ratios():
    # Shift efficiency must POOL (total actual / total target), reconciling with
    # build_management_summary.target_achievement and ai/shift.py — not average
    # per-shift ratios. Two shifts of very different size make the two methods
    # disagree, and a third UNPLANNED shift (target 0) exposes the honesty bug:
    # the old code scored it a real 0% and divided by its slot, dragging the
    # headline down. Numbers derived by hand.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=70))
    # Big shift: 800/1000 = 80%. Small shift: 90/100 = 90%. Unplanned: 50/0.
    db.add(models.ShiftData(id=1, shift_name="A", target_output=1000, actual_output=800))
    db.add(models.ShiftData(id=2, shift_name="B", target_output=100, actual_output=90))
    db.add(models.ShiftData(id=3, shift_name="C", target_output=0, actual_output=50))
    db.commit()

    out = analytics_routes.analytics_summary(db=db, current_user={})
    # Pooled: (800 + 90 + 50) / (1000 + 100 + 0) = 940 / 1100 = 85.45% -> 85.
    # The old mean-of-ratios gave round((80 + 90 + 0) / 3) = round(56.67) = 57,
    # both over-weighting the tiny shift and counting the unplanned shift as 0%.
    assert out["avg_shift_efficiency"] == 85, out["avg_shift_efficiency"]
    print("PASS analytics-summary shift efficiency is pooled (85%), not mean-of-ratios (57%)")


def test_analytics_summary_shift_efficiency_all_zero_target_is_zero_not_a_crash():
    # Every shift unplanned (target 0): there is nothing to measure attainment
    # against, so the pooled rate is 0 (guarded divisor), never a ZeroDivisionError.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Idle", utilization=0))
    db.add(models.ShiftData(id=1, shift_name="A", target_output=0, actual_output=40))
    db.add(models.ShiftData(id=2, shift_name="B", target_output=0, actual_output=0))
    db.commit()
    out = analytics_routes.analytics_summary(db=db, current_user={})
    assert out["avg_shift_efficiency"] == 0, out["avg_shift_efficiency"]
    print("PASS analytics-summary shift efficiency: all-zero-target -> 0, no divide-by-zero")


def test_analytics_summary_shift_efficiency_is_pooled_in_sql_not_a_whole_table_scan():
    # Shift efficiency pools across the growing shift_data table (total actual /
    # total target). It must produce that number by aggregating in SQL, never by
    # hydrating the whole table into Python (rule-4) — the same fix already applied
    # to the sibling production_records scan in this very function. Shifts of very
    # different size make the pooled result differ from a per-shift mean, and the
    # numbers are derived BY HAND here so the test can't pass a broken pooling.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=70))
    # Big 700/1000=70%, small 95/100=95%, unplanned 30/0. Pooled = (700+95+30) /
    # (1000+100+0) = 825/1100 = 75.0% -> 75. A mean of ratios would be
    # round((70+95+0)/3) = round(55) = 55 — over-weighting the small shift and
    # scoring the unplanned one a false 0%.
    db.add(models.ShiftData(id=1, shift_name="A", target_output=1000, actual_output=700))
    db.add(models.ShiftData(id=2, shift_name="B", target_output=100, actual_output=95))
    db.add(models.ShiftData(id=3, shift_name="C", target_output=0, actual_output=30))
    db.commit()

    out = analytics_routes.analytics_summary(db=db, current_user={})
    assert out["avg_shift_efficiency"] == 75, out["avg_shift_efficiency"]
    assert out["avg_shift_efficiency"] != 55, \
        "shift efficiency must pool by target volume, not average per-shift ratios"

    # Regression guard: the compute must aggregate in SQL, not fall back to a whole-
    # table `.all()` scan of the growing shift_data table (rule-4).
    src = inspect.getsource(analytics_routes.analytics_summary)
    assert "db.query(models.ShiftData).all()" not in src, \
        "analytics-summary must pool shift_data in SQL, not hydrate the whole table"
    assert "func.sum(models.ShiftData" in src, \
        "analytics-summary should sum shift_data with func.sum"
    print("PASS analytics-summary shift efficiency is pooled in SQL (75%), no whole-table scan")


def test_analytics_summary_shift_efficiency_empty_table_is_zero_not_a_crash():
    # No shifts at all: COALESCE(SUM(..), 0) makes the empty-table aggregate read 0,
    # so the pooled rate is a guarded 0 rather than an int()-of-None / divide-by-zero.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Idle", utilization=0))
    db.commit()
    out = analytics_routes.analytics_summary(db=db, current_user={})
    assert out["avg_shift_efficiency"] == 0, out["avg_shift_efficiency"]
    print("PASS analytics-summary shift efficiency: empty shift_data -> 0, no empty-aggregate crash")


def test_analytics_summary_plant_oee_is_pooled_in_sql_not_a_whole_table_scan():
    # The plant OEE headline pools across production_records (ratio of sums). It must
    # produce that number by aggregating in SQL, never by hydrating the whole growing
    # table into Python (rule-4) — the same fix already applied to the sibling
    # executive rollups. Two machines of very different volume make the pooled result
    # differ from a naive per-record mean, and the numbers are derived BY HAND here
    # (independently of pooled_oee) so the test can't pass a broken pooling.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Running", utilization=80))
    # Big run: planned 1000, runtime 500, ideal 30s, total 500, good 400, rej 100.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    # Tiny run: planned 10, runtime 10, ideal 60s, total 10, good 10, rej 0.
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    db.commit()

    out = analytics_routes.analytics_summary(db=db, current_user={})
    # Pooled sums: planned 1010, runtime 510, total 510, good 410,
    #   ideal_seconds = 30*500 + 60*10 = 15000 + 600 = 15600.
    # availability = min(510/1010, 1)      = 0.50495... -> round(*100) = 50
    # performance  = min(15600/(510*60),1) = min(0.5098..,1) -> round(*100) = 51
    # quality      = min(410/510, 1)       = 0.80392... -> round(*100) = 80
    # oee          = round(0.50495*0.5098*0.80392*100) = round(20.69..) = 21
    assert out["avg_availability"] == 50, out["avg_availability"]
    assert out["avg_performance"] == 51, out["avg_performance"]
    assert out["avg_quality"] == 80, out["avg_quality"]
    assert out["avg_oee"] == 21, out["avg_oee"]
    # ...and it is emphatically NOT the mean of the two per-record OEEs. Big alone:
    #   a=min(500/1000,1)=.5, p=min(15000/30000,1)=.5, q=400/500=.8 -> round(20)=20.
    #   Tiny alone: a=1, p=min(600/600,1)=1, q=1 -> 100. mean(20,100)=60 != 21.
    assert out["avg_oee"] != 60, "plant OEE must pool by volume, not average per-record OEE"

    # Regression guard: the compute must aggregate in SQL, not fall back to a whole-
    # table `.all()` scan of the growing production_records table (rule-4).
    src = inspect.getsource(analytics_routes.analytics_summary)
    assert "db.query(models.ProductionRecord).all()" not in src, \
        "analytics-summary must pool production_records in SQL, not hydrate the whole table"
    assert "func.sum(models.ProductionRecord" in src, \
        "analytics-summary should sum production_records with func.sum"
    print("PASS analytics-summary plant OEE is pooled in SQL (a50/p51/q80/oee21), no whole-table scan")


def test_analytics_summary_plant_oee_empty_production_falls_back_not_a_crash():
    # No production records at all: pooled has_data is False (record COUNT is 0), so
    # OEE falls back to the per-machine utilization estimate rather than dividing by
    # an empty aggregate. Pins that the SQL count, not a hydrated list, drives the
    # `if record_count == 0` fallback branch.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M", status="Running", utilization=100))
    db.commit()
    out = analytics_routes.analytics_summary(db=db, current_user={})
    # fallback = calculate_fallback_oee(100) = round(100/100 * 0.9 * 0.95 * 100) = 86.
    assert out["avg_oee"] == 86, out["avg_oee"]
    print("PASS analytics-summary: empty production -> utilization fallback (86%), no empty-aggregate crash")


def test_executive_oee_null_utilization_fallback_is_zero_not_a_crash():
    # No production for the machine -> availability falls back to utilization. With
    # a NULL reading the old `max(machine.utilization, 0)` raised TypeError; it must
    # treat an unset reading as 0. Running with no runtime -> performance 90, no
    # quality rows -> quality 95, so OEE = round(0 * .9 * .95 * 100) = 0.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Unset", status="Running", utilization=0))
    db.commit()
    _null_utilization(db, 1)
    out = analytics_routes.get_executive_oee(db=db, current_user={})
    row = next(r for r in out["machine_ranking"] if r["machine_name"] == "Unset")
    assert row["availability"] == 0, row
    assert row["oee"] == 0, row
    assert row["utilization"] is None, row      # raw reading still surfaced honestly
    print("PASS executive-oee: NULL utilization fallback -> availability 0 (no max(None,0) crash)")


def test_final_executive_summary_null_columns_are_zero_not_a_crash():
    # /analytics/final-executive-summary sums / compares several columns that are
    # Column(Integer, default=0) WITHOUT nullable=False — passed_quantity,
    # dispatched_quantity, current_stock, reorder_level, amount — so a real SQL
    # NULL (raw SQL / migration / a cleared update) is legitimate. The old raw
    # sum(...) and `current_stock <= reorder_level` then did int + None / None <=
    # None and 500'd the whole summary on a single unset row. Each must coalesce
    # to the column's own default of 0. Every expected number is derived by hand.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="A", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="B", status="Running", utilization=60))
    db.add(models.Machine(id=3, name="C", status="Idle", utilization=0))

    db.add(_work_order(id=1, work_order_no="W1", status="Running", target_quantity=100, actual_quantity=10))
    db.add(_work_order(id=2, work_order_no="W2", status="Completed", target_quantity=50, actual_quantity=50))
    db.add(models.ProductionPlan(id=1, plan_no="P1", status="Running", planned_quantity=100,
                                 actual_quantity=10, plan_date=date(2026, 1, 1), shift_name="A"))

    db.add(models.QualityInspection(inspection_no="Q1", inspector="I", inspected_quantity=100, passed_quantity=80))
    db.add(models.QualityInspection(inspection_no="Q2", inspector="I", inspected_quantity=50, passed_quantity=0))

    db.add(models.InventoryItem(item_code="I1", item_name="One", category="raw", unit="pcs",
                                current_stock=5, reorder_level=10))
    db.add(models.InventoryItem(item_code="I2", item_name="Two", category="raw", unit="pcs",
                                current_stock=0, reorder_level=0))
    db.add(models.InventoryItem(item_code="I3", item_name="Three", category="raw", unit="pcs",
                                current_stock=100, reorder_level=10))

    db.add(models.CustomerOrder(order_no="O1", customer_name="Cust", product_name="P",
                                order_quantity=100, dispatched_quantity=60, due_date=date(2026, 1, 1)))
    db.add(models.CustomerOrder(order_no="O2", customer_name="Cust", product_name="P",
                                order_quantity=200, dispatched_quantity=0, due_date=date(2026, 1, 1)))

    db.add(models.PurchaseOrder(po_no="PO1", item_name="Part", order_quantity=10, unit="pcs",
                                expected_delivery_date=date(2026, 1, 1)))

    db.add(models.CostRecord(cost_no="C1", cost_type="Downtime", description="d", amount=1000))
    db.add(models.CostRecord(cost_no="C2", cost_type="Downtime", description="d", amount=0))
    db.commit()

    # Force genuine SQL NULLs (the ORM would apply default=0 to a None on insert).
    db.execute(text("UPDATE quality_inspections SET passed_quantity = NULL WHERE inspection_no = 'Q2'"))
    db.execute(text("UPDATE inventory_items SET current_stock = NULL, reorder_level = NULL WHERE item_code = 'I2'"))
    db.execute(text("UPDATE customer_orders SET dispatched_quantity = NULL WHERE order_no = 'O2'"))
    db.execute(text("UPDATE cost_records SET amount = NULL WHERE cost_no = 'C2'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_final_executive_summary(db=db, current_user={})

    assert out["machine_count"] == 3, out
    assert out["running_machines"] == 2, out
    assert out["work_orders"] == 2, out
    assert out["production_plans"] == 1, out
    # quality: inspected 100+50 = 150 (NOT NULL); passed 80 + 0(NULL->0) = 80
    #          -> round(80/150*100) = round(53.33) = 53
    assert out["quality_rate"] == 53, out
    # low stock: I1 (5<=10) + I2 (0<=0, both NULL->0) low; I3 (100<=10) not. = 2
    assert out["low_stock_items"] == 2, out
    assert out["customer_orders"] == 2, out
    # dispatch: order 100+200 = 300 (NOT NULL); dispatched 60 + 0(NULL->0) = 60
    #           -> round(60/300*100) = 20
    assert out["dispatch_rate"] == 20, out
    assert out["purchase_orders"] == 1, out
    # cost: 1000 + 0(NULL->0) = 1000
    assert out["total_cost"] == 1000, out
    print("PASS final-executive-summary: NULL count columns -> 0, no crash "
          "(quality 53% / dispatch 20% / 2 low-stock / £1000)")


def test_final_executive_summary_empty_tables_are_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_final_executive_summary(db=db, current_user={})
    assert out["machine_count"] == 0 and out["running_machines"] == 0
    assert out["work_orders"] == 0 and out["production_plans"] == 0
    # zero denominators -> 0, not ZeroDivisionError
    assert out["quality_rate"] == 0, out
    assert out["dispatch_rate"] == 0, out
    assert out["low_stock_items"] == 0, out
    assert out["customer_orders"] == 0 and out["purchase_orders"] == 0
    assert out["total_cost"] == 0, out
    print("PASS final-executive-summary: empty tables -> zeros, no divide-by-zero")


def test_final_executive_summary_does_not_hydrate_whole_tables():
    # Regression guard for the rule-4 fix: the endpoint must COUNT/SUM in SQL, never
    # fall back to `.all()` on these EIGHT growing tables (work_orders,
    # production_plans, quality_inspections, inventory_items, customer_orders,
    # purchase_orders, cost_records — plus machines) just to len()/sum() them in
    # Python. The hand-derived value tests above prove the numbers are unchanged;
    # this pins that they are produced without whole-table hydration.
    src = inspect.getsource(analytics_routes.get_final_executive_summary)
    assert ".all()" not in src, "final-executive-summary must aggregate in SQL, not hydrate whole tables"
    assert "func.count(" in src, "final-executive-summary should use func.count aggregates"
    assert "func.sum(" in src, "final-executive-summary should use func.sum aggregates"
    print("PASS final-executive-summary: counts/sums in SQL (no .all() table hydration)")


def test_final_executive_summary_counts_are_tenant_scoped():
    # Every model this endpoint aggregates is in SCOPED_MODELS, so each SQL COUNT/SUM
    # must stay per-tenant exactly as the old `.all()` scans did via the ORM hook
    # (ADR-0002): a tenant sees only its OWN rows in the executive summary. Seed two
    # tenants and assert GMATS's summary excludes DEFAULT's rows entirely.
    import tenancy as T
    db = _fresh_session()

    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(models.Machine(name="DEF-A", status="Running", utilization=50))
        db.add(_work_order(work_order_no="DW1", status="Running", target_quantity=100, actual_quantity=0))
        db.add(models.CustomerOrder(order_no="DO1", customer_name="C", product_name="P",
                                    order_quantity=100, dispatched_quantity=50, due_date=date(2026, 1, 1)))
        db.add(models.CostRecord(cost_no="DC1", cost_type="Downtime", description="d", amount=999))
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        db.add(models.Machine(name="GM-A", status="Running", utilization=50))
        db.add(models.Machine(name="GM-B", status="Idle", utilization=0))
        db.add(_work_order(work_order_no="GW1", status="Running", target_quantity=100, actual_quantity=0))
        db.add(models.QualityInspection(inspection_no="GQ1", inspector="I",
                                        inspected_quantity=100, passed_quantity=75))
        db.add(models.CustomerOrder(order_no="GO1", customer_name="C", product_name="P",
                                    order_quantity=200, dispatched_quantity=40, due_date=date(2026, 1, 1)))
        db.add(models.CostRecord(cost_no="GC1", cost_type="Downtime", description="d", amount=500))
        db.commit()
        out = analytics_routes.get_final_executive_summary(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)

    # GMATS sees only its own rows; DEFAULT's machine / work order / order / cost are invisible.
    assert out["machine_count"] == 2, out
    assert out["running_machines"] == 1, out
    assert out["work_orders"] == 1, out
    # quality_rate over GMATS's single inspection only: 75/100 -> 75
    assert out["quality_rate"] == 75, out
    # dispatch over GMATS's single order only: 40/200 -> 20 (NOT folding DEFAULT's 50/100)
    assert out["customer_orders"] == 1 and out["dispatch_rate"] == 20, out
    # total_cost is GMATS's £500 alone, never £999 + £500
    assert out["total_cost"] == 500, out
    print("PASS final-executive-summary: SQL counts/sums stay tenant-scoped (GMATS sees only its own)")


def _operator_job(**kw):
    kw.setdefault("operator_name", "Op")
    return models.OperatorJobExecution(**kw)


def test_operator_terminal_analytics_null_counts_and_reconciled_totals():
    # good_count / rejected_count are Column(Integer, default=0) WITHOUT
    # nullable=False, so a real SQL NULL (raw SQL / migration / a cleared update)
    # is legitimate. The old sum(row.good_count for ...) did int + None and 500'd;
    # COALESCE(SUM, 0) must treat the NULL as 0. Status buckets now come from a SQL
    # GROUP BY instead of a Python scan of the whole (growing) table. Every
    # expected number is derived by hand, not read back from the endpoint.
    db = _fresh_session()
    db.add(_operator_job(id=1, execution_no="OJ1", job_status="Started", good_count=40, rejected_count=10))
    db.add(_operator_job(id=2, execution_no="OJ2", job_status="Paused", good_count=20, rejected_count=0))
    db.add(_operator_job(id=3, execution_no="OJ3", job_status="Completed", good_count=100, rejected_count=None))
    db.add(_operator_job(id=4, execution_no="OJ4", job_status="Completed", good_count=None, rejected_count=5))
    db.commit()
    # Force genuine SQL NULLs (the ORM applies default=0 to a None passed on insert).
    db.execute(text("UPDATE operator_job_executions SET rejected_count = NULL WHERE execution_no = 'OJ3'"))
    db.execute(text("UPDATE operator_job_executions SET good_count = NULL WHERE execution_no = 'OJ4'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_operator_terminal_analytics(db=db, current_user={})
    assert out["total_jobs"] == 4, out
    assert out["started"] == 1 and out["paused"] == 1 and out["completed"] == 2, out
    # good: 40 + 20 + 100 + 0(NULL->0) = 160
    assert out["good_count"] == 160, out
    # rejected: 10 + 0 + 0(NULL->0) + 5 = 15
    assert out["rejected_count"] == 15, out
    # quality_rate = round(160 / (160+15) * 100) = round(91.43) = 91
    assert out["quality_rate"] == 91, out
    # the status buckets reconcile with the total job count (all four are named)
    assert out["started"] + out["paused"] + out["completed"] == 4, out
    print("PASS operator-terminal analytics: NULL counts -> 0, totals reconcile "
          "(160 good / 15 rejected / 91%)")


def test_operator_terminal_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_operator_terminal_analytics(db=db, current_user={})
    assert out["total_jobs"] == 0, out
    assert out["good_count"] == 0 and out["rejected_count"] == 0, out
    assert out["started"] == 0 and out["paused"] == 0 and out["completed"] == 0, out
    # zero denominator -> 0, not a ZeroDivisionError; SUM over no rows -> 0, not None
    assert out["quality_rate"] == 0, out
    print("PASS operator-terminal analytics: empty table -> zeros, no divide-by-zero, no None-sum crash")


def _inventory_item(**kw):
    kw.setdefault("item_name", kw.get("item_code", "ITEM"))
    kw.setdefault("unit", "pcs")
    return models.InventoryItem(**kw)


def test_inventory_analytics_null_stock_and_bounded_transaction_count():
    # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
    # nullable=False, so a genuine SQL NULL slips in via raw SQL / a migration / a
    # cleared update. The old `item.current_stock <= item.reorder_level` did
    # `None <= int` and the old `sum(item.current_stock ...)` did `int + None` —
    # both TypeError and 500 the endpoint. Coalescing to 0 must keep every number
    # honest. Separately, the transaction count now comes from a SQL COUNT instead
    # of loading the whole (growing) inventory_transactions table just for len().
    # Every expected number is derived by hand, not read back from the endpoint.
    db = _fresh_session()
    db.add(_inventory_item(id=1, item_code="ST1", category="Steel", supplier="ACME",
                           current_stock=100, reorder_level=20))
    db.add(_inventory_item(id=2, item_code="ST2", category="Steel", supplier=None,
                           current_stock=5, reorder_level=10))
    db.add(_inventory_item(id=3, item_code="BO1", category="Bolts", supplier="ACME",
                           current_stock=1, reorder_level=1))
    db.add(_inventory_item(id=4, item_code="BO2", category="Bolts", supplier="ACME",
                           current_stock=1, reorder_level=5))
    # three movements on the ledger — we only ever need the count
    db.add(models.InventoryTransaction(item_id=1, transaction_type="IN", quantity=50))
    db.add(models.InventoryTransaction(item_id=1, transaction_type="OUT", quantity=10))
    db.add(models.InventoryTransaction(item_id=2, transaction_type="IN", quantity=5))
    db.commit()
    # Force genuine SQL NULLs (the ORM applies default=0 to a None passed on insert):
    # BO1 has NULL stock AND NULL reorder; BO2 has NULL stock, reorder = 5.
    db.execute(text("UPDATE inventory_items SET current_stock = NULL, reorder_level = NULL WHERE item_code = 'BO1'"))
    db.execute(text("UPDATE inventory_items SET current_stock = NULL WHERE item_code = 'BO2'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_inventory_analytics(db=db, current_user={})
    assert out["total_items"] == 4, out
    # total_stock_units = 100 + 5 + 0(NULL) + 0(NULL) = 105
    assert out["total_stock_units"] == 105, out
    # low stock: ST2 (5<=10), BO1 (0<=0), BO2 (0<=5) -> 3.  ST1 (100<=20) is not.
    assert out["low_stock_items"] == 3, out
    # count came from SQL, and equals the true number of ledger rows
    assert out["transactions"] == 3, out
    # per-category stock reconciles with the total (Steel 105 + Bolts 0 = 105)
    assert out["category_counts"] == {"Steel": 105, "Bolts": 0}, out
    assert sum(out["category_counts"].values()) == out["total_stock_units"], out
    # per-supplier stock reconciles too; a NULL supplier folds into "Unknown"
    assert out["supplier_counts"] == {"ACME": 100, "Unknown": 5}, out
    assert sum(out["supplier_counts"].values()) == out["total_stock_units"], out
    print("PASS inventory analytics: NULL stock -> 0, totals reconcile (105 units), "
          "transaction count is a bounded SQL COUNT (3)")


def test_inventory_analytics_empty_tables_are_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_inventory_analytics(db=db, current_user={})
    assert out["total_items"] == 0 and out["low_stock_items"] == 0, out
    assert out["total_stock_units"] == 0, out
    # COUNT over no rows is 0, not None (the `or 0` guard also holds)
    assert out["transactions"] == 0, out
    assert out["category_counts"] == {} and out["supplier_counts"] == {}, out
    print("PASS inventory analytics: empty tables -> zeros, no None-count, no crash")


def test_inventory_analytics_transaction_count_is_tenant_scoped():
    # The SQL COUNT that replaced `.all()` must stay tenant-scoped, exactly like
    # the auto-scoped load it replaced (ADR-0002 do_orm_execute hook). A tenant
    # must never see another tenant's ledger size.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()
    tok = T.set_current_tenant("GMATS")
    try:
        db.add(models.InventoryTransaction(item_id=1, transaction_type="IN", quantity=5))
        db.add(models.InventoryTransaction(item_id=1, transaction_type="OUT", quantity=2))
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(models.InventoryTransaction(item_id=1, transaction_type="IN", quantity=9))
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        out = analytics_routes.get_inventory_analytics(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)
    # GMATS wrote 2 of the 3 rows; it must count only its own.
    assert out["transactions"] == 2, out
    print("PASS inventory analytics: transaction COUNT stays tenant-scoped (GMATS sees 2 of 3)")


def _escalation(**kw):
    kw.setdefault("title", "T")
    kw.setdefault("owner", "O")
    kw.setdefault("department", "D")
    return models.Escalation(**kw)


def test_escalation_analytics_buckets_and_reconciled_totals():
    # The endpoint used to hydrate the whole (growing) escalations table and
    # bucket it in Python; it now GROUP BYs in SQL. Semantics must be identical:
    # `total` counts every row, the status/severity buckets count only their
    # named values. Numbers are derived by hand, not read back from the endpoint.
    # One row carries a status ("Cancelled") outside the three named buckets and
    # another a NULL status — both must still be counted in `total` and must not
    # crash the GROUP BY.
    db = _fresh_session()
    db.add(_escalation(id=1, severity="Critical", status="Open"))
    db.add(_escalation(id=2, severity="High", status="In Progress"))
    db.add(_escalation(id=3, severity="Medium", status="Resolved"))
    db.add(_escalation(id=4, severity="Low", status="Open"))
    db.add(_escalation(id=5, severity="High", status="Open"))
    db.add(_escalation(id=6, severity="Critical", status="Cancelled"))
    db.commit()
    # A genuine NULL status (the column is nullable) — written directly because
    # the ORM would apply default="Open" if we passed status=None on insert.
    db.execute(text("UPDATE escalations SET status = NULL WHERE id = 3"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_escalation_analytics(db=db, current_user={})
    # total counts all 6 rows (including the Cancelled and the NULL-status one).
    assert out["total"] == 6, out
    # status buckets: Open = ids 1,4,5 = 3; In Progress = id 2 = 1;
    # Resolved = 0 (id 3's status was nulled out, so it no longer counts).
    assert out["open"] == 3, out
    assert out["in_progress"] == 1, out
    assert out["resolved"] == 0, out
    # severity buckets: Critical = ids 1,6 = 2; High = ids 2,5 = 2; Medium = 1; Low = 1.
    assert out["critical"] == 2, out
    assert out["high"] == 2, out
    assert out["medium"] == 1, out
    assert out["low"] == 1, out
    # severity is NOT NULL and every row here has a named severity, so the four
    # severity buckets reconcile with the headline total.
    assert out["critical"] + out["high"] + out["medium"] + out["low"] == out["total"], out
    # the named status buckets + the two unnamed-status rows (Cancelled, NULL)
    # also account for every row — nothing double-counted, nothing dropped.
    assert out["open"] + out["in_progress"] + out["resolved"] + 2 == out["total"], out
    print("PASS escalation analytics: SQL buckets match hand totals, severity reconciles (6 rows)")


def test_escalation_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_escalation_analytics(db=db, current_user={})
    assert out["total"] == 0
    assert out["open"] == 0 and out["in_progress"] == 0 and out["resolved"] == 0
    assert out["critical"] == 0 and out["high"] == 0
    assert out["medium"] == 0 and out["low"] == 0
    print("PASS escalation analytics: empty table -> all zeros, no crash")


def test_escalation_analytics_is_tenant_scoped():
    # The GROUP BY aggregates that replaced `.all()` must stay tenant-scoped,
    # exactly like the auto-scoped load they replaced (ADR-0002 do_orm_execute
    # hook). A tenant must never see another tenant's escalation counts.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()
    tok = T.set_current_tenant("GMATS")
    try:
        db.add(_escalation(id=1, severity="Critical", status="Open"))
        db.add(_escalation(id=2, severity="High", status="Resolved"))
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(_escalation(id=3, severity="Low", status="Open"))
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        out = analytics_routes.get_escalation_analytics(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)
    # GMATS wrote 2 of the 3 rows; it must count only its own.
    assert out["total"] == 2, out
    assert out["open"] == 1 and out["resolved"] == 1, out
    assert out["critical"] == 1 and out["high"] == 1, out
    assert out["low"] == 0, out  # the DEFAULT-tenant Low row is invisible to GMATS
    print("PASS escalation analytics: SQL GROUP BY stays tenant-scoped (GMATS sees 2 of 3)")


def test_quality_analytics_null_count_columns_are_zero_not_a_crash():
    # /analytics/quality sums passed / failed / rework / scrap_quantity, which are
    # Column(Integer, default=0) WITHOUT nullable=False — a raw-SQL / migration /
    # cleared-field write can store a real NULL. The old raw sum(...) then did
    # int + None -> TypeError -> 500 the whole quality rollup. Each must coalesce
    # to the column's own default of 0. inspected_quantity IS nullable=False, so
    # the denominator stays exact. Every expected number is derived by hand.
    db = _fresh_session()
    db.add(models.QualityInspection(inspection_no="Q1", inspector="I", machine_id=1,
                                    inspected_quantity=100, passed_quantity=80,
                                    failed_quantity=15, rework_quantity=3, scrap_quantity=2,
                                    defect_category="Scratch"))
    # Q2's non-inspected counts are all NULLed below; if they leaked (not coalesced)
    # these 99s would corrupt every total — so they double as a "not-coalesced" trap.
    db.add(models.QualityInspection(inspection_no="Q2", inspector="I", machine_id=2,
                                    inspected_quantity=60, passed_quantity=99,
                                    failed_quantity=99, rework_quantity=99, scrap_quantity=99,
                                    defect_category="Dent"))
    db.commit()
    # Force genuine SQL NULLs (the ORM would apply default=0 to a None on insert).
    db.execute(text(
        "UPDATE quality_inspections SET passed_quantity = NULL, failed_quantity = NULL, "
        "rework_quantity = NULL, scrap_quantity = NULL WHERE inspection_no = 'Q2'"
    ))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_quality_analytics(db=db, current_user={})

    assert out["total_inspections"] == 2, out
    assert out["inspected_quantity"] == 160, out            # 100 + 60 (both NOT NULL)
    assert out["passed_quantity"] == 80, out                # 80 + 0(NULL)
    assert out["failed_quantity"] == 15, out                # 15 + 0(NULL)
    assert out["rework_quantity"] == 3, out                 # 3 + 0(NULL)
    assert out["scrap_quantity"] == 2, out                  # 2 + 0(NULL)
    assert out["pass_rate"] == 50, out                      # round(80/160*100)
    assert out["fail_rate"] == 9, out                       # round(15/160*100)=round(9.375)
    # defect_counts / machine_failures reconcile to `failed` and prove the NULL
    # became 0 (not the trap 99): Scratch 15, Dent 0.
    assert out["defect_counts"] == {"Scratch": 15, "Dent": 0}, out
    assert out["machine_failures"] == {1: 15, 2: 0}, out
    assert sum(out["defect_counts"].values()) == out["failed_quantity"], out
    print("PASS quality analytics: NULL passed/failed/rework/scrap -> 0, no crash "
          "(pass 50% / fail 9%)")


def test_quality_analytics_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_quality_analytics(db=db, current_user={})
    assert out["total_inspections"] == 0, out
    assert out["inspected_quantity"] == 0 and out["failed_quantity"] == 0, out
    assert out["pass_rate"] == 0 and out["fail_rate"] == 0, out   # 0/0 guarded -> 0
    assert out["defect_counts"] == {} and out["machine_failures"] == {}, out
    print("PASS quality analytics: empty table -> zeros, no divide-by-zero")


def test_quality_analytics_is_tenant_scoped():
    # The SUM/GROUP BY aggregates that replaced the `.all()` scan must stay
    # tenant-scoped, exactly like the auto-scoped load they replaced (ADR-0002
    # do_orm_execute hook). A tenant must never see another tenant's inspections.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()
    tok = T.set_current_tenant("GMATS")
    try:
        db.add(models.QualityInspection(inspection_no="Q1", inspector="I", machine_id=1,
                                        inspected_quantity=100, passed_quantity=90,
                                        failed_quantity=10, defect_category="Scratch"))
        db.add(models.QualityInspection(inspection_no="Q2", inspector="I", machine_id=1,
                                        inspected_quantity=50, passed_quantity=45,
                                        failed_quantity=5, defect_category="Scratch"))
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(models.QualityInspection(inspection_no="Q3", inspector="I", machine_id=2,
                                        inspected_quantity=999, passed_quantity=0,
                                        failed_quantity=999, defect_category="Dent"))
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        out = analytics_routes.get_quality_analytics(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)
    # GMATS wrote 2 of the 3 rows; it must see only its own (150 inspected, 15
    # failed), never the DEFAULT tenant's 999s bleeding into any total or bucket.
    assert out["total_inspections"] == 2, out
    assert out["inspected_quantity"] == 150, out                 # 100 + 50
    assert out["failed_quantity"] == 15, out                     # 10 + 5
    assert out["pass_rate"] == 90, out                           # round(135/150*100)
    assert out["fail_rate"] == 10, out                           # round(15/150*100)
    assert out["defect_counts"] == {"Scratch": 15}, out          # no "Dent" from DEFAULT
    assert out["machine_failures"] == {1: 15}, out               # no machine 2 from DEFAULT
    print("PASS quality analytics: SQL SUM/GROUP BY stays tenant-scoped (GMATS sees 2 of 3)")


def test_executive_oee_null_quality_columns_in_per_machine_fallback():
    # When a machine has NO production records but DOES have inspections,
    # executive-oee falls back to its pooled inspection quality (passed/inspected).
    # passed_quantity is nullable, so `bucket["passed"] += row.passed_quantity`
    # did int + None -> TypeError -> 500. It must coalesce to 0.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="NoProd", status="Idle", utilization=0))
    db.add(models.QualityInspection(inspection_no="A", inspector="I", machine_id=1,
                                    inspected_quantity=100, passed_quantity=90))
    db.add(models.QualityInspection(inspection_no="B", inspector="I", machine_id=1,
                                    inspected_quantity=50, passed_quantity=40))
    db.commit()
    db.execute(text("UPDATE quality_inspections SET passed_quantity = NULL WHERE inspection_no = 'B'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_executive_oee(db=db, current_user={})
    row = next(r for r in out["machine_ranking"] if r["machine_id"] == 1)
    # bucket: inspected 100+50 = 150; passed 90 + 0(NULL) = 90 -> quality round(90/150*100)=60
    assert row["quality"] == 60, out
    # no production anywhere -> pooled plant OEE is 0 (not fabricated)
    assert out["plant_oee"] == 0, out
    print("PASS executive-oee: NULL passed_quantity in per-machine fallback -> quality 60, no crash")


def test_executive_oee_per_machine_components_capped_at_100():
    # The per-machine ranking must not print a component the data can't support.
    # The HTTP ingest rejects negatives and enforces good+rejected==total, but it
    # does NOT require runtime_minutes <= planned_minutes, so a machine that ran
    # PAST its planned window (runtime > planned) is a real, reachable input whose
    # raw availability exceeds 100%. Quality is likewise capped (a data-entry slip
    # can store good > total). Both are clamped to [0,100] like performance already
    # is and like pooled_oee / calculate_oee_from_record clamp every component, so a
    # single machine's OEE can't top 100% or disagree with the capped pooled plant.
    #
    # One overtime record (values chosen so every number is hand-derivable):
    #   availability = min(125/100, 1) * 100          = 100  (raw 125 -> capped)
    #   performance  = min((60*100)/(125*60), 1) * 100 = 80   (6000/7500 = 0.8)
    #   quality      = min(110/100, 1) * 100           = 100  (raw 110 -> capped)
    #   oee          = round(1.0 * 0.8 * 1.0 * 100)     = 80
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Overtime", status="Running", utilization=80))
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=100, runtime_minutes=125,
        ideal_cycle_time_seconds=60, total_count=100, good_count=110, rejected_count=0))
    db.commit()

    out = analytics_routes.get_executive_oee(db=db, current_user={})
    row = next(r for r in out["machine_ranking"] if r["machine_id"] == 1)

    # availability capped at 100 (was 125), quality capped at 100 (was 110)
    assert row["availability"] == 100, row
    assert row["quality"] == 100, row
    assert row["performance"] == 80, row
    # derived OEE stays within the physical bound
    assert row["oee"] == 80, row
    assert row["oee"] <= 100 and row["availability"] <= 100 and row["quality"] <= 100, row
    # and the pooled plant rollup is itself bounded (pooled_oee caps every component)
    assert out["plant_oee"] <= 100 and out["plant_availability"] <= 100, out
    print("PASS executive-oee: per-machine availability/quality capped at 100 (no >100% metric)")


def test_factory_command_center_null_stock_and_failed_are_zero_not_a_crash():
    # factory-command-center compares current_stock <= reorder_level (both nullable
    # Integers) and sums failed_quantity (nullable). A real SQL NULL made
    # `None <= None` and `sum(... None ...)` 500 the command centre. Coalesce to 0.
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="I1", item_name="One", category="raw", unit="pcs",
                                current_stock=5, reorder_level=10))      # low
    db.add(models.InventoryItem(item_code="I2", item_name="Two", category="raw", unit="pcs",
                                current_stock=0, reorder_level=0))       # NULLed -> 0<=0 low
    db.add(models.InventoryItem(item_code="I3", item_name="Three", category="raw", unit="pcs",
                                current_stock=100, reorder_level=10))    # not low
    db.add(models.QualityInspection(inspection_no="Q1", inspector="I", machine_id=1,
                                    inspected_quantity=100, failed_quantity=20))
    db.add(models.QualityInspection(inspection_no="Q2", inspector="I", machine_id=1,
                                    inspected_quantity=50, failed_quantity=7))  # NULLed below
    db.commit()
    db.execute(text("UPDATE inventory_items SET current_stock = NULL, reorder_level = NULL WHERE item_code = 'I2'"))
    db.execute(text("UPDATE quality_inspections SET failed_quantity = NULL WHERE inspection_no = 'Q2'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_factory_command_center(db=db, current_user={})
    # low stock: I1 (5<=10) + I2 (0<=0, both NULL->0); I3 (100<=10) not. = 2
    assert out["low_stock_items"] == 2, out
    # fail rate: inspected 100+50 = 150 (NOT NULL); failed 20 + 0(NULL) = 20
    #            -> round(20/150*100) = round(13.33) = 13
    assert out["quality_fail_rate"] == 13, out
    print("PASS factory-command-center: NULL stock/reorder + failed_quantity -> 0, no crash "
          "(2 low-stock / fail 13%)")


def test_factory_command_center_counts_reconcile_with_independent_numbers():
    # Pin every headline to a hand-derived number after moving the counts/sums into
    # SQL, including the NULL-status subtleties that must stay byte-identical to the
    # old Python comprehensions:
    #   active_work_orders = status IN (Running, Planned): a NULL status excluded.
    #   behind_plans       = status == "Behind": a NULL status excluded.
    #   open_escalations   = status != Resolved: a NULL status STILL counts as open
    #                        (old Python `None != "Resolved"` is True), so it's OR'd
    #                        back in — dropping it (bare SQL `!=`) would be a regression.
    #   low_stock          = COALESCE(current_stock,0) <= COALESCE(reorder_level,0).
    #   quality_fail_rate  = SUM(failed)/SUM(inspected), NULL failed -> 0.
    # Plus an hour-format downtime fixture ("2 hrs 15 min" -> 135, NOT 2) so the
    # free-text total (the one scan that stays in Python) is exercised too.
    db = _fresh_session()

    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="M2", status="Breakdown", utilization=0))
    db.add(models.Machine(id=3, name="M3", status="Idle", utilization=10))
    db.add(models.Machine(id=4, name="M4", status="Maintenance", utilization=0))

    db.add(models.DowntimeLog(machine_id=2, reason="Breakdown", duration="2 hrs 15 min"))
    db.add(models.DowntimeLog(machine_id=2, reason="Tool Change", duration="45 min"))

    # Work orders: Running + Planned are active (2); Completed, Delayed, NULL are not.
    for i, st in enumerate(["Running", "Planned", "Completed", "Delayed", "Running"]):
        db.add(models.WorkOrder(work_order_no=f"WO{i}", part_number="P", batch_number="B",
                                machine_id=1, target_quantity=100, status=st))
    # Production plans: 2 Behind, 1 Running, 1 NULL-status.
    for i, st in enumerate(["Behind", "Behind", "Running", "Planned"]):
        db.add(models.ProductionPlan(plan_no=f"PL{i}", planned_quantity=100,
                                     plan_date=date(2026, 1, 1), shift_name="A", status=st))
    # Escalations: Open + In Progress + (NULL) are open (3); Resolved is not.
    for i, st in enumerate(["Open", "In Progress", "Resolved", "Open"]):
        db.add(models.Escalation(title=f"E{i}", severity="High", owner="o",
                                 department="d", status=st))
    # Inventory: I1 low (5<=10), I2 low after NULLing (0<=0), I3 not (100<=10 false).
    db.add(models.InventoryItem(item_code="I1", item_name="One", category="raw", unit="pcs",
                                current_stock=5, reorder_level=10))
    db.add(models.InventoryItem(item_code="I2", item_name="Two", category="raw", unit="pcs",
                                current_stock=0, reorder_level=0))
    db.add(models.InventoryItem(item_code="I3", item_name="Three", category="raw", unit="pcs",
                                current_stock=100, reorder_level=10))
    # Quality: inspected 100+50=150, failed 20 + NULL(->0) = 20 -> 13%.
    db.add(models.QualityInspection(inspection_no="Q1", inspector="I", machine_id=1,
                                    inspected_quantity=100, failed_quantity=20))
    db.add(models.QualityInspection(inspection_no="Q2", inspector="I", machine_id=1,
                                    inspected_quantity=50, failed_quantity=7))
    db.add(models.FactoryLayoutNode(node_name="N1", machine_id=1, zone="Line A"))
    db.commit()
    # Turn one work-order, one plan, one escalation, one inspection to a genuine NULL.
    db.execute(text("UPDATE work_orders SET status = NULL WHERE work_order_no = 'WO4'"))
    db.execute(text("UPDATE production_plans SET status = NULL WHERE plan_no = 'PL3'"))
    db.execute(text("UPDATE escalations SET status = NULL WHERE title = 'E3'"))
    db.execute(text("UPDATE inventory_items SET current_stock = NULL, reorder_level = NULL WHERE item_code = 'I2'"))
    db.execute(text("UPDATE quality_inspections SET failed_quantity = NULL WHERE inspection_no = 'Q2'"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_factory_command_center(db=db, current_user={})

    assert out["machines"] == 4, out
    assert out["running"] == 1 and out["breakdown"] == 1, out
    assert out["idle"] == 1 and out["maintenance"] == 1, out
    assert out["total_downtime_minutes"] == 180, out          # 135 (2h15) + 45
    assert out["active_work_orders"] == 2, out                # Running + Planned; NULL/Completed/Delayed out
    assert out["behind_plans"] == 2, out                      # 2 Behind; NULL out
    # E0 Open, E1 In Progress, E2 Resolved, E3 Open->NULLed. Open ones:
    # E0, E1, and E3 (NULL, still counts as open) = 3; E2 Resolved excluded.
    assert out["open_escalations"] == 3, out
    assert out["low_stock_items"] == 2, out                   # I1 + I2(NULL->0<=0); I3 out
    assert out["quality_fail_rate"] == 13, out                # round(20/150*100)
    print("PASS factory-command-center: SQL counts reconcile with hand-derived numbers "
          "(WO 2 / behind 2 / open-esc 3 / low-stock 2 / fail 13% / downtime 180)")


def test_factory_command_center_does_not_hydrate_growing_tables():
    # Regression guard for the rule-4 fix: the endpoint must COUNT/SUM the growing
    # transactional tables in SQL, never `.all()`-hydrate them into Python. The
    # bounded reference sets it legitimately reads whole — the machine roster, the
    # layout nodes, and the free-text downtime log (no SQL SUM for "2 hrs 15 min") —
    # are exempt; the five growing tables below must not be scanned row-by-row.
    src = inspect.getsource(analytics_routes.get_factory_command_center)
    for hydration in (
        "db.query(models.WorkOrder).all()",
        "db.query(models.ProductionPlan).all()",
        "db.query(models.Escalation).all()",
        "db.query(models.InventoryItem).all()",
        "db.query(models.QualityInspection).all()",
    ):
        assert hydration not in src, f"factory-command-center must not hydrate: {hydration}"
    assert "func.count(" in src, "factory-command-center should COUNT in SQL"
    print("PASS factory-command-center: growing tables counted in SQL (no .all() hydration)")


def test_factory_command_center_empty_tables_are_zero_not_a_crash():
    # Empty plant: every count/sum is 0 and the 0/0 fail rate is guarded to 0 — the
    # SQL aggregates must return the column-default 0 (via COALESCE / `or 0`), never
    # a None -> TypeError 500.
    db = _fresh_session()
    out = analytics_routes.get_factory_command_center(db=db, current_user={})
    assert out["machines"] == 0 and out["total_downtime_minutes"] == 0, out
    assert out["active_work_orders"] == 0 and out["behind_plans"] == 0, out
    assert out["open_escalations"] == 0 and out["low_stock_items"] == 0, out
    assert out["quality_fail_rate"] == 0, out
    assert out["zone_summary"] == [], out
    print("PASS factory-command-center: empty tables -> all zeros, no crash")


def _count_selects(engine):
    """Attach a SELECT counter to an engine for the life of the returned handle.

    Lets a test assert the endpoint issues a BOUNDED number of queries — the
    proof that the per-signal N+1 lookups are gone, not merely that the numbers
    still add up."""
    from sqlalchemy import event

    state = {"selects": 0}

    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            state["selects"] += 1

    event.listen(engine, "before_cursor_execute", _before_cursor)
    return state


def _fresh_session_with_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)(), engine


def test_industrial_gateway_names_resolved_and_scan_is_bounded():
    # The gateway rollup used to run a fresh SELECT for the device AND the machine
    # of every distinct signal in the window (an N+1 on the growing
    # industrial_signals table). Resolving both from rosters read once must (a)
    # leave the resolved names byte-for-byte identical and (b) issue a bounded
    # number of queries regardless of how many signals are in the window.
    db, engine = _fresh_session_with_engine()
    db.add(models.Machine(id=1, name="Press-01", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Lathe-02", status="Idle", utilization=40))
    db.add(models.IndustrialDevice(id=10, device_code="D10", device_name="PLC-North", status="Online"))
    db.add(models.IndustrialDevice(id=11, device_code="D11", device_name="PLC-South", status="Offline"))
    db.add(models.PlcSignalMapping(mapping_code="M1", device_id=10, source_signal="s", mes_field="f", enabled="Yes"))
    db.add(models.PlcSignalMapping(mapping_code="M2", device_id=10, source_signal="s", mes_field="f", enabled="No"))
    # 8 signals across 2 devices/machines, each a distinct (device, signal) key so
    # none are deduped — the loop resolves a name for all 8.
    for i in range(4):
        db.add(models.IndustrialSignal(device_id=10, machine_id=1, signal_name=f"temp{i}",
                                       signal_value="hot", numeric_value=90, quality="Good"))
        db.add(models.IndustrialSignal(device_id=11, machine_id=2, signal_name=f"vibe{i}",
                                       signal_value="low", numeric_value=5, quality="Good"))
    db.commit()

    counter = _count_selects(engine)
    out = analytics_routes.get_industrial_gateway_analytics(db=db, current_user={})

    # Names are resolved correctly from the maps.
    by_name = {(r["signal_name"]): r for r in out["latest_signals"]}
    assert by_name["temp0"]["device_name"] == "PLC-North"
    assert by_name["temp0"]["machine_name"] == "Press-01"
    assert by_name["vibe0"]["device_name"] == "PLC-South"
    assert by_name["vibe0"]["machine_name"] == "Lathe-02"
    # Header counts are hand-derived from the fixture.
    assert out["devices"] == 2, out
    assert out["online_devices"] == 1 and out["offline_devices"] == 1, out
    assert out["signals"] == 8, out
    assert out["mappings"] == 2 and out["enabled_mappings"] == 1, out

    # The scan is bounded: devices + signals + mappings + machine-name roster = 4
    # SELECTs, and it does NOT grow with the 8 signals in the window. The old code
    # issued 4 + 2*(distinct signals) = 20 here.
    assert counter["selects"] <= 6, (
        f"expected a bounded query count, got {counter['selects']} — the per-signal N+1 is back")
    print(f"PASS industrial-gateway: names resolved from maps, bounded scan "
          f"({counter['selects']} SELECTs for 8 signals, not the old ~20)")


def test_industrial_gateway_missing_and_null_refs_fall_back_to_same_labels():
    # A signal with NO machine (machine_id NULL) must render "-"; a signal whose
    # device_id / machine_id point at a row that isn't there must fall back to the
    # SAME "Device {id}" / "-" labels the per-row-query version produced — the map
    # lookup must not crash or change the label on an absent id.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Press-01", status="Running", utilization=80))
    db.add(models.IndustrialDevice(id=10, device_code="D10", device_name="PLC-North", status="Online"))
    # (a) real device, no machine -> "-"
    db.add(models.IndustrialSignal(device_id=10, machine_id=None, signal_name="temp",
                                   signal_value="hot", numeric_value=90))
    # (b) device id with no matching device row -> "Device 99"; machine id 42 absent -> "-"
    db.add(models.IndustrialSignal(device_id=99, machine_id=42, signal_name="ghost",
                                   signal_value="x", numeric_value=1))
    db.commit()

    out = analytics_routes.get_industrial_gateway_analytics(db=db, current_user={})
    rows = {r["signal_name"]: r for r in out["latest_signals"]}
    assert rows["temp"]["device_name"] == "PLC-North" and rows["temp"]["machine_name"] == "-", rows
    assert rows["ghost"]["device_name"] == "Device 99" and rows["ghost"]["machine_name"] == "-", rows
    print("PASS industrial-gateway: absent device -> 'Device {id}', absent/NULL machine -> '-' (labels unchanged)")


def test_industrial_gateway_empty_tables_are_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_industrial_gateway_analytics(db=db, current_user={})
    assert out["devices"] == 0 and out["signals"] == 0 and out["mappings"] == 0
    assert out["online_devices"] == 0 and out["offline_devices"] == 0
    assert out["enabled_mappings"] == 0 and out["latest_signals"] == []
    print("PASS industrial-gateway: empty tables -> zeros / empty list, no crash")


def test_industrial_gateway_signals_count_is_true_total_past_the_500_window():
    # The regression the fix targets: industrial_signals is a growing table (the
    # adapter poll appends every tick and only trims back to ~1,000 rows), while
    # the endpoint hydrates only the last 500 for the latest-signal display. The
    # "signals" KPI must be the TRUE total, not the length of that 500-row window
    # (which froze at 500 forever) — the same true-total basis the sibling
    # /analytics/iot-command endpoint already reports for its telemetry count, and
    # the same basis as this endpoint's own device/mapping headcounts.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Press-01", status="Running", utilization=80))
    db.add(models.IndustrialDevice(id=10, device_code="D10", device_name="PLC-North", status="Online"))
    # 650 signals — well past the 500-row display window. Independently counted:
    # exactly 650 rows exist, all on device 10 / machine 1.
    db.add_all([
        models.IndustrialSignal(device_id=10, machine_id=1, signal_name=f"temp{i}",
                                signal_value=str(i), numeric_value=i, quality="Good")
        for i in range(650)
    ])
    db.commit()

    out = analytics_routes.get_industrial_gateway_analytics(db=db, current_user={})

    # true total ingested — NOT the 500-row display cap
    assert out["signals"] == 650, out
    # the display list is still capped: only the last 500 rows were scanned, so at
    # most 30 distinct (device, signal) pairs are surfaced.
    assert len(out["latest_signals"]) <= 30, out
    # sibling headcounts stay their own true totals, independent of the window
    assert out["devices"] == 1 and out["online_devices"] == 1, out
    print("PASS industrial-gateway: signals is a true total (650) past the 500-row window")


def _machine_event(machine_name, new_status):
    return models.MachineEvent(machine_name=machine_name, new_status=new_status)


def test_machine_state_summary_true_totals_and_reconciliation():
    # /analytics/machine-state-summary tallies state transitions per machine. Every
    # expected number is derived by hand from the rows below.
    db = _fresh_session()
    plan = {
        "CNC-1": ["Running"] * 5 + ["Idle"] * 3 + ["Breakdown"] * 2 + ["Maintenance"] * 1 + ["Offline"] * 1,
        "CNC-2": ["Running"] * 2 + ["Offline"] * 1,
    }
    for name, statuses in plan.items():
        db.add_all(_machine_event(name, s) for s in statuses)
    db.commit()

    rows = analytics_routes.get_machine_state_summary(db=db, current_user={})
    by_name = {r["machine_name"]: r for r in rows}

    cnc1 = by_name["CNC-1"]
    assert cnc1["Running"] == 5 and cnc1["Idle"] == 3, cnc1
    assert cnc1["Breakdown"] == 2 and cnc1["Maintenance"] == 1, cnc1
    # total_events counts EVERY event incl. the Offline one (not a charted bucket),
    # so it is the four buckets (11) plus that Offline (1) = 12 — the reconciliation
    # the old per-row loop kept (total_events >= sum of the four charted buckets).
    assert cnc1["total_events"] == 12, cnc1
    assert cnc1["total_events"] - (cnc1["Running"] + cnc1["Idle"] + cnc1["Breakdown"] + cnc1["Maintenance"]) == 1, cnc1

    cnc2 = by_name["CNC-2"]
    assert cnc2["Running"] == 2 and cnc2["Idle"] == 0, cnc2
    assert cnc2["Breakdown"] == 0 and cnc2["Maintenance"] == 0, cnc2
    assert cnc2["total_events"] == 3, cnc2

    # Deterministic order: busiest machine first (CNC-1 = 12 events, CNC-2 = 3).
    assert [r["machine_name"] for r in rows] == ["CNC-1", "CNC-2"], rows
    print("PASS machine-state-summary: SQL GROUP BY matches hand totals and reconciles (Offline in total only)")


def test_machine_state_summary_counts_all_events_not_a_recent_window():
    # Regression: the old code loaded only the most-recent 300 events GLOBALLY and
    # bucketed them in Python, so once machine_events passed 300 rows a quiet machine
    # dropped off the chart entirely and a busy one showed only its slice of the last
    # 300 — never its true transition frequency. The SQL GROUP BY reports the TRUE
    # totals over the whole (tenant-scoped) history.
    db = _fresh_session()
    # "Quiet" writes the OLDEST rows (4 events); "Busy" then writes 320 — so the
    # last-300-by-id window would have excluded Quiet outright and capped Busy at 300.
    db.add_all(_machine_event("Quiet", "Running") for _ in range(4))
    db.add_all(_machine_event("Busy", "Idle") for _ in range(320))
    db.commit()

    rows = analytics_routes.get_machine_state_summary(db=db, current_user={})
    by_name = {r["machine_name"]: r for r in rows}

    # Quiet survives with its true count (the old 300-cap would have dropped it).
    assert "Quiet" in by_name, by_name
    assert by_name["Quiet"]["Running"] == 4 and by_name["Quiet"]["total_events"] == 4, by_name["Quiet"]
    # Busy shows all 320, not the 300 a recent-window cap would have shown.
    assert by_name["Busy"]["Idle"] == 320 and by_name["Busy"]["total_events"] == 320, by_name["Busy"]
    # Busiest first.
    assert rows[0]["machine_name"] == "Busy", rows
    print("PASS machine-state-summary: true totals over all events, not a 300-row recent window")


def test_machine_state_summary_empty_table_is_zero_not_a_crash():
    db = _fresh_session()
    rows = analytics_routes.get_machine_state_summary(db=db, current_user={})
    assert rows == [], rows
    print("PASS machine-state-summary: empty table -> [], no crash")


def test_machine_state_summary_is_tenant_scoped():
    # The GROUP BY aggregate that replaced `.all()` must stay tenant-scoped, exactly
    # like the auto-scoped load it replaced (ADR-0002 do_orm_execute hook). A tenant
    # must never see another tenant's machine-state tallies.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()
    tok = T.set_current_tenant("GMATS")
    try:
        db.add_all(_machine_event("GMATS-CNC", s) for s in ["Running", "Running", "Breakdown"])
        db.commit()
    finally:
        T.reset_current_tenant(tok)
    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add_all(_machine_event("DEFAULT-CNC", s) for s in ["Idle", "Maintenance"])
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        rows = analytics_routes.get_machine_state_summary(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)

    by_name = {r["machine_name"]: r for r in rows}
    # GMATS sees only its own machine; the DEFAULT tenant's events are invisible.
    assert set(by_name) == {"GMATS-CNC"}, by_name
    assert by_name["GMATS-CNC"]["Running"] == 2 and by_name["GMATS-CNC"]["Breakdown"] == 1, by_name
    assert by_name["GMATS-CNC"]["total_events"] == 3, by_name
    print("PASS machine-state-summary: SQL GROUP BY stays tenant-scoped (GMATS sees only its own)")


def test_system_health_counts_are_sql_bounded_and_reconciled():
    # /analytics/system-health used to pull SIX whole tables into Python — machines,
    # users, alerts, escalations, notifications AND the unbounded audit_logs — just
    # to len()/filter them (rule-4 antipattern; audit_logs grows on every login /
    # mutation, so that scan is the heaviest and most dangerous of the six). The
    # counts now come from SQL COUNT/filtered-COUNT. Every expected number is derived
    # by hand, and the NULL-status escalation pins the open-count parity: SQL `status
    # != 'Resolved'` is NULL (not TRUE) for a NULL status, so without OR-ing the NULL
    # back in this would silently under-count opens vs the old Python `!=`.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="A", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="B", status="Idle", utilization=0))

    db.add(models.User(username="admin", password="x", role="Admin"))
    db.add(models.User(username="op", password="x", role="Operator"))
    db.add(models.User(username="sup", password="x", role="Supervisor"))

    db.add(models.Alert(alert_type="OEE", severity="High", message="m1"))
    db.add(models.Alert(alert_type="Downtime", severity="Critical", message="m2"))

    # Escalations: one Resolved (excluded), one Open, one In Progress, and one that
    # will be forced to a genuine NULL status (must count as open).
    db.add(models.Escalation(id=1, title="E1", severity="High", owner="o", department="d", status="Resolved"))
    db.add(models.Escalation(id=2, title="E2", severity="High", owner="o", department="d", status="Open"))
    db.add(models.Escalation(id=3, title="E3", severity="Medium", owner="o", department="d", status="In Progress"))
    db.add(models.Escalation(id=4, title="E4", severity="Low", owner="o", department="d", status="Open"))

    # Notifications: two Unread, one Read, one forced to NULL (NULL is NOT unread).
    db.add(models.Notification(id=1, notification_type="Machine", title="N1", message="m", status="Unread"))
    db.add(models.Notification(id=2, notification_type="Machine", title="N2", message="m", status="Unread"))
    db.add(models.Notification(id=3, notification_type="Machine", title="N3", message="m", status="Read"))
    db.add(models.Notification(id=4, notification_type="Machine", title="N4", message="m", status="Read"))

    db.add(models.AuditLog(actor="admin", action="login"))
    db.add(models.AuditLog(actor="admin", action="change_password"))
    db.commit()

    # Force genuine SQL NULLs (the ORM would apply the column default on insert).
    db.execute(text("UPDATE escalations SET status = NULL WHERE id = 4"))
    db.execute(text("UPDATE notifications SET status = NULL WHERE id = 4"))
    db.commit()
    db.expire_all()

    out = analytics_routes.get_system_health(db=db, current_user={})

    assert out["api_status"] == "Healthy" and out["database_status"] == "Connected", out
    assert out["machines"] == 2, out
    assert out["users"] == 3, out
    assert out["alerts"] == 2, out
    # open = every escalation NOT Resolved: E2 (Open) + E3 (In Progress) + E4
    # (NULL, counts as open) = 3; the one Resolved (E1) is excluded.
    assert out["open_escalations"] == 3, out
    # unread = only status == "Unread": N1 + N2 = 2; N3 (Read) and N4 (NULL) excluded.
    assert out["unread_notifications"] == 2, out
    assert out["audit_logs"] == 2, out
    print("PASS system-health: SQL counts reconcile (2 machines / 3 users / 2 alerts / "
          "3 open incl. NULL-status / 2 unread / 2 audit rows)")


def test_system_health_empty_tables_are_zero_not_a_crash():
    db = _fresh_session()
    out = analytics_routes.get_system_health(db=db, current_user={})
    assert out["machines"] == 0 and out["users"] == 0 and out["alerts"] == 0, out
    assert out["open_escalations"] == 0 and out["unread_notifications"] == 0, out
    assert out["audit_logs"] == 0, out
    # Static status strings still report healthy on an empty database.
    assert out["api_status"] == "Healthy" and out["database_status"] == "Connected", out
    assert "MES" in out["modules_enabled"], out
    print("PASS system-health: empty tables -> zero counts, no crash")


def test_system_health_does_not_hydrate_whole_tables():
    # Regression guard for the rule-4 fix: the endpoint must aggregate in SQL, never
    # fall back to `.all()` on these growing tables (audit_logs especially).
    src = inspect.getsource(analytics_routes.get_system_health)
    assert ".all()" not in src, "system-health must COUNT in SQL, not hydrate whole tables"
    assert "func.count(" in src, "system-health should use func.count aggregates"
    print("PASS system-health: counts in SQL (no .all() table hydration)")


def test_system_health_counts_are_tenant_scoped():
    # The scoped counts (machines, alerts, escalations, notifications) must stay
    # per-tenant exactly as the old `.all()` scans did via the ORM hook (ADR-0002):
    # a tenant sees only its own rows in the health panel.
    import tenancy as T
    db = _fresh_session()

    tok = T.set_current_tenant("DEFAULT")
    try:
        db.add(models.Machine(name="DEF-A", status="Running", utilization=50))
        db.add(models.Alert(alert_type="OEE", severity="High", message="d"))
        db.commit()
    finally:
        T.reset_current_tenant(tok)

    tok = T.set_current_tenant("GMATS")
    try:
        db.add(models.Machine(name="GM-A", status="Running", utilization=50))
        db.add(models.Machine(name="GM-B", status="Idle", utilization=0))
        db.add(models.Escalation(title="GE", severity="High", owner="o", department="d", status="Open"))
        db.commit()
        out = analytics_routes.get_system_health(db=db, current_user={"tenant": "GMATS"})
    finally:
        T.reset_current_tenant(tok)

    # GMATS sees only its own two machines and one open escalation; DEFAULT's are invisible.
    assert out["machines"] == 2, out
    assert out["alerts"] == 0, out
    assert out["open_escalations"] == 1, out
    print("PASS system-health: SQL counts stay tenant-scoped (GMATS sees only its own)")


def _oee_rec(machine_id):
    # A record whose OEE is derivable by hand: availability 80/100 = 0.8,
    # performance 60*100/(80*60) = 1.25 -> clamped to 1.0, quality 90/100 = 0.9,
    # so OEE = 0.8 * 1.0 * 0.9 = 0.72 -> 72%. machine_id tags insertion order.
    return models.ProductionRecord(
        machine_id=machine_id, planned_minutes=100, runtime_minutes=80,
        ideal_cycle_time_seconds=60, total_count=100, good_count=90, rejected_count=10)


def test_oee_trends_window_is_recent_200_not_frozen_on_oldest():
    # /analytics/oee-trends must track RECENT production. The old query ordered
    # id ASC then limited to 200, pinning the window to the first 200 rows ever
    # written — so past 200 rows the trend freezes on the oldest data forever.
    # Insert 205 records tagged 1..205 by insertion order (id 1..205 in sqlite).
    db = _fresh_session()
    for k in range(1, 206):
        db.add(_oee_rec(k))
    db.commit()

    rows = analytics_routes.get_oee_trends(db=db, current_user={})

    # bounded to the 200-row window
    assert len(rows) == 200, len(rows)
    ids = [r["machine_id"] for r in rows]
    # the window is the NEWEST 200 (ids 6..205); the oldest 5 (ids 1..5) are the
    # ones dropped — NOT the newest. This is the whole bug: an ASC limit dropped
    # the newest instead.
    assert ids == list(range(6, 206)), (ids[:3], ids[-3:])
    # chronological within the window (oldest-first) so the chart reads left->right
    assert ids == sorted(ids)
    assert rows[0]["machine_id"] == 6 and rows[-1]["machine_id"] == 205
    # x-axis index runs 1..200 over the window
    assert rows[0]["record"] == 1 and rows[-1]["record"] == 200
    # the very first records ever written are no longer masquerading as "the trend"
    assert 1 not in ids and 5 not in ids
    # OEE is the hand-derived 72% (and the >100% performance is clamped to 100)
    assert rows[-1]["oee"] == 72 and rows[-1]["performance"] == 100
    print("PASS oee-trends: recent 200-row window, chronological, not frozen on oldest")


def test_oee_trends_small_table_returns_all_oldest_first():
    # Below the window size, every record is returned oldest -> newest (the trend
    # direction), and the hand-derived per-record OEE is pinned.
    db = _fresh_session()
    for k in range(1, 4):
        db.add(_oee_rec(k))
    db.commit()
    rows = analytics_routes.get_oee_trends(db=db, current_user={})
    assert [r["machine_id"] for r in rows] == [1, 2, 3], rows
    assert [r["record"] for r in rows] == [1, 2, 3], rows
    assert rows[0]["availability"] == 80 and rows[0]["quality"] == 90
    assert rows[0]["performance"] == 100 and rows[0]["oee"] == 72
    print("PASS oee-trends: small table returns all records oldest->newest")


def test_oee_trends_empty_table_is_empty_not_a_crash():
    db = _fresh_session()
    rows = analytics_routes.get_oee_trends(db=db, current_user={})
    assert rows == [], rows
    print("PASS oee-trends: empty table -> [] (no crash)")


if __name__ == "__main__":
    test_analytics_paths_owned_by_module()
    test_analytics_summary_is_module_level_and_shared()
    test_module_has_no_relocated_helper_copies()
    test_executive_oee_plant_rollup_is_pooled_not_a_per_machine_mean()
    test_executive_oee_no_production_is_zero_not_fabricated()
    test_work_order_analytics_null_actual_and_reconciled_totals()
    test_work_order_analytics_empty_table_is_zero_not_a_crash()
    test_production_plan_analytics_null_actual_and_reconciled_totals()
    test_production_plan_analytics_empty_table_is_zero_not_a_crash()
    test_analytics_summary_null_utilization_averages_only_readings()
    test_analytics_summary_all_null_utilization_is_zero_not_a_crash()
    test_analytics_summary_shift_efficiency_is_pooled_not_mean_of_ratios()
    test_analytics_summary_shift_efficiency_all_zero_target_is_zero_not_a_crash()
    test_analytics_summary_shift_efficiency_is_pooled_in_sql_not_a_whole_table_scan()
    test_analytics_summary_shift_efficiency_empty_table_is_zero_not_a_crash()
    test_analytics_summary_plant_oee_is_pooled_in_sql_not_a_whole_table_scan()
    test_analytics_summary_plant_oee_empty_production_falls_back_not_a_crash()
    test_executive_oee_null_utilization_fallback_is_zero_not_a_crash()
    test_final_executive_summary_null_columns_are_zero_not_a_crash()
    test_final_executive_summary_empty_tables_are_zero_not_a_crash()
    test_final_executive_summary_does_not_hydrate_whole_tables()
    test_final_executive_summary_counts_are_tenant_scoped()
    test_operator_terminal_analytics_null_counts_and_reconciled_totals()
    test_operator_terminal_analytics_empty_table_is_zero_not_a_crash()
    test_inventory_analytics_null_stock_and_bounded_transaction_count()
    test_inventory_analytics_empty_tables_are_zero_not_a_crash()
    test_inventory_analytics_transaction_count_is_tenant_scoped()
    test_escalation_analytics_buckets_and_reconciled_totals()
    test_escalation_analytics_empty_table_is_zero_not_a_crash()
    test_escalation_analytics_is_tenant_scoped()
    test_quality_analytics_null_count_columns_are_zero_not_a_crash()
    test_quality_analytics_empty_table_is_zero_not_a_crash()
    test_quality_analytics_is_tenant_scoped()
    test_executive_oee_null_quality_columns_in_per_machine_fallback()
    test_executive_oee_per_machine_components_capped_at_100()
    test_factory_command_center_null_stock_and_failed_are_zero_not_a_crash()
    test_factory_command_center_counts_reconcile_with_independent_numbers()
    test_factory_command_center_does_not_hydrate_growing_tables()
    test_factory_command_center_empty_tables_are_zero_not_a_crash()
    test_industrial_gateway_names_resolved_and_scan_is_bounded()
    test_industrial_gateway_missing_and_null_refs_fall_back_to_same_labels()
    test_industrial_gateway_empty_tables_are_zero_not_a_crash()
    test_industrial_gateway_signals_count_is_true_total_past_the_500_window()
    test_machine_state_summary_true_totals_and_reconciliation()
    test_machine_state_summary_counts_all_events_not_a_recent_window()
    test_machine_state_summary_empty_table_is_zero_not_a_crash()
    test_machine_state_summary_is_tenant_scoped()
    test_system_health_counts_are_sql_bounded_and_reconciled()
    test_system_health_empty_tables_are_zero_not_a_crash()
    test_system_health_does_not_hydrate_whole_tables()
    test_system_health_counts_are_tenant_scoped()
    test_oee_trends_window_is_recent_200_not_frozen_on_oldest()
    test_oee_trends_small_table_returns_all_oldest_first()
    test_oee_trends_empty_table_is_empty_not_a_crash()
    print("ALL ANALYTICS ROUTE TESTS PASSED")
