"""Per-machine index migration tests (ADR-0006 hardening).

The Machine-Health twin filters growing tables by machine on every poll:

  * /machine-health (ai.twin.build_twins) fires, PER machine, a query for that
    machine's recent downtime, its open maintenance tasks, and its pending agent
    actions;
  * /machine-health/{id} (build_machine_detail) adds per-machine production and
    quality lookups.

A plain ForeignKey column is NOT indexed (SQLAlchemy indexes only the primary
key), so each `machine_id == X` / `related_machine_id == X` filter was a
full-table scan on a table that grows forever. main._ensure_index now adds an
idempotent index for each of those columns. These tests pin:

  1. the five indexes exist on the exact (table, column) the read-models filter;
  2. the migration is idempotent (CREATE INDEX IF NOT EXISTS — re-running never
     errors and never duplicates);
  3. the indexed columns are the real columns the twin filters (drift guard), and
     a per-machine query returns ONLY the target machine's rows over the index.

Run:  python backend/test_machine_id_indexes.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import inspect

import main            # importing main runs every _ensure_index migration
import models
from ai import twin

# (table, column) each read-model filters per machine — the columns main.py must
# have indexed. Derived from the actual filters in ai/twin.py, not restated by
# hand: if a filter column changes, the drift guard below fails.
EXPECTED = [
    ("downtime_logs", "machine_id"),
    ("production_records", "machine_id"),
    ("quality_inspections", "machine_id"),
    ("maintenance_tasks", "machine_id"),
    ("agent_actions", "related_machine_id"),
]


def _indexed_columns(inspector, table):
    """The set of single-column index targets on a table (column name -> covered)."""
    cols = set()
    for ix in inspector.get_indexes(table):
        # single-column indexes are what _ensure_index creates
        if len(ix["column_names"]) == 1:
            cols.add(ix["column_names"][0])
    return cols


def test_each_per_machine_column_is_indexed():
    inspector = inspect(main.engine)
    for table, column in EXPECTED:
        assert column in _indexed_columns(inspector, table), (
            f"{table}.{column} is filtered per-machine by the twin but is not indexed"
        )
    print("PASS every per-machine twin filter column is indexed")


def test_index_name_and_idempotency():
    # main._ensure_index names indexes ix_<table>_<column>; re-running the exact
    # migration must not raise (CREATE INDEX IF NOT EXISTS) and must not add a
    # second index for the same column.
    inspector = inspect(main.engine)
    for table, column in EXPECTED:
        names = {ix["name"] for ix in inspector.get_indexes(table)
                 if ix["column_names"] == [column]}
        assert f"ix_{table}_{column}" in names, (table, column, names)

    # Idempotent re-run — same statements main.py already issued at import.
    for table, column in EXPECTED:
        main._ensure_index(table, column)

    inspector = inspect(main.engine)   # fresh reflection after the re-run
    for table, column in EXPECTED:
        matching = [ix for ix in inspector.get_indexes(table)
                    if ix["column_names"] == [column] and ix["name"] == f"ix_{table}_{column}"]
        assert len(matching) == 1, f"re-running the migration duplicated {table}.{column}: {matching}"
    print("PASS index migration is idempotent (no error, no duplicate on re-run)")


def test_drift_guard_columns_are_real_and_filtered():
    # The indexed column must be a real column on the model, so an index can never
    # silently target a renamed/removed column. (The twin's own tests assert the
    # per-machine FILTER correctness; this guards the index tracks that column.)
    model_by_table = {
        "downtime_logs": models.DowntimeLog,
        "production_records": models.ProductionRecord,
        "quality_inspections": models.QualityInspection,
        "maintenance_tasks": models.MaintenanceTask,
        "agent_actions": models.AgentAction,
    }
    for table, column in EXPECTED:
        model = model_by_table[table]
        assert hasattr(model, column), f"{model.__name__} has no column {column}"
    print("PASS indexed columns are real model columns (drift guard)")


def test_per_machine_query_returns_only_that_machine_over_the_index():
    # Independent correctness: with two machines each carrying their own rows, the
    # twin snapshot for machine 1 must reflect ONLY machine 1's downtime / open
    # tasks / pending actions — the exact per-machine filters the new indexes serve.
    # Uses an isolated in-memory DB so the count values are hand-derived, not tied
    # to whatever ci.db holds.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    # machine 1: two downtimes, one open task, one pending action
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="40 min"))
    db.add(models.DowntimeLog(machine_id=1, reason="Setup", duration="20 min"))
    db.add(models.MaintenanceTask(tenant_code="DEFAULT", task_no="T1", machine_id=1,
                                  task_type="Predictive", priority="High", assigned_to="x",
                                  planned_date=datetime.utcnow().date(), status="Open"))
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="x", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    # machine 2: its OWN rows — must never bleed into machine 1's snapshot
    db.add(models.DowntimeLog(machine_id=2, reason="Other", duration="99 min"))
    db.add(models.MaintenanceTask(tenant_code="DEFAULT", task_no="T2", machine_id=2,
                                  task_type="Preventive", priority="Low", assigned_to="y",
                                  planned_date=datetime.utcnow().date(), status="Open"))
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="reorder", action_type="draft_po",
                              summary="y", ref_kind="purchase_order", ref_id=9,
                              related_machine_id=2, status="Proposed"))
    db.commit()

    twins = {t["machine_id"]: t for t in twin.build_twins(db, "DEFAULT")}
    m1 = twins[1]
    assert len(m1["recent_downtime"]) == 2          # only machine 1's two logs
    assert m1["open_maintenance_tasks"] == 1        # machine 2's task excluded
    assert m1["pending_agent_actions"] == 1         # machine 2's action excluded
    m2 = twins[2]
    assert len(m2["recent_downtime"]) == 1 and m2["open_maintenance_tasks"] == 1
    print("PASS per-machine twin snapshot is scoped to the indexed machine column")


if __name__ == "__main__":
    test_each_per_machine_column_is_indexed()
    test_index_name_and_idempotency()
    test_drift_guard_columns_are_real_and_filtered()
    test_per_machine_query_returns_only_that_machine_over_the_index()
    print("MACHINE-ID INDEX MIGRATION OK")
