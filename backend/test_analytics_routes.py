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
    test_executive_oee_null_utilization_fallback_is_zero_not_a_crash()
    test_final_executive_summary_null_columns_are_zero_not_a_crash()
    test_final_executive_summary_empty_tables_are_zero_not_a_crash()
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
    test_executive_oee_null_quality_columns_in_per_machine_fallback()
    test_factory_command_center_null_stock_and_failed_are_zero_not_a_crash()
    print("ALL ANALYTICS ROUTE TESTS PASSED")
