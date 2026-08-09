"""The backend is authoritative about approvals (ADR-0015).

WHAT WAS MEASURED ON MASTER
---------------------------
An adversarial probe, one attempt per row:

    1. another tenant's Admin approves : refused 404      PO stays Draft
    2. role guard declared on the route: True             Admin/Supervisor
    3. the SAME approve replayed       : refused 400      PO stays Approved
    4. reject an already-approved one  : refused 400      PO stays Approved
    5. ai.agents.apply_decision() direct: ACCEPTED        PO -> Approved
    6. apply_decision() a SECOND time  : action Rejected, PO still Approved
    7. a DELETED user approves         : ACCEPTED         PO -> Approved
    8. a 400-day-old proposal          : ACCEPTED         PO -> Approved

Rows 1-4 held. Rows 5-8 did not. Row 6 is the worst: the AgentAction flipped to
"Rejected" while the purchase order stayed "Approved", so the audit trail
recorded a rejection of something the system had gone ahead with.

Run: DATABASE_URL="sqlite:///./ci.db" python test_approval_gate.py
"""
import inspect
import os
import sys
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agent_routes
import ai.agents
import approvals
import models
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []
_open = []
_seq = [0]


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    _open.append(db)
    tenancy.install_scoping()
    return db


def seed_user(db, tenant, name, role, active=True):
    tok = tenancy.set_current_tenant(None)
    if not db.query(models.User).filter(models.User.username == name).first():
        db.add(models.User(username=name, password="x", role=role,
                           tenant_code=tenant, is_active=active))
        db.commit()
    tenancy.reset_current_tenant(tok)
    return {"sub": name, "role": role, "tenant": tenant}


def propose(db, tenant="FACTORY_A", created_at=None, expires_at=None):
    """A Proposed action plus the Draft purchase order it would advance."""
    _seq[0] += 1
    tok = tenancy.set_current_tenant(None)
    po = models.PurchaseOrder(
        tenant_code=tenant, po_no=f"PO-{tenant}-{_seq[0]}", item_name="Steel",
        order_quantity=10, unit="kg",
        expected_delivery_date=datetime.utcnow().date(), status="Draft")
    db.add(po)
    db.flush()
    a = models.AgentAction(tenant_code=tenant, agent="reorder",
                           action_type="draft_po", summary="restock",
                           ref_kind="purchase_order", ref_id=po.id,
                           status="Proposed",
                           created_at=created_at or datetime.utcnow(),
                           expires_at=expires_at)
    db.add(a)
    db.commit()
    ids = (a.id, po.id)
    tenancy.reset_current_tenant(tok)
    return ids


def po_status(db, po_id):
    tok = tenancy.set_current_tenant(None)
    s = db.query(models.PurchaseOrder).filter(
        models.PurchaseOrder.id == po_id).first().status
    tenancy.reset_current_tenant(tok)
    return s


def action_status(db, aid):
    tok = tenancy.set_current_tenant(None)
    s = db.query(models.AgentAction).filter(
        models.AgentAction.id == aid).first().status
    tenancy.reset_current_tenant(tok)
    return s


def approve(db, tenant, aid, actor):
    tok = tenancy.set_current_tenant(tenant)
    try:
        return agent_routes.approve_agent_action(aid, db=db, current_user=actor)
    finally:
        tenancy.reset_current_tenant(tok)


def refused(fn, *a, **kw):
    """(was_refused, status_code)."""
    try:
        fn(*a, **kw)
        return False, None
    except HTTPException as e:
        return True, e.status_code


# =====================================================================
def test_the_gate_refuses_every_bypass():
    db = fresh_db()
    print("=" * 74)
    print("1. THE BYPASSES THAT USED TO WORK")
    print("=" * 74)

    admin = seed_user(db, "FACTORY_A", "a-admin", "Admin")

    # --- 5: reaching past the route entirely -----------------------------
    aid, pid = propose(db)
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    was, code = refused(ai.agents.apply_decision, db, action, "approve",
                        decided_by="not-a-human")
    db.rollback()
    tenancy.reset_current_tenant(tok)
    check("apply_decision() called directly, with no actor, is refused", was,
          f"ACCEPTED, PO now {po_status(db, pid)}")
    check("...and the purchase order was not advanced",
          po_status(db, pid) == "Draft", po_status(db, pid))

    # --- 6: the audit record can no longer contradict the item -----------
    aid, pid = propose(db)
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    ai.agents.apply_decision(db, action, "approve", decided_by="a-admin", actor=admin)
    db.commit()
    first_action, first_po = action.status, po_status(db, pid)
    was, code = refused(ai.agents.apply_decision, db, action, "reject",
                        decided_by="second-caller", actor=admin)
    db.rollback()
    tenancy.reset_current_tenant(tok)
    check("a SECOND apply_decision() on the same action is refused", was, str(code))
    check("the audit record still says Approved, matching the purchase order",
          action_status(db, aid) == "Approved" and po_status(db, pid) == "Approved",
          f"action={action_status(db, aid)} po={po_status(db, pid)} "
          f"(was {first_action}/{first_po})")

    # --- 7: a deleted user -----------------------------------------------
    aid, pid = propose(db)
    tok = tenancy.set_current_tenant(None)
    db.add(models.User(username="leaver", password="x", role="Admin",
                       tenant_code="FACTORY_A", is_active=True))
    db.commit()
    db.query(models.User).filter(models.User.username == "leaver").delete(
        synchronize_session=False)
    db.commit()
    tenancy.reset_current_tenant(tok)
    was, code = refused(approve, db, "FACTORY_A", aid,
                        {"sub": "leaver", "role": "Admin", "tenant": "FACTORY_A"})
    check("a DELETED user's token cannot approve", was and code == 401, str(code))
    check("...and the purchase order was not advanced",
          po_status(db, pid) == "Draft", po_status(db, pid))

    # --- 7b: a disabled user ---------------------------------------------
    aid, pid = propose(db)
    seed_user(db, "FACTORY_A", "suspended", "Admin", active=False)
    was, code = refused(approve, db, "FACTORY_A", aid,
                        {"sub": "suspended", "role": "Admin", "tenant": "FACTORY_A"})
    check("a DISABLED user cannot approve", was and code == 403, str(code))

    # --- 7c: the token lies about the role -------------------------------
    aid, pid = propose(db)
    seed_user(db, "FACTORY_A", "operator", "Operator")
    was, code = refused(approve, db, "FACTORY_A", aid,
                        {"sub": "operator", "role": "Admin", "tenant": "FACTORY_A"})
    check("a token claiming Admin for a database Operator cannot approve",
          was and code == 403, str(code))

    # --- 7d: the token lies about the tenant -----------------------------
    aid, pid = propose(db, "FACTORY_A")
    seed_user(db, "FACTORY_B", "b-admin", "Admin")
    was, code = refused(approve, db, "FACTORY_A", aid,
                        {"sub": "b-admin", "role": "Admin", "tenant": "FACTORY_A"})
    check("an Admin of ANOTHER tenant cannot approve, even claiming this one",
          was and code == 404, str(code))

    # --- 8: staleness -----------------------------------------------------
    aid, pid = propose(db, created_at=datetime.utcnow() - timedelta(days=400))
    was, code = refused(approve, db, "FACTORY_A", aid, admin)
    check("a 400-day-old proposal cannot be approved", was and code == 409, str(code))
    check("...and the purchase order was not advanced",
          po_status(db, pid) == "Draft", po_status(db, pid))

    aid, pid = propose(db, expires_at=datetime.utcnow() - timedelta(minutes=1))
    was, code = refused(approve, db, "FACTORY_A", aid, admin)
    check("an explicitly expired proposal cannot be approved",
          was and code == 409, str(code))

    # --- the ORDER of the checks is itself a guard -------------------------
    # Tenant is verified FIRST, before state and freshness. If it were not, a
    # caller from another tenant would learn the state of an action that is not
    # theirs: 400 "Already approved" and 409 "expired" are different answers,
    # and the difference is information. Everything outside their tenant must
    # give one answer — 404.
    aid, pid = propose(db, "FACTORY_A")
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    ai.agents.apply_decision(db, action, "approve", decided_by="a-admin", actor=admin)
    db.commit()
    was, code = refused(ai.agents.apply_decision, db, action, "approve",
                        decided_by="b-admin",
                        actor={"sub": "b-admin", "role": "Admin",
                               "tenant": "FACTORY_B"})
    db.rollback()
    tenancy.reset_current_tenant(tok)
    check("a foreign tenant gets 404, not the action's state (no state leak)",
          was and code == 404, f"{code} — leaks that the action is decided")


def test_the_gate_still_lets_real_work_through():
    """CONTROL. Every assertion above is satisfied by a gate that refuses
    everything, so these must pass or the suite proves nothing."""
    db = fresh_db()
    print()
    print("=" * 74)
    print("2. CONTROL — LEGITIMATE APPROVAL STILL WORKS")
    print("=" * 74)

    admin = seed_user(db, "FACTORY_A", "a-admin", "Admin")
    aid, pid = propose(db)
    approve(db, "FACTORY_A", aid, admin)
    check("a valid Admin approves a fresh proposal",
          po_status(db, pid) == "Approved" and action_status(db, aid) == "Approved",
          f"po={po_status(db, pid)} action={action_status(db, aid)}")

    sup = seed_user(db, "FACTORY_A", "a-sup", "Supervisor")
    aid, pid = propose(db)
    approve(db, "FACTORY_A", aid, sup)
    check("a Supervisor may approve too", po_status(db, pid) == "Approved",
          po_status(db, pid))

    aid, pid = propose(db)
    tok = tenancy.set_current_tenant("FACTORY_A")
    agent_routes.reject_agent_action(aid, db=db, current_user=admin)
    tenancy.reset_current_tenant(tok)
    check("a rejection cancels the purchase order",
          po_status(db, pid) == "Cancelled" and action_status(db, aid) == "Rejected",
          f"po={po_status(db, pid)} action={action_status(db, aid)}")

    # The auto-approval path: a tenant declared IN ADVANCE that this agent may
    # act unattended. That is a human decision, made earlier — the gate must
    # still allow it, or the reorder agent stops working entirely.
    aid, pid = propose(db)
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    ai.agents.apply_decision(db, action, "approve", decided_by="auto-policy",
                             require_actor=False)
    db.commit()
    tenancy.reset_current_tenant(tok)
    check("the auto-approval path still works",
          po_status(db, pid) == "Approved", po_status(db, pid))
    check("...and is attributed to the policy, not to a person",
          action_status(db, aid) == "Approved", action_status(db, aid))

    # ...but auto-approval does NOT bypass freshness.
    aid, pid = propose(db, created_at=datetime.utcnow() - timedelta(days=400))
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    was, code = refused(ai.agents.apply_decision, db, action, "approve",
                        decided_by="auto-policy", require_actor=False)
    db.rollback()
    tenancy.reset_current_tenant(tok)
    check("auto-approval does NOT bypass the freshness check",
          was and code == 409, str(code))

    # A misspelled decision must be refused, not silently treated as a
    # rejection. apply_decision computes `approve = decision == "approve"`, so
    # without this guard "aprove" cancels the purchase order — the opposite of
    # what the caller asked for, with no error to notice.
    aid, pid = propose(db)
    tok = tenancy.set_current_tenant(None)
    action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
    was, code = refused(ai.agents.apply_decision, db, action, "aprove",
                        decided_by="a-admin", actor=admin)
    db.rollback()
    tenancy.reset_current_tenant(tok)
    check("a misspelled decision is refused, not read as a rejection",
          was and code == 400, f"{code}, PO now {po_status(db, pid)}")
    check("...and the purchase order was not cancelled",
          po_status(db, pid) == "Draft", po_status(db, pid))


def test_expiry_semantics():
    db = fresh_db()
    print()
    print("=" * 74)
    print("3. WHEN IS A PROPOSAL STALE?")
    print("=" * 74)
    now = datetime(2026, 8, 9, 12, 0, 0)

    class A:
        def __init__(s, created_at=None, expires_at=None):
            s.created_at, s.expires_at = created_at, expires_at

    check("an explicit expiry in the future is fresh",
          approvals.is_expired(A(now - timedelta(days=1), now + timedelta(hours=1)),
                               now) is False)
    check("an explicit expiry in the past is stale",
          approvals.is_expired(A(now - timedelta(days=1), now - timedelta(hours=1)),
                               now) is True)
    check("an explicit expiry EXACTLY now is stale (>= , not >)",
          approvals.is_expired(A(now - timedelta(days=1), now), now) is True)
    check(f"no explicit expiry falls back to {approvals.DEFAULT_TTL_DAYS} days",
          approvals.is_expired(A(now - timedelta(days=approvals.DEFAULT_TTL_DAYS - 1)),
                               now) is False)
    check("...and past that it is stale",
          approvals.is_expired(A(now - timedelta(days=approvals.DEFAULT_TTL_DAYS + 1)),
                               now) is True)
    # A row with neither is unmeasurable. Fail CLOSED: an action whose age
    # cannot be established must not be eternally actionable.
    check("a row with no created_at and no expires_at is treated as stale",
          approvals.is_expired(A(), now) is True)


def test_housekeeping_marks_stale_proposals():
    db = fresh_db()
    print()
    print("=" * 74)
    print("4. STALE PROPOSALS ARE MARKED, NOT LEFT IN THE QUEUE")
    print("=" * 74)
    propose(db, created_at=datetime.utcnow() - timedelta(days=400))
    propose(db, created_at=datetime.utcnow() - timedelta(days=400))
    fresh_aid, _ = propose(db)
    # A stale one belonging to ANOTHER tenant, and a stale one that was already
    # decided. Housekeeping must touch neither: the first is not ours to sweep,
    # and the second would have its audit trail rewritten from "Approved" to
    # "Expired" — a decision a human made, erased by a background job.
    other_aid, _ = propose(db, "FACTORY_B",
                           created_at=datetime.utcnow() - timedelta(days=400))
    decided_aid, _ = propose(db, created_at=datetime.utcnow() - timedelta(days=400))
    tok = tenancy.set_current_tenant(None)
    db.query(models.AgentAction).filter(
        models.AgentAction.id == decided_aid).first().status = "Approved"
    db.commit()

    n = approvals.expire_stale(db, "FACTORY_A")
    db.commit()
    tenancy.reset_current_tenant(tok)
    check("both stale proposals were marked Expired", n == 2, str(n))
    check("the fresh one was left alone",
          action_status(db, fresh_aid) == "Proposed", action_status(db, fresh_aid))
    check("another tenant's stale proposal was NOT swept",
          action_status(db, other_aid) == "Proposed", action_status(db, other_aid))
    check("an already-decided action keeps its decision",
          action_status(db, decided_aid) == "Approved", action_status(db, decided_aid))

    # CONTROL: the gate does not DEPEND on this having run — it evaluates
    # freshness at decision time. A guard that needs a background job to have
    # run is off whenever the job is.
    db2 = fresh_db()
    admin = seed_user(db2, "FACTORY_A", "a-admin", "Admin")
    aid, pid = propose(db2, created_at=datetime.utcnow() - timedelta(days=400))
    check("CONTROL: it is still Proposed (housekeeping has not run)",
          action_status(db2, aid) == "Proposed", action_status(db2, aid))
    was, code = refused(approve, db2, "FACTORY_A", aid, admin)
    check("...and the gate refuses it anyway", was and code == 409, str(code))


def test_the_route_still_declares_its_role_guard():
    """require_roles is a FastAPI dependency, so calling the handler directly
    never evaluates it. Asserted statically — a direct-call 'success' would
    prove nothing about what happens over HTTP."""
    print()
    print("=" * 74)
    print("5. THE ROUTE'S ROLE GUARD IS STILL DECLARED")
    print("=" * 74)
    for name in ("approve_agent_action", "reject_agent_action"):
        src = inspect.getsource(getattr(agent_routes, name))
        check(f"{name} declares require_roles([\"Admin\", \"Supervisor\"])",
              'require_roles(["Admin", "Supervisor"])' in src, src[:100])

    # `_check_actor` refuses a falsy is_active rather than only an explicit
    # False, so a NULL would lock the account out rather than admit it. That
    # branch is unreachable ONLY because the column cannot hold NULL — which
    # makes the column definition the real guard. Pin it here: if someone makes
    # is_active nullable, this fails, and the fail-closed reading becomes
    # load-bearing rather than theoretical.
    col = models.User.__table__.c.is_active
    check("users.is_active is NOT NULL", col.nullable is False, str(col.nullable))
    check("...with a server default, so rows predating the column read TRUE",
          col.server_default is not None, str(col.server_default))


if __name__ == "__main__":
    test_the_gate_refuses_every_bypass()
    test_the_gate_still_lets_real_work_through()
    test_expiry_semantics()
    test_housekeeping_marks_stale_proposals()
    test_the_route_still_declares_its_role_guard()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE BACKEND IS AUTHORITATIVE ABOUT APPROVALS")
