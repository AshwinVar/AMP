"""The two decision drawers get the server's expiry answer (ADR-0015 addendum).

An undecided proposal past its expiry can only be rejected: approvals.authorise
refuses an approve with 409, and reject is the way to release the item it holds.
The Approvals inbox and Mission Control already read an `expired` flag for this.
The agent drawer (ai/roster.build_agent_detail "recent") and the machine cockpit
drawer (ai/twin._open_actions) did not carry one, so both offered Approve on an
expired proposal and the approver found out only from the refusal.

Both now carry `expired` from approvals.is_expired, the one rule, so the drawers
disable Approve and say why, as the other two surfaces do.

Run:  python backend/test_drawers_know_an_expired_proposal.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from ai import roster, twin
from database import Base

TENANT = "ACME"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=80, tenant_code=TENANT))
    now = datetime.utcnow()
    common = dict(tenant_code=TENANT, agent="maintenance", action_type="open_task",
                  ref_kind="maintenance_task", severity="High", related_machine_id=1)
    db.add(models.AgentAction(id=1, summary="fresh", status="Proposed", ref_id=11,
                              created_at=now - timedelta(hours=1),
                              expires_at=now + timedelta(days=2), **common))
    db.add(models.AgentAction(id=2, summary="stale", status="Proposed", ref_id=12,
                              created_at=now - timedelta(days=9),
                              expires_at=now - timedelta(days=1), **common))
    db.add(models.AgentAction(id=3, summary="decided", status="Approved", ref_id=13,
                              created_at=now - timedelta(days=9),
                              expires_at=now - timedelta(days=1), decided_by="boss",
                              decided_at=now - timedelta(days=8), **common))
    db.commit()
    return db


def test_the_agent_drawer_says_which_proposals_expired():
    detail = roster.build_agent_detail(_db(), TENANT, "maintenance")
    by_id = {a["id"]: a for a in detail["recent"]}
    assert by_id[1]["expired"] is False, by_id[1]
    assert by_id[2]["expired"] is True, by_id[2]
    # a decided action is not "expired": it is not waiting for anyone
    assert by_id[3]["expired"] is None, by_id[3]
    print("PASS the agent drawer's recent actions carry the server's expiry answer")


def test_the_machine_drawer_says_which_proposals_expired():
    rows = twin._open_actions(_db(), 1, TENANT)
    by_id = {a["id"]: a for a in rows}
    assert set(by_id) == {1, 2}, by_id                  # only undecided proposals are "open"
    assert by_id[1]["expired"] is False and by_id[2]["expired"] is True, by_id
    print("PASS the machine drawer's open actions carry the server's expiry answer")


def test_both_use_the_one_expiry_rule():
    """A NULL expires_at falls back to the age limit inside approvals.is_expired; a
    second copy of the rule in either drawer payload would read it as 'never'."""
    db = _db()
    old = db.query(models.AgentAction).filter(models.AgentAction.id == 1).one()
    old.expires_at = None
    old.created_at = datetime.utcnow() - timedelta(days=400)
    db.commit()
    assert {a["id"]: a for a in roster.build_agent_detail(db, TENANT, "maintenance")["recent"]}[1]["expired"] is True
    assert {a["id"]: a for a in twin._open_actions(db, 1, TENANT)}[1]["expired"] is True
    print("PASS a NULL expiry falls back to the age limit in both payloads")


if __name__ == "__main__":
    test_the_agent_drawer_says_which_proposals_expired()
    test_the_machine_drawer_says_which_proposals_expired()
    test_both_use_the_one_expiry_rule()
    print("ALL DRAWER EXPIRY TESTS PASSED")
