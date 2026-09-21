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
    # avg_health is the mean of the MEASURED twins' health scores (composition
    # contract): CNC-02 has nothing recorded, so its 100 is an absence and is
    # left out — averaging it in read "Fleet health 60" for a fleet whose one
    # measured machine is at 20.
    ts = twin.build_twins(db, "DEFAULT")
    measured = [t for t in ts if t["health_measured"]]
    assert [t["machine_id"] for t in measured] == [1], ts
    assert p["fleet"]["machines"] == 2 and p["fleet"]["measured"] == 1
    assert p["fleet"]["avg_health"] == round(sum(t["health_score"] for t in measured) / len(measured)) == 20
    assert p["fleet"]["needs_attention"] == 1                  # only the critical one
    assert p["fleet"]["worst"]["machine_id"] == 1 and p["fleet"]["worst"]["health_band"] == "Critical"
    assert p["agents"]["awaiting_you"] == 1                    # GMATS action excluded (stamped-tenant filter)
    assert p["agents"]["agents_active"] == 1
    assert p["headline"].startswith("Fleet health 20 (1 of 2 measured)") and "awaiting you" in p["headline"], p["headline"]

    # a brand-new (empty) factory -> no health figure (None, never 0: 0 is the
    # worst score there is), no divide-by-zero, "all clear"
    empty = pulse.build_pulse(_fresh_session(), "DEFAULT")
    assert empty["fleet"]["machines"] == 0 and empty["fleet"]["measured"] == 0
    assert empty["fleet"]["avg_health"] is None
    assert empty["fleet"]["worst"] is None
    assert empty["headline"].startswith("Fleet health not measured") and "all clear" in empty["headline"]

    # machines but nothing recorded for any of them -> not measured, said so
    silent_db = _fresh_session()
    silent_db.add(models.Machine(id=5, name="NEW-01", status="Running", utilization=70))
    silent_db.commit()
    silent = pulse.build_pulse(silent_db, "DEFAULT")
    assert silent["fleet"]["machines"] == 1 and silent["fleet"]["measured"] == 0
    assert silent["fleet"]["avg_health"] is None
    assert "nothing recorded yet" in silent["headline"], silent["headline"]


if __name__ == "__main__":
    test_pulse_composes_fleet_health_and_agent_workload()
    print("PULSE OK: composes fleet health + agent workload; worst machine; tenant-scoped; no divide-by-zero")
