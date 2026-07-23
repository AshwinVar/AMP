"""Machine Health twin tests (ADR-0006 / PR #21).

The twin composes one snapshot per machine (state + health score + downtime +
open tasks + pending agent actions), worst health first, tenant-scoped.

Run:  python backend/test_twin.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import twin


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _session_capturing_sql():
    """A session that records every SQL statement it emits, so a test can prove a
    query was bounded in SQL rather than loaded whole and filtered in Python."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    stmts: list = []

    @event.listens_for(engine, "before_cursor_execute")
    def _cap(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    return sessionmaker(bind=engine)(), stmts


def _norm(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_cockpit_drilldowns_are_windowed_in_sql_not_loaded_whole():
    # The cockpit's 7-day downtime and production trends must bound the window in
    # SQL — loading a machine's entire (growing) history to draw a 7-row sparkline
    # is the anti-pattern. A 400-day-old row must never be loaded, not merely
    # dropped in Python afterward.
    db, stmts = _session_capturing_sql()
    now = datetime.utcnow()
    old = now - timedelta(days=400)                       # far outside the 7-day window
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="30 min", created_at=now))
    # ancient rows that must NOT be scanned to build a 7-day view
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=100,
                                   ideal_cycle_time_seconds=30, total_count=999, good_count=999,
                                   rejected_count=0, created_at=old))
    db.add(models.DowntimeLog(machine_id=1, reason="Ancient", duration="999 min", created_at=old))
    db.commit()

    stmts.clear()
    prod = twin._machine_production(db, 1)
    dt = twin._downtime_trend(db, 1)

    # correctness: the 400-day-old rows are excluded from the 7-day view
    assert prod["good"] == 90 and prod["total"] == 100 and prod["good_rate"] == 90   # ancient 999s not counted
    assert len(prod["daily"]) == 7
    assert sum(d["count"] for d in dt) == 1 and len(dt) == 7                          # only today's downtime

    # the fix: each drill-down SELECT carries a created_at lower bound (bounded in
    # SQL), so the ancient rows are never loaded. Without the bound this fails.
    prod_selects = [s for s in stmts if "from production_records" in _norm(s)]
    dt_selects = [s for s in stmts if "from downtime_logs" in _norm(s)]
    assert prod_selects and all("created_at >=" in _norm(s) for s in prod_selects), prod_selects
    assert dt_selects and all("created_at >=" in _norm(s) for s in dt_selects), dt_selects
    print("PASS cockpit downtime/production drill-downs are windowed in SQL (ancient rows never loaded)")


def test_twin_composes_health_and_is_tenant_scoped():
    db = _fresh_session()
    # critical machine: breakdown (+35) + util<40 (+20) + downtime>=120 (+25) = 80 risk -> health 20
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30, line="SMT"))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    db.add(models.MaintenanceTask(tenant_code="DEFAULT", task_no="AUTO-1", machine_id=1,
                                  task_type="Predictive (auto)", priority="Critical",
                                  assigned_to="x", planned_date=datetime.utcnow().date(), status="Open"))
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="x", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    # healthy machine
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=65))
    # another tenant's action targeting machine 1 must NOT be counted for DEFAULT
    db.add(models.AgentAction(tenant_code="GMATS", agent="maintenance", action_type="open_task",
                              summary="x", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    db.commit()

    twins = twin.build_twins(db, "DEFAULT")
    assert len(twins) == 2
    assert twins[0]["machine_id"] == 1                              # worst health first
    assert twins[0]["line"] == "SMT"                                # production line flows through
    assert twins[0]["health_score"] == 20 and twins[0]["risk_score"] == 80
    assert twins[0]["health_band"] == "Critical"
    assert twins[0]["open_maintenance_tasks"] == 1
    assert twins[0]["pending_agent_actions"] == 1                   # GMATS action excluded
    assert len(twins[0]["recent_downtime"]) == 1
    assert twins[1]["machine_id"] == 2 and twins[1]["health_band"] == "Healthy"
    assert "oee" in twins[0] and twins[0]["oee"]["has_data"] is False   # no production -> zeroed OEE


def test_machine_detail_composes_cockpit_and_scopes_actions():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    db.add(models.MaintenanceTask(tenant_code="DEFAULT", task_no="AUTO-1", machine_id=1,
                                  task_type="Predictive (auto)", priority="Critical",
                                  assigned_to="x", planned_date=datetime.utcnow().date(), status="Proposed"))
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="Open a Critical task", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    # another tenant's action on the same machine must not leak into the cockpit
    db.add(models.AgentAction(tenant_code="GMATS", agent="maintenance", action_type="open_task",
                              summary="leak", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90, rejected_count=10))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=50, passed_quantity=45, failed_quantity=5, defect_category="surface"))
    db.commit()

    detail = twin.build_machine_detail(db, "DEFAULT", 1)
    assert detail["machine_id"] == 1 and detail["health_band"] == "Critical"
    assert detail["risk_factors"]                                     # non-empty risk breakdown
    assert len(detail["downtime_7d"]) == 7 and detail["downtime_7d"][-1]["count"] == 1  # today's downtime
    assert len(detail["production_7d"]["daily"]) == 7                 # per-machine throughput series
    assert detail["production_7d"]["good"] == 90 and detail["production_7d"]["good_rate"] == 90
    assert detail["quality"]["inspections"] == 1 and detail["quality"]["fail_rate"] == 10
    assert detail["quality"]["top_defects"][0]["category"] == "surface"
    # OEE = A x P x Q: availability 440/480 -> 92%, quality 90/100 -> 90%
    assert detail["oee"]["has_data"] and detail["oee"]["availability"] == 92 and detail["oee"]["quality"] == 90
    assert 0 <= detail["oee"]["oee"] <= 100
    kinds = {e["kind"] for e in detail["timeline"]}
    assert kinds == {"downtime", "task", "action"}                   # all three merged
    assert all(e["detail"] != "leak" for e in detail["timeline"])    # GMATS action excluded
    assert len(detail["open_actions"]) == 1                          # only DEFAULT's proposed action
    assert detail["open_actions"][0]["summary"] == "Open a Critical task"

    assert twin.build_machine_detail(db, "DEFAULT", 999) is None      # unknown machine -> caller 404s


if __name__ == "__main__":
    test_twin_composes_health_and_is_tenant_scoped()
    test_machine_detail_composes_cockpit_and_scopes_actions()
    test_cockpit_drilldowns_are_windowed_in_sql_not_loaded_whole()
    print("TWIN OK: health twin + single-machine cockpit (risk factors, timeline, open actions); worst first; tenant-scoped")
