"""Machine Health twin tests (ADR-0006 / PR #21).

The twin composes one snapshot per machine (state + health score + downtime +
open tasks + pending agent actions), worst health first, tenant-scoped.

Run:  python backend/test_twin.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import twin


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_twin_composes_health_and_is_tenant_scoped():
    db = _fresh_session()
    # critical machine: breakdown (+35) + util<40 (+20) + downtime>=120 (+25) = 80 risk -> health 20
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    db.add(models.MaintenanceTask(tenant_code="DEFAULT", task_no="AUTO-1", machine_id=1,
                                  task_type="Predictive (auto)", priority="Critical",
                                  assigned_to="x", planned_date=date.today(), status="Open"))
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
    assert twins[0]["health_score"] == 20 and twins[0]["risk_score"] == 80
    assert twins[0]["health_band"] == "Critical"
    assert twins[0]["open_maintenance_tasks"] == 1
    assert twins[0]["pending_agent_actions"] == 1                   # GMATS action excluded
    assert len(twins[0]["recent_downtime"]) == 1
    assert twins[1]["machine_id"] == 2 and twins[1]["health_band"] == "Healthy"


if __name__ == "__main__":
    test_twin_composes_health_and_is_tenant_scoped()
    print("TWIN OK: per-machine health twin composes state + risk + tasks + actions; worst first; tenant-scoped")
