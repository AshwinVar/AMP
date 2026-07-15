"""Factory Pulse read-model tests (ADR-0006).

The pulse composes fleet health (from the twins) with the agent workload (from
the impact rollup) into one command-header snapshot, tenant-scoped.

Run:  python backend/test_pulse.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import pulse, twin


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_pulse_composes_fleet_health_and_agent_workload():
    db = _fresh_session()
    # one critical machine (breakdown + util<40 + downtime>=120 -> risk 80 -> health 20)
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.add(models.DowntimeLog(machine_id=1, reason="Wear", duration="120 min"))
    # one healthy machine (health 100)
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=65))
    # an agent action awaiting a human (the approval backlog)
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="Open a Critical task", ref_kind="maintenance_task", ref_id=1,
                              related_machine_id=1, status="Proposed"))
    # another tenant's machine/action must not bleed into DEFAULT's pulse
    db.add(models.AgentAction(tenant_code="GMATS", agent="reorder", action_type="draft_po",
                              summary="x", ref_kind="purchase_order", ref_id=9, status="Proposed"))
    db.commit()

    p = pulse.build_pulse(db, "DEFAULT")
    # avg_health is the mean of the twins' health scores (composition contract)
    ts = twin.build_twins(db, "DEFAULT")
    assert p["fleet"]["machines"] == 2
    assert p["fleet"]["avg_health"] == round(sum(t["health_score"] for t in ts) / len(ts))
    assert p["fleet"]["needs_attention"] == 1                  # only the critical one
    assert p["fleet"]["worst"]["machine_id"] == 1 and p["fleet"]["worst"]["health_band"] == "Critical"
    assert p["agents"]["awaiting_you"] == 1                    # GMATS action excluded (stamped-tenant filter)
    assert p["agents"]["agents_active"] == 1
    assert p["headline"].startswith("Fleet health ") and "awaiting you" in p["headline"]

    # a brand-new (empty) factory -> zeroed, no divide-by-zero, "all clear"
    empty = pulse.build_pulse(_fresh_session(), "DEFAULT")
    assert empty["fleet"]["machines"] == 0 and empty["fleet"]["avg_health"] == 0
    assert empty["fleet"]["worst"] is None
    assert "all clear" in empty["headline"]


if __name__ == "__main__":
    test_pulse_composes_fleet_health_and_agent_workload()
    print("PULSE OK: composes fleet health + agent workload; worst machine; tenant-scoped; no divide-by-zero")
