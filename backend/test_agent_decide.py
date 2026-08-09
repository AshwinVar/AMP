"""Agent approve/reject decision guard (_decide_agent_action).

The human oversight decision (POST /agent-actions/{id}/approve|reject) may only
advance an action that is still "Proposed". The guard that enforces that used to
render the rejection message with `action.status.lower()` — which raised
AttributeError -> an unhandled 500 when the status column was a genuine NULL.

AgentAction.status is Column(String, default="Proposed") WITHOUT nullable=False,
so a row written by raw SQL / a migration / a legacy path can hold NULL, and
`None != "Proposed"` steers exactly such a row into that message. This pins:

  * a NULL-status action returns a clean 400 ("Already decided"), NOT a 500;
  * an already-decided (Approved/Rejected) action returns 400 naming its state;
  * a genuinely Proposed action is still decided (approve -> Approved,
    reject -> Rejected, with the decider stamped);
  * a missing / cross-tenant action is a 404 (tenant scoping holds).

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_agent_decide.py
"""
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import ai.agents  # noqa: F401  — bind the ai.agents submodule the handler calls
import models
from database import Base
from agent_routes import _decide_agent_action


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    # alice is the authenticated caller in every case below, so she must exist
    # as a real, active, approving user of DEFAULT — approvals.authorise
    # re-verifies the approver against the DATABASE, not the token. Seeding her
    # also makes the refusal cases stricter: a refusal can now only come from
    # the state guard under test, never from an actor who happens not to exist.
    db.add(models.User(username="alice", password="x", role="Admin",
                       tenant_code="DEFAULT", is_active=True))
    db.commit()
    return db


def _action(status, agent="maintenance", decided_by=None, tenant="DEFAULT"):
    return models.AgentAction(
        tenant_code=tenant, agent=agent, action_type="open_task",
        summary="x", ref_kind="maintenance_task", ref_id=None,
        status=status, decided_by=decided_by,
    )


def _user(tenant="DEFAULT"):
    return {"tenant": tenant, "sub": "alice", "username": "alice"}


def _expect_http(fn, status_code, detail):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == status_code, (e.status_code, e.detail)
        assert e.detail == detail, e.detail
        return
    raise AssertionError(f"expected HTTPException {status_code}/{detail!r}, none raised")


def test_null_status_is_400_not_500():
    # A row with a genuine NULL status (as a bulk insert / migration-added column
    # would leave it). The old code did `None.lower()` here -> AttributeError 500.
    db = _fresh_session()
    db.execute(text(
        "INSERT INTO agent_actions (tenant_code, agent, action_type, summary, ref_kind, status) "
        "VALUES ('DEFAULT', 'quality', 'open_task', 'x', 'maintenance_task', NULL)"
    ))
    db.commit()
    action_id = db.query(models.AgentAction).one().id

    # Independently derived: status is None -> coalesced to "decided" -> lowercased.
    _expect_http(
        lambda: _decide_agent_action(action_id, "approve", db, _user()),
        400, "Already decided",
    )
    # The row must be untouched by the refused decision.
    row = db.query(models.AgentAction).one()
    assert row.status is None, row.status
    assert row.decided_by is None, row.decided_by
    print("PASS a NULL-status action returns 400 'Already decided' (no 500)")


def test_already_decided_names_its_state():
    db = _fresh_session()
    db.add_all([_action("Approved", decided_by="bob"), _action("Rejected", decided_by="bob")])
    db.commit()
    approved, rejected = db.query(models.AgentAction).order_by(models.AgentAction.id).all()

    _expect_http(lambda: _decide_agent_action(approved.id, "approve", db, _user()),
                 400, "Already approved")
    _expect_http(lambda: _decide_agent_action(rejected.id, "reject", db, _user()),
                 400, "Already rejected")
    print("PASS an already-decided action returns 400 naming its state")


def test_proposed_action_is_decided():
    db = _fresh_session()
    db.add_all([_action("Proposed"), _action("Proposed")])
    db.commit()
    a, b = db.query(models.AgentAction).order_by(models.AgentAction.id).all()

    out = _decide_agent_action(a.id, "approve", db, _user())
    assert out["status"] == "Approved", out
    assert out["decided_by"] == "alice", out
    assert db.get(models.AgentAction, a.id).status == "Approved"

    out = _decide_agent_action(b.id, "reject", db, _user())
    assert out["status"] == "Rejected", out
    assert db.get(models.AgentAction, b.id).status == "Rejected"
    print("PASS a Proposed action is approved/rejected and stamped")


def test_missing_and_cross_tenant_are_404():
    db = _fresh_session()
    db.add(_action("Proposed", tenant="OTHER"))
    db.commit()
    other_id = db.query(models.AgentAction).one().id

    # Unknown id.
    _expect_http(lambda: _decide_agent_action(999999, "approve", db, _user()),
                 404, "Agent action not found")
    # Another tenant's action is invisible to DEFAULT -> 404, not decided.
    _expect_http(lambda: _decide_agent_action(other_id, "approve", db, _user("DEFAULT")),
                 404, "Agent action not found")
    assert db.get(models.AgentAction, other_id).status == "Proposed"
    print("PASS a missing / cross-tenant action is a 404 (tenant scoping holds)")


if __name__ == "__main__":
    test_null_status_is_400_not_500()
    test_already_decided_names_its_state()
    test_proposed_action_is_decided()
    test_missing_and_cross_tenant_are_404()
    print("ALL AGENT DECIDE TESTS PASSED")
