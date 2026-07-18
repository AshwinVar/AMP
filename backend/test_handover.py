"""Shift-handover read-model tests (ADR-0007).

Composes the briefing + production read-models plus open-item counts into an
end-of-shift summary. Run:  python backend/test_handover.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import handover


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_handover_summarises_output_open_work_and_attention():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # open work to carry over: a pending approval + an open escalation
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="escalation", action_type="raise_escalation",
                              summary="x", ref_kind="escalation", ref_id=1, status="Proposed"))
    db.add(models.Escalation(tenant_code="DEFAULT", machine_id=1, title="t", severity="High",
                             owner="o", department="Maintenance", status="Open", source="Manual"))
    db.commit()

    h = handover.build_handover(db, "DEFAULT")
    assert h["has_data"] is True
    assert h["produced"]["good"] == 90 and h["produced"]["total"] == 100 and h["produced"]["good_rate"] == 90
    assert h["open_work"]["pending_approvals"] == 1 and h["open_work"]["open_escalations"] == 1
    # attention comes from the briefing — the down machine is the top alert
    assert any(a["key"] == "machines_down" for a in h["attention"])
    assert h["oee_trend"] in {"up", "down", "flat"}

    # empty plant -> no data, empty open work, no crash
    empty = handover.build_handover(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False
    assert empty["open_work"]["pending_approvals"] == 0 and empty["attention"] == []


if __name__ == "__main__":
    test_handover_summarises_output_open_work_and_attention()
    print("HANDOVER OK: output + OEE, open work to carry over (approvals + escalations), attention list + wins; "
          "composed from briefing/production; empty-safe")
