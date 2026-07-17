"""Agent Impact read-model tests (ADR-0005).

The impact rollup counts the fleet's concrete outputs (tasks/POs/escalations
that weren't rejected), the autonomy rate, the human decision backlog, and a
7-day window — all tenant-scoped.

Run:  python backend/test_impact.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import impact


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, ref_kind, status, decided_by=None, tenant="DEFAULT"):
    return models.AgentAction(tenant_code=tenant, agent=agent, action_type="x", summary="x",
                              ref_kind=ref_kind, ref_id=1, status=status, decided_by=decided_by)


def test_impact_rolls_up_outputs_autonomy_and_backlog():
    db = _fresh_session()
    db.add_all([
        _action("reorder", "purchase_order", "Approved", "auto-policy"),   # auto-approved output
        _action("maintenance", "maintenance_task", "Proposed"),            # pending output + backlog
        _action("quality", "maintenance_task", "Approved", "alice"),       # human-approved output
        _action("escalation", "escalation", "Rejected", "bob"),            # rejected -> not an output
        _action("reorder", "purchase_order", "Approved", "auto-policy", tenant="GMATS"),  # other tenant
    ])
    db.commit()

    imp = impact.build_impact(db, "DEFAULT")
    assert imp["total_actions"] == 4                                       # GMATS action excluded
    assert set(imp["agents_active"]) == {"reorder", "maintenance", "quality", "escalation"}
    assert imp["auto_approved"] == 1 and imp["pending_backlog"] == 1
    # outputs: PO(approved)=1, maintenance_task(proposed+approved)=2, escalation(rejected)=0
    assert imp["outputs"]["purchase_orders"] == 1
    assert imp["outputs"]["maintenance_tasks"] == 2
    assert imp["outputs"]["escalations"] == 0
    # decisions = approved(2) + rejected(1) = 3; auto(1)/3 -> 33%
    assert imp["auto_rate"] == 33
    assert imp["last_7_days"]["total"] == 4                                # all just created
    assert "agent" in imp["headline"]
    # per-agent contribution (ROI view): reorder led with 1 auto-approved PO
    by_agent = {a["agent"]: a for a in imp["by_agent"]}
    assert set(by_agent) == {"reorder", "maintenance", "quality", "escalation"}
    assert by_agent["reorder"]["actions"] == 1 and by_agent["reorder"]["auto_approved"] == 1
    assert by_agent["reorder"]["outputs"]["purchase_orders"] == 1
    assert by_agent["maintenance"]["pending"] == 1
    assert by_agent["quality"]["approved"] == 1
    assert all(a["name"] for a in imp["by_agent"])                         # display names present

    # empty tenant -> zeroed, no divide-by-zero
    empty = impact.build_impact(db, "NOBODY")
    assert empty["total_actions"] == 0 and empty["auto_rate"] == 0 and empty["agents_active"] == []
    assert empty["by_agent"] == []


if __name__ == "__main__":
    test_impact_rolls_up_outputs_autonomy_and_backlog()
    print("IMPACT OK: outputs + autonomy rate + backlog + 7-day window; tenant-scoped; no divide-by-zero")
