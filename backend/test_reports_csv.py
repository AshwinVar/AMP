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


if __name__ == "__main__":
    test_downtime_csv_content_and_deleted_machine()
    test_oee_csv_values_independently_derived()
    test_exports_are_empty_safe()
    test_downtime_export_query_count_is_constant()
    test_oee_export_query_count_is_constant()
    print("ALL REPORT CSV TESTS PASSED")
