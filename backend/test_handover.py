"""Shift-handover read-model tests (ADR-0007).

Composes the briefing + production read-models plus open-item counts into an
end-of-shift summary. Run:  python backend/test_handover.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import handover, escalations


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


def test_open_escalation_count_counts_null_status_and_reconciles_with_queue():
    """The carry-over count must include a NULL-status open escalation and use the
    SAME open definition as the escalation-queue read-model — a whitelist of
    exact status strings dropped both a NULL status and any non-terminal drift."""
    db = _fresh_session()

    # Five escalations spanning the states a real table holds:
    #   Open          -> open
    #   In Progress   -> open
    #   NULL status   -> open (default="Open" is not nullable=False; a raw-SQL /
    #                    migration / cleared-field row can hold a genuine NULL)
    #   Resolved      -> closed
    #   Cancelled     -> closed
    db.add(models.Escalation(tenant_code="DEFAULT", title="a", severity="High",
                             owner="o", department="Maintenance", status="Open", source="Manual"))
    db.add(models.Escalation(tenant_code="DEFAULT", title="b", severity="Medium",
                             owner="o", department="Production", status="In Progress", source="Manual"))
    db.add(models.Escalation(tenant_code="DEFAULT", title="c", severity="Critical",
                             owner="o", department="Maintenance", status=None, source="Manual"))
    db.add(models.Escalation(tenant_code="DEFAULT", title="d", severity="Low",
                             owner="o", department="Quality", status="Resolved", source="Manual"))
    db.add(models.Escalation(tenant_code="DEFAULT", title="e", severity="Low",
                             owner="o", department="Quality", status="Cancelled", source="Manual"))
    db.commit()

    h = handover.build_handover(db, "DEFAULT")

    # Independently derived: 3 open (Open, In Progress, NULL), 2 closed.
    assert h["open_work"]["open_escalations"] == 3, h["open_work"]["open_escalations"]

    # Reconciles with the escalation-queue read-model's own "open" count (rule 3) —
    # the two surfaces show the same handover page and must agree.
    queue = escalations.build_escalation_summary(db, "DEFAULT")
    assert h["open_work"]["open_escalations"] == queue["open"], (
        h["open_work"]["open_escalations"], queue["open"])

    # A resolved/cancelled-only plant carries nothing over.
    closed_only = _fresh_session()
    closed_only.add(models.Escalation(tenant_code="DEFAULT", title="z", severity="Low",
                                      owner="o", department="Quality", status="Resolved", source="Manual"))
    closed_only.commit()
    assert handover.build_handover(closed_only, "DEFAULT")["open_work"]["open_escalations"] == 0


if __name__ == "__main__":
    test_handover_summarises_output_open_work_and_attention()
    test_open_escalation_count_counts_null_status_and_reconciles_with_queue()
    print("HANDOVER OK: output + OEE, open work to carry over (approvals + escalations), attention list + wins; "
          "composed from briefing/production; empty-safe; open-escalation count is NULL-safe and "
          "reconciles with the escalation-queue read-model")
