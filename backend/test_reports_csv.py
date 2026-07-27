"""Report CSV export tests — content correctness + no N+1 machine lookup.

The /reports/downtime.csv and /reports/oee.csv exports resolve a machine name
per row. Those names used to be looked up with a fresh per-row query (an N+1
over the unbounded downtime / production tables); they now come from a single
bounded machine-name map. These tests pin the rendered CSV to independently
hand-derived values, cover the edge cases (empty table, a row whose machine was
deleted), and assert the query count does NOT grow with the number of rows.

Run:  python backend/test_reports_csv.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models
import reports_routes
from database import Base


def _engine_and_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


class _SelectCounter:
    """Counts SELECT statements executed on an engine, so a test can assert an
    export issues a bounded number of queries rather than one-per-row."""

    def __init__(self, engine):
        self.count = 0
        event.listen(engine, "before_cursor_execute", self._on_exec)

    def _on_exec(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1

    def reset(self):
        self.count = 0


def _seed_machines(db, n):
    for i in range(1, n + 1):
        db.add(models.Machine(id=i, name=f"CNC-{i}", status="Running", utilization=80))
    db.commit()


def test_downtime_csv_content_and_deleted_machine():
    engine, db = _engine_and_session()
    _seed_machines(db, 2)
    # Two logs on real machines + one pointing at a machine id that no longer
    # exists (deleted) -> its machine_name column must render empty, not crash.
    db.add(models.DowntimeLog(id=1, machine_id=1, reason="Breakdown", duration="2 hrs", notes="belt"))
    db.add(models.DowntimeLog(id=2, machine_id=2, reason="Changeover", duration="30 min", notes=None))
    db.add(models.DowntimeLog(id=3, machine_id=999, reason="Unknown", duration="5 min", notes=None))
    db.commit()

    body = reports_routes.export_downtime_csv(db=db, current_user={}).body.decode()
    lines = body.strip().splitlines()

    assert lines[0] == "id,machine_id,machine_name,reason,duration,notes,created_at"
    assert len(lines) == 4  # header + 3 logs, id-ascending
    # Machine names resolved from the map; missing machine -> blank name; notes None -> blank
    assert lines[1].startswith("1,1,CNC-1,Breakdown,2 hrs,belt,")
    assert lines[2].startswith("2,2,CNC-2,Changeover,30 min,,")
    assert lines[3].startswith("3,999,,Unknown,5 min,,")
    print("PASS downtime.csv renders names, blanks a deleted machine, keeps id order")


def test_oee_csv_values_independently_derived():
    engine, db = _engine_and_session()
    _seed_machines(db, 1)
    # planned=100 runtime=90 ideal=30s total=150 good=140 rejected=10
    #   availability = 90/100          = 0.90 -> 90
    #   performance  = (30*150)/(90*60) = 4500/5400 = 0.8333 -> 83
    #   quality      = 140/150          = 0.9333 -> 93
    #   oee          = 0.90*0.8333*0.9333 = 0.70 -> 70
    db.add(models.ProductionRecord(id=1, machine_id=1, planned_minutes=100, runtime_minutes=90,
                                   ideal_cycle_time_seconds=30, total_count=150, good_count=140,
                                   rejected_count=10))
    db.commit()

    body = reports_routes.export_oee_csv(db=db, current_user={}).body.decode()
    lines = body.strip().splitlines()
    assert lines[0].startswith("id,machine_id,machine_name,availability,performance,quality,oee")
    assert len(lines) == 2
    # id,machine_id,name,availability,performance,quality,oee,planned,runtime,total,good,rejected,created
    assert lines[1].startswith("1,1,CNC-1,90,83,93,70,100,90,150,140,10,")
    print("PASS oee.csv rows carry the hand-derived OEE components")


def test_exports_are_empty_safe():
    engine, db = _engine_and_session()
    down = reports_routes.export_downtime_csv(db=db, current_user={}).body.decode().strip().splitlines()
    oee = reports_routes.export_oee_csv(db=db, current_user={}).body.decode().strip().splitlines()
    assert down == ["id,machine_id,machine_name,reason,duration,notes,created_at"]
    assert oee == [
        "id,machine_id,machine_name,availability,performance,quality,oee,"
        "planned_minutes,runtime_minutes,total_count,good_count,rejected_count,created_at"
    ]
    print("PASS empty downtime/oee exports return the header only, no crash")


def test_downtime_export_query_count_is_constant():
    """The whole point of the fix: query count must not grow with row count."""
    def queries_for(num_logs):
        engine, db = _engine_and_session()
        _seed_machines(db, 3)
        for i in range(1, num_logs + 1):
            db.add(models.DowntimeLog(id=i, machine_id=(i % 3) + 1, reason="R", duration="1 hr"))
        db.commit()
        counter = _SelectCounter(engine)
        counter.reset()
        reports_routes.export_downtime_csv(db=db, current_user={})
        return counter.count

    few = queries_for(2)
    many = queries_for(40)
    # Was O(rows): one machine lookup per log. Now O(1): logs + machine map.
    assert few == many, f"query count grew with rows ({few} -> {many}); N+1 not eliminated"
    assert many <= 3, f"expected a bounded handful of queries, got {many}"
    print(f"PASS downtime.csv issues a constant {many} queries for 2 and for 40 logs (no N+1)")


def test_oee_export_query_count_is_constant():
    def queries_for(num_records):
        engine, db = _engine_and_session()
        _seed_machines(db, 3)
        for i in range(1, num_records + 1):
            db.add(models.ProductionRecord(id=i, machine_id=(i % 3) + 1, planned_minutes=100,
                                           runtime_minutes=80, ideal_cycle_time_seconds=30,
                                           total_count=100, good_count=95, rejected_count=5))
        db.commit()
        counter = _SelectCounter(engine)
        counter.reset()
        reports_routes.export_oee_csv(db=db, current_user={})
        return counter.count

    few = queries_for(2)
    many = queries_for(40)
    assert few == many, f"query count grew with rows ({few} -> {many}); N+1 not eliminated"
    assert many <= 3, f"expected a bounded handful of queries, got {many}"
    print(f"PASS oee.csv issues a constant {many} queries for 2 and for 40 records (no N+1)")


def test_operational_exports_content_and_name_maps():
    # One row per operational table, with the machine/supplier name resolved from
    # the bounded map — pinned lines, incl. a PO whose supplier row is gone.
    from datetime import date
    engine, db = _engine_and_session()
    _seed_machines(db, 1)
    db.add(models.WorkOrder(id=1, work_order_no="WO-1", part_number="P1", batch_number="B1",
                            machine_id=1, target_quantity=100, actual_quantity=40,
                            status="Planned", material_state="RAW"))
    db.add(models.MaintenanceTask(id=1, task_no="PM-1", machine_id=1, task_type="Preventive",
                                  priority="High", assigned_to="tech",
                                  planned_date=date(2026, 7, 1), status="Open"))
    db.add(models.Supplier(id=1, supplier_code="S1", supplier_name="Acme"))
    db.add(models.PurchaseOrder(id=1, po_no="PO-1", supplier_id=1, item_name="Wire",
                                order_quantity=10, received_quantity=4, unit="kg",
                                expected_delivery_date=date(2026, 7, 2), status="Open"))
    db.add(models.PurchaseOrder(id=2, po_no="PO-2", supplier_id=999, item_name="Paste",
                                order_quantity=5, received_quantity=0, unit="kg",
                                expected_delivery_date=date(2026, 7, 3), status="Open"))
    db.add(models.InventoryItem(id=1, item_code="IT-1", item_name="Bracket", category="Parts",
                                unit="pcs", current_stock=50, reorder_level=10))
    db.add(models.QualityInspection(id=1, inspection_no="QI-1", machine_id=1, inspector="QA",
                                    inspected_quantity=100, passed_quantity=95,
                                    failed_quantity=5, status="Closed"))
    db.add(models.Escalation(id=1, title="Oven trip", severity="High", owner="Ops",
                             department="Maintenance", status="Open", source="Manual",
                             machine_id=1))
    db.commit()

    u = {}
    wo = reports_routes.export_work_orders_csv(db=db, current_user=u).body.decode().splitlines()
    assert wo[1].startswith("1,WO-1,P1,B1,1,CNC-1,100,40,Planned,RAW")
    mt = reports_routes.export_maintenance_csv(db=db, current_user=u).body.decode().splitlines()
    assert mt[1] == "1,PM-1,1,CNC-1,Preventive,High,tech,2026-07-01,,0,,Open"
    po = reports_routes.export_purchase_orders_csv(db=db, current_user=u).body.decode().splitlines()
    assert po[1].startswith("1,PO-1,1,Acme,Wire,10,4,kg,2026-07-02,Open")
    assert po[2].startswith("2,PO-2,999,,Paste,5,0,kg,2026-07-03,Open")   # deleted supplier -> blank
    inv = reports_routes.export_inventory_csv(db=db, current_user=u).body.decode().splitlines()
    assert inv[1] == "1,IT-1,Bracket,Parts,,pcs,50,10,"
    qc = reports_routes.export_quality_csv(db=db, current_user=u).body.decode().splitlines()
    assert qc[1] == "1,QI-1,1,CNC-1,QA,100,95,5,,0,0,Closed"
    esc = reports_routes.export_escalations_csv(db=db, current_user=u).body.decode().splitlines()
    assert esc[1].startswith("1,Oven trip,High,Maintenance,Ops,Open,Manual,1,CNC-1,")
    print("PASS operational exports render pinned rows with bounded name maps")


def test_operational_exports_are_empty_safe_and_bounded():
    engine, db = _engine_and_session()
    _seed_machines(db, 1)
    db.commit()
    u = {}
    exports = [
        reports_routes.export_work_orders_csv, reports_routes.export_maintenance_csv,
        reports_routes.export_purchase_orders_csv, reports_routes.export_inventory_csv,
        reports_routes.export_quality_csv, reports_routes.export_escalations_csv,
    ]
    for fn in exports:
        body = fn(db=db, current_user=u).body.decode()
        assert len(body.splitlines()) == 1, f"{fn.__name__} not header-only when empty"

    # Query count must not grow with rows (no per-row name lookups).
    for i in range(1, 31):
        db.add(models.WorkOrder(work_order_no=f"W{i}", part_number="P", batch_number="B",
                                machine_id=1, target_quantity=1, status="Planned"))
    db.commit()
    counter = _SelectCounter(engine)
    counter.reset()
    reports_routes.export_work_orders_csv(db=db, current_user=u)
    assert counter.count <= 3, f"work-orders export ran {counter.count} SELECTs for 30 rows"
    print("PASS operational exports are empty-safe and issue a bounded query count")


if __name__ == "__main__":
    test_downtime_csv_content_and_deleted_machine()
    test_oee_csv_values_independently_derived()
    test_exports_are_empty_safe()
    test_downtime_export_query_count_is_constant()
    test_oee_export_query_count_is_constant()
    test_operational_exports_content_and_name_maps()
    test_operational_exports_are_empty_safe_and_bounded()
    print("ALL REPORT CSV TESTS PASSED")
