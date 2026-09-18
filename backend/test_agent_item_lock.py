"""An agent proposal holds its item until a human decides it (ADR-0015 addendum).

WHAT WAS MEASURED ON MASTER (794d483)
-------------------------------------
Agents create an item in a pending state (MaintenanceTask 'Proposed',
PurchaseOrder 'Draft', Escalation 'Proposed') and pair it with an AgentAction
'Proposed'. The approval gate guarded the ACTION, and nothing guarded the ITEM:

  * PATCH /maintenance/tasks/{id}, /escalations/{id} and /purchase-orders/{id}
    (Operator allowed) moved the item out of its pending state, or booked stock
    against a Draft PO nobody approved;
  * DELETE on the same paths removed the item under a live proposal;
  * approving or rejecting afterwards recorded 'Approved' / 'Rejected' while the
    item never moved -- an audit record contradicting what the system did.

WHAT THIS SUITE PINS
--------------------
  1. every bypass path, through the real handlers, is refused with 409 and
     leaves item, action, stock and ledger untouched;
  2. a decision on an item that already moved or vanished records nothing false:
     the gate refuses, and the route withdraws the proposal (compare-and-set);
  3. controls: look-alikes, decided items and moved legacy rows stay editable;
  4. the licence clause: a tenant whose plan cannot reach the decision API keeps
     today's behaviour (nothing is held), a missing config row fails closed;
  5. an expired proposal still holds its item; approve is refused, reject is
     the recorded exit;
  6. lists carry awaiting_approval from the same predicate, and a list with
     nothing pending costs zero agent_actions queries (a successful PATCH
     leaves its row unheld, so its response carries null by construction);
  7. the double-decide race, sequentially: a stale second decision -- even one
     whose session already holds the item -- cannot overwrite the first;
  8. withdraw_orphaned (a tested function, deliberately NOT wired to boot);
  9. auto-approval still works with no tenant bound (items are tenant-stamped);
 10. the invariant: an item is held exactly when the gate would let it be
     decided (for a licensed tenant, ignoring expiry);
 11. the same race with the two decisions truly overlapping, which only
     PostgreSQL can hold (SELECT ... FOR UPDATE). On SQLite it prints SKIP;
     verify_pg_approvals.py runs it on a local scratch PostgreSQL;
 12. nobody moves an item INTO its pending status by hand (PATCH or POST, 400),
     so an orphaned proposal cannot be re-armed over rewritten content;
 13. every proposal is reachable from the Approvals list (it pages), and each
     says whether it has expired;
 14. a plan change cannot re-arm a proposal a human changed: on a plan that
     cannot reach Approvals, a PATCH or DELETE withdraws the proposal in its
     own transaction, recorded against the person who made it;
 15. a proposal never holds a newer row that reuses its item's id (SQLite
     reuses deleted ids): it names only a row created no later than itself;
 16. every withdrawal records who caused it (an AuditLog row, same commit).

Run: DATABASE_URL="sqlite:///./ci.db" python test_agent_item_lock.py
     DATABASE_URL=<local scratch postgresql> python test_agent_item_lock.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agent_routes
import ai.agents
import approvals
import factory_ops_routes
import models
import orders_routes
import platform_routes
import schemas
import tenancy
from database import Base
from events import InventoryLow

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

A, B = "FACTORY_A", "FACTORY_B"
ADMIN = {"sub": "a-admin", "role": "Admin", "tenant": A}
SUP = {"sub": "a-sup", "role": "Supervisor", "tenant": A}
OPER = {"sub": "a-op", "role": "Operator", "tenant": A}
B_ADMIN = {"sub": "b-admin", "role": "Admin", "tenant": B}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def banner(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class unbound:
    """Run a block with no tenant bound (seeding and inspection)."""

    def __enter__(self):
        self.tok = tenancy.set_current_tenant(None)

    def __exit__(self, *exc):
        tenancy.reset_current_tenant(self.tok)


class bound:
    def __init__(self, tenant):
        self.tenant = tenant

    def __enter__(self):
        self.tok = tenancy.set_current_tenant(self.tenant)

    def __exit__(self, *exc):
        tenancy.reset_current_tenant(self.tok)


def fresh_db(bind=None):
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    eng = bind or engine
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    _open.append(db)
    tenancy.install_scoping()
    with unbound():
        for actor in (ADMIN, SUP, OPER, B_ADMIN):
            db.add(models.User(username=actor["sub"], password="x", role=actor["role"],
                               tenant_code=actor["tenant"], is_active=True))
        for t in (A, B):
            db.add(models.Machine(tenant_code=t, name=f"CNC-{t}", status="Running"))
        db.commit()
    return db


def _machine(db, tenant):
    with unbound():
        return db.query(models.Machine).filter(models.Machine.tenant_code == tenant).first().id


def seed_item(db, kind, tenant=A, status=None, stock_item=False, created_at=None, item_id=None):
    """A real item of `kind` in its pending status (or `status`). ``created_at``
    backdates it (an agent writes its item and its proposal at the same moment);
    ``item_id`` forces its id, as SQLite does when it reuses a deleted row's id."""
    _seq[0] += 1
    n = _seq[0]
    with unbound():
        if kind == "maintenance_task":
            item = models.MaintenanceTask(
                tenant_code=tenant, task_no=f"AUTO-MAINT-{n}", machine_id=_machine(db, tenant),
                task_type="Predictive (auto)", priority="Critical",
                assigned_to="Maintenance team", planned_date=datetime.utcnow().date(),
                status=status or "Proposed", downtime_minutes=0)
        elif kind == "purchase_order":
            inv_id = None
            if stock_item:
                inv = models.InventoryItem(tenant_code=tenant, item_code=f"RM-{n}",
                                           item_name="Steel", category="Raw", unit="kg",
                                           current_stock=3, reorder_level=10)
                db.add(inv)
                db.flush()
                inv_id = inv.id
            item = models.PurchaseOrder(
                tenant_code=tenant, po_no=f"AUTO-PO-{n}", supplier_id=None, item_id=inv_id,
                item_name="Steel", order_quantity=10, received_quantity=0, unit="kg",
                expected_delivery_date=datetime.utcnow().date(), status=status or "Draft")
        else:
            item = models.Escalation(
                tenant_code=tenant, machine_id=_machine(db, tenant), title=f"Repeated downtime {n}",
                severity="High", owner="Maintenance Lead", department="Maintenance",
                status=status or "Proposed", source="Escalation agent")
        if created_at is not None:
            item.created_at = created_at
        if item_id is not None:
            item.id = item_id
        db.add(item)
        db.commit()
        return item.id


def seed_action(db, kind, ref_id, tenant=A, status="Proposed", created_at=None,
                expires_at=None, agent=None):
    with unbound():
        a = models.AgentAction(
            tenant_code=tenant, agent=agent or {"maintenance_task": "maintenance",
                                                "purchase_order": "reorder",
                                                "escalation": "escalation"}.get(kind, "maintenance"),
            action_type="x", summary="proposal", ref_kind=kind, ref_id=ref_id,
            status=status, created_at=created_at or datetime.utcnow(), expires_at=expires_at)
        db.add(a)
        db.commit()
        return a.id


def propose(db, kind, tenant=A, **kw):
    """An agent's proposal: its item first, then the action, at one moment (a
    backdated proposal backdates its item too, as ai.agents writes them)."""
    iid = seed_item(db, kind, tenant, stock_item=kw.pop("stock_item", False),
                    created_at=kw.get("created_at"))
    return seed_action(db, kind, iid, tenant, **kw), iid


def set_licence(db, tenant, modules):
    with unbound():
        row = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == tenant).first()
        if modules is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(models.TenantConfig(tenant_code=tenant, enabled_modules=modules))
        else:
            row.enabled_modules = modules
        db.commit()


MODEL = {"maintenance_task": models.MaintenanceTask, "purchase_order": models.PurchaseOrder,
         "escalation": models.Escalation}


def item_row(db, kind, iid):
    db.expire_all()
    with unbound():
        return db.query(MODEL[kind]).filter(MODEL[kind].id == iid).first()


def action_row(db, aid):
    db.expire_all()
    with unbound():
        return db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()


def call(tenant, fn, *a, **kw):
    """(ok, status_code, result)."""
    with bound(tenant):
        try:
            return True, None, fn(*a, **kw)
        except HTTPException as e:
            return False, e.status_code, e.detail


def patch(db, kind, iid, body, actor=OPER, tenant=A):
    db.expire_all()
    if kind == "maintenance_task":
        return call(tenant, factory_ops_routes.update_maintenance_task, iid,
                    schemas.MaintenanceTaskUpdate(**body), db=db, current_user=actor)
    if kind == "purchase_order":
        return call(tenant, orders_routes.update_purchase_order, iid,
                    schemas.PurchaseOrderUpdate(**body), db=db, current_user=actor)
    return call(tenant, factory_ops_routes.update_escalation, iid,
                schemas.EscalationUpdate(**body), db=db, current_user=actor)


def delete(db, kind, iid, actor=ADMIN, tenant=A):
    db.expire_all()
    fn = {"maintenance_task": factory_ops_routes.delete_maintenance_task,
          "purchase_order": orders_routes.delete_purchase_order,
          "escalation": factory_ops_routes.delete_escalation}[kind]
    return call(tenant, fn, iid, db=db, current_user=actor)


def listing(db, kind, tenant=A, actor=ADMIN):
    db.expire_all()
    fn = {"maintenance_task": factory_ops_routes.get_maintenance_tasks,
          "purchase_order": orders_routes.get_purchase_orders,
          "escalation": factory_ops_routes.get_escalations}[kind]
    resp = {"maintenance_task": schemas.MaintenanceTaskResponse,
            "purchase_order": schemas.PurchaseOrderResponse,
            "escalation": schemas.EscalationResponse}[kind]
    ok, code, rows = call(tenant, fn, db=db, current_user=actor)
    assert ok, (code, rows)
    # Through the response model, as FastAPI serialises it -- a direct handler
    # call alone would skip response_model and hide a serialisation 500.
    return {r.id: resp.model_validate(r).model_dump() for r in rows}


def decide(db, aid, decision, actor=ADMIN, tenant=A):
    db.expire_all()
    fn = agent_routes.approve_agent_action if decision == "approve" else agent_routes.reject_agent_action
    return call(tenant, fn, aid, db=db, current_user=actor)


PENDING = {"maintenance_task": "Proposed", "purchase_order": "Draft", "escalation": "Proposed"}
MOVE = {"maintenance_task": {"status": "Open"}, "purchase_order": {"status": "Open"},
        "escalation": {"status": "Resolved"}}
OTHER_FIELD = {"maintenance_task": {"downtime_minutes": 30},
               "purchase_order": {"notes": "changed"},
               "escalation": {"owner": "Someone else"}}
APPROVED = {"maintenance_task": "Open", "purchase_order": "Approved", "escalation": "Open"}
KINDS = ("maintenance_task", "purchase_order", "escalation")


# =====================================================================
def test_every_bypass_is_refused():
    banner("1. EVERY PATH THAT USED TO MOVE A HELD ITEM IS REFUSED (409)")
    for kind in KINDS:
        db = fresh_db()
        aid, iid = propose(db, kind)
        for label, body in (("status", MOVE[kind]), ("another field", OTHER_FIELD[kind]),
                            ("an empty body", {}),
                            ("a no-op re-sending the pending status", {"status": PENDING[kind]})):
            ok, code, _ = patch(db, kind, iid, body)
            check(f"{kind}: PATCH {label} is refused with 409", not ok and code == 409,
                  f"ok={ok} code={code}")
        for actor in (OPER, SUP, ADMIN):
            ok, code, _ = patch(db, kind, iid, MOVE[kind], actor=actor)
            check(f"{kind}: ...for {actor['role']} too", not ok and code == 409, str(code))
        ok, code, _ = delete(db, kind, iid)
        check(f"{kind}: Admin DELETE is refused with 409", not ok and code == 409, str(code))
        item, act = item_row(db, kind, iid), action_row(db, aid)
        check(f"{kind}: the item still exists, still {PENDING[kind]}",
              item is not None and item.status == PENDING[kind],
              None if item is None else item.status)
        check(f"{kind}: the action is still Proposed and undecided",
              act.status == "Proposed" and act.decided_by is None and act.decided_at is None,
              f"{act.status}/{act.decided_by}")
        if kind == "maintenance_task":
            check("maintenance_task: downtime and completed_date untouched",
                  item.downtime_minutes == 0 and item.completed_date is None,
                  f"{item.downtime_minutes}/{item.completed_date}")
        if kind == "escalation":
            check("escalation: resolved_at still NULL, owner unchanged",
                  item.resolved_at is None and item.owner == "Maintenance Lead",
                  f"{item.resolved_at}/{item.owner}")
        # The exit still works: Supervisor approves, and the item moves with it.
        ok, code, out = decide(db, aid, "approve", actor=SUP)
        check(f"{kind}: a Supervisor then approves it", ok and out["status"] == "Approved",
              f"{code} {out}")
        check(f"{kind}: ...and the item moved with the decision",
              item_row(db, kind, iid).status == APPROVED[kind], item_row(db, kind, iid).status)

    # The receipt path: stock and ledger must not move for a held Draft PO.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order", stock_item=True)
    ok, code, _ = patch(db, "purchase_order", iid, {"received_quantity": 5})
    po = item_row(db, "purchase_order", iid)
    with unbound():
        inv = db.query(models.InventoryItem).filter(models.InventoryItem.id == po.item_id).first()
        ledger = db.query(models.InventoryTransaction).filter(
            models.InventoryTransaction.reference == po.po_no).count()
    check("purchase_order: PATCH received_quantity on a held Draft is refused (409)",
          not ok and code == 409, str(code))
    check("...received stays 0, status stays Draft, stock stays 3, no ledger row",
          po.received_quantity == 0 and po.status == "Draft" and inv.current_stock == 3
          and ledger == 0,
          f"recv={po.received_quantity} status={po.status} stock={inv.current_stock} ledger={ledger}")

    # The refusal names the action and who can decide it.
    ok, code, detail = patch(db, "purchase_order", iid, {"status": "Open"})
    check("the 409 detail names the agent action and the approvers",
          f"#{aid}" in str(detail) and "Admin or Supervisor" in str(detail), str(detail))


def test_a_decision_never_contradicts_the_item():
    banner("2. A DECISION ON AN ITEM THAT DID NOT MOVE RECORDS NOTHING FALSE")
    for kind in KINDS:
        for decision in ("approve", "reject"):
            # Legacy state: the item was moved by hand before this fix existed.
            db = fresh_db()
            aid, iid = propose(db, kind)
            with unbound():
                item = db.query(MODEL[kind]).filter(MODEL[kind].id == iid).first()
                item.status = "In Progress"
                db.commit()
            ok, code, detail = decide(db, aid, decision)
            act, item = action_row(db, aid), item_row(db, kind, iid)
            check(f"{kind}: {decision} on a MOVED item is refused with 409",
                  not ok and code == 409, f"{ok} {code} {detail}")
            check(f"{kind}: ...no {decision} is recorded; the proposal is withdrawn",
                  act.status == "Cancelled" and act.decided_by == approvals.WITHDRAWN_BY
                  and act.decided_at is not None, f"{act.status}/{act.decided_by}")
            check(f"{kind}: ...and the item is exactly as the human left it",
                  item.status == "In Progress", item.status)
            ok2, code2, detail2 = decide(db, aid, decision)
            check(f"{kind}: deciding it again is a clean 400 'Already cancelled'",
                  not ok2 and code2 == 400 and detail2 == "Already cancelled",
                  f"{code2} {detail2}")

        # Deleted item.
        db = fresh_db()
        aid, iid = propose(db, kind)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == iid).delete(synchronize_session=False)
            db.commit()
        ok, code, _ = decide(db, aid, "approve")
        act = action_row(db, aid)
        check(f"{kind}: approve on a DELETED item is 409 and withdraws, not 'Approved'",
              not ok and code == 409 and act.status == "Cancelled", f"{code} {act.status}")

    # No item recorded at all.
    db = fresh_db()
    aid = seed_action(db, "purchase_order", None)
    ok, code, _ = decide(db, aid, "reject")
    check("an action with no item (ref_id NULL) is 409 and withdrawn",
          not ok and code == 409 and action_row(db, aid).status == "Cancelled",
          f"{code} {action_row(db, aid).status}")

    # The item belongs to another tenant (a producer that forgot to stamp it).
    db = fresh_db()
    iid = seed_item(db, "purchase_order", tenant=B)
    aid = seed_action(db, "purchase_order", iid, tenant=A)
    ok, code, _ = decide(db, aid, "approve")
    check("an action whose item sits in ANOTHER tenant is 409 and withdrawn",
          not ok and code == 409 and action_row(db, aid).status == "Cancelled", str(code))
    check("...and the other tenant's item was not touched",
          item_row(db, "purchase_order", iid).status == "Draft",
          item_row(db, "purchase_order", iid).status)

    # The gate itself (reached past the route) refuses and writes nothing.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order")
    with unbound():
        db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == iid).first().status = "Open"
        db.commit()
        action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
        try:
            ai.agents.apply_decision(db, action, "approve", decided_by="a-admin", actor=ADMIN)
            check("apply_decision() direct on a moved item is refused", False, "ACCEPTED")
        except approvals.ProposalWithdrawn as e:
            check("apply_decision() direct on a moved item raises ProposalWithdrawn (409)",
                  e.status_code == 409, str(e.status_code))
        db.rollback()
    act = action_row(db, aid)
    check("...and the gate wrote nothing (still Proposed, undecided)",
          act.status == "Proposed" and act.decided_by is None, f"{act.status}/{act.decided_by}")
    # Auto-approval on an orphan is refused too.
    with unbound():
        action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
        try:
            ai.agents.apply_decision(db, action, "approve", decided_by="auto-policy",
                                     require_actor=False)
            check("auto-approval of an orphan is refused", False, "ACCEPTED")
        except approvals.ProposalWithdrawn as e:
            check("auto-approval of an orphan is refused (409)", e.status_code == 409,
                  str(e.status_code))
        db.rollback()

    # Check order is kept: outsiders and non-approvers learn nothing about the item.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order")
    with unbound():
        db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == iid).first().status = "Open"
        db.add(models.User(username="a-off", password="x", role="Admin", tenant_code=A,
                           is_active=False))
        db.commit()
    ok, code, _ = decide(db, aid, "approve", actor=B_ADMIN, tenant=B)
    check("a FACTORY_B Admin deciding A's orphan gets 404, not 409", code == 404, str(code))
    ok, code, _ = decide(db, aid, "approve",
                         actor={"sub": "a-off", "role": "Admin", "tenant": A})
    check("a disabled Admin deciding an orphan gets 403, not 409", code == 403, str(code))
    ok, code, _ = decide(db, aid, "approve", actor={"sub": "a-op", "role": "Operator", "tenant": A})
    check("an Operator deciding an orphan gets 403, not 409", code == 403, str(code))
    check("...and none of them withdrew it", action_row(db, aid).status == "Proposed",
          action_row(db, aid).status)


def test_controls_stay_editable():
    banner("3. CONTROLS: WHAT IS NOT HELD STAYS EDITABLE")
    for kind in KINDS:
        db = fresh_db()
        # A human look-alike: pending status, no AgentAction at all.
        iid = seed_item(db, kind)
        ok, code, _ = patch(db, kind, iid, MOVE[kind])
        check(f"{kind}: a look-alike (no action) can be PATCHed", ok, str(code))
        iid2 = seed_item(db, kind)
        ok, code, _ = delete(db, kind, iid2)
        check(f"{kind}: ...and deleted", ok and item_row(db, kind, iid2) is None, str(code))

        # A look-alike whose only action is already decided.
        iid3 = seed_item(db, kind)
        seed_action(db, kind, iid3, status="Approved")
        seed_action(db, kind, iid3, status="Rejected")
        seed_action(db, kind, iid3, status="Expired")
        ok, code, _ = patch(db, kind, iid3, MOVE[kind])
        check(f"{kind}: pending status with only DECIDED actions is editable", ok, str(code))

        # A legacy row moved by hand while its action stayed Proposed.
        aid4, iid4 = propose(db, kind)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == iid4).first().status = "In Progress"
            db.commit()
        ok, code, _ = patch(db, kind, iid4, OTHER_FIELD[kind])
        check(f"{kind}: a moved legacy row with a Proposed action stays editable", ok, str(code))
        flags = listing(db, kind)
        check(f"{kind}: ...and its list flag is null", flags[iid4]["awaiting_approval"] is None,
              str(flags[iid4]["awaiting_approval"]))

        # After a decision the item is ordinary again.
        aid5, iid5 = propose(db, kind)
        decide(db, aid5, "approve")
        ok, code, _ = patch(db, kind, iid5, OTHER_FIELD[kind])
        check(f"{kind}: approved, then editable", ok, str(code))
        aid6, iid6 = propose(db, kind)
        decide(db, aid6, "reject")
        ok, code, _ = delete(db, kind, iid6)
        check(f"{kind}: rejected (Cancelled), then deletable", ok, str(code))

    # A proposal of ANOTHER kind that happens to carry the same id holds nothing.
    db = fresh_db()
    task_id = seed_item(db, "maintenance_task")
    seed_action(db, "purchase_order", task_id)   # a reorder action pointing at "id N"
    ok, code, _ = patch(db, "maintenance_task", task_id, {"status": "Open"})
    check("an action of another kind with the same ref_id does not hold the item", ok, str(code))

    # A proposal holds only its OWN tenant's item, even in a mixed batch.
    db = fresh_db()
    a_item = seed_item(db, "purchase_order", tenant=A)
    b_item = seed_item(db, "purchase_order", tenant=B)
    seed_action(db, "purchase_order", b_item, tenant=A)   # A's action naming B's row id
    with unbound():
        rows = db.query(models.PurchaseOrder).filter(
            models.PurchaseOrder.id.in_([a_item, b_item])).all()
        held = approvals.awaiting_decision(db, models.PurchaseOrder, rows)
    check("in a mixed-tenant batch, A's action does not hold B's item", held == {}, str(held))

    # A look-alike Draft PO receipt still books stock (the old behaviour).
    db = fresh_db()
    iid = seed_item(db, "purchase_order", stock_item=True)
    ok, code, _ = patch(db, "purchase_order", iid, {"received_quantity": 5})
    po = item_row(db, "purchase_order", iid)
    with unbound():
        stock = db.query(models.InventoryItem).filter(
            models.InventoryItem.id == po.item_id).first().current_stock
    check("a look-alike Draft PO receipt still books stock", ok and stock == 8, f"{code} {stock}")

    # Cross-tenant: 404 first, so pending state does not leak.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order")
    ok, code, _ = patch(db, "purchase_order", iid, {"status": "Open"}, actor=B_ADMIN, tenant=B)
    check("FACTORY_B PATCHing A's held PO gets 404, not 409", code == 404, str(code))
    ok, code, _ = delete(db, "purchase_order", iid, actor=B_ADMIN, tenant=B)
    check("FACTORY_B DELETEing A's held PO gets 404, not 409", code == 404, str(code))


def test_licence_clause():
    banner("4. THE LOCK HOLDS ONLY WHERE THE PLAN CAN REACH THE DECISION API")
    growth = "core,operations,factory"
    full = "core,operations,factory,intelligence,admin"
    for kind in KINDS:
        db = fresh_db()
        set_licence(db, A, growth)
        aid, iid = propose(db, kind)
        flags = listing(db, kind)
        check(f"{kind}: growth plan (no Intelligence pack): list flag is null",
              flags[iid]["awaiting_approval"] is None, str(flags[iid]["awaiting_approval"]))
        ok, code, _ = patch(db, kind, iid, MOVE[kind])
        check(f"{kind}: growth plan: PATCH succeeds (today's behaviour kept)", ok, str(code))

        db = fresh_db()
        set_licence(db, A, full)
        aid, iid = propose(db, kind)
        ok, code, _ = patch(db, kind, iid, MOVE[kind])
        check(f"{kind}: plan WITH the Intelligence pack: held (409)", code == 409, str(code))

        db = fresh_db()
        set_licence(db, A, None)
        aid, iid = propose(db, kind)
        ok, code, _ = patch(db, kind, iid, MOVE[kind])
        check(f"{kind}: NO TenantConfig row: held (fails closed, 409)", code == 409, str(code))

    # The licence is read per tenant: B's plan does not unlock A's items.
    db = fresh_db()
    set_licence(db, A, full)
    set_licence(db, B, growth)
    aid, iid = propose(db, "purchase_order")
    ok, code, _ = patch(db, "purchase_order", iid, {"status": "Open"})
    check("another tenant's growth plan does not release this tenant's lock", code == 409, str(code))
    check("the decision-API path resolves to a gated pack in the manifest",
          approvals.decision_api_pack() is not None, str(approvals.decision_api_pack()))


def test_expired_proposals():
    banner("5. AN EXPIRED PROPOSAL STILL HOLDS; REJECT IS THE RECORDED EXIT")
    for kind in KINDS:
        db = fresh_db()
        aid, iid = propose(db, kind, created_at=datetime.utcnow() - timedelta(days=400))
        ok, code, _ = patch(db, kind, iid, MOVE[kind])
        check(f"{kind}: a 400-day-old proposal still holds its item (409)", code == 409, str(code))
        flags = listing(db, kind)
        flag = flags[iid]["awaiting_approval"]
        check(f"{kind}: ...and its flag says expired", flag is not None and flag["expired"] is True,
              str(flag))
        ok, code, detail = patch(db, kind, iid, MOVE[kind])
        # "reject" alone could not tell the two messages apart: the generic held
        # message says "approve or reject" too, so deleting the expired branch left
        # this green. The expired message says it has expired and offers no approve.
        text = str(detail).lower()
        check(f"{kind}: ...the 409 says it has expired and must be rejected",
              "has expired" in text and "reject" in text and "approve" not in text, str(detail))
        ok, code, detail = decide(db, aid, "approve")
        check(f"{kind}: approving it is refused as expired (409)", code == 409, str(code))
        # The agent cannot re-propose while the item is held (every dedup counts
        # the pending status as open), so "ask the agent to re-evaluate" would
        # send the approver nowhere. The answer names the one exit: reject.
        check(f"{kind}: ...and the refusal says it can only be rejected, which releases the item",
              "only be rejected" in str(detail) and "releases" in str(detail)
              and "re-evaluate" not in str(detail), str(detail))
        check(f"{kind}: ...and nothing was written", action_row(db, aid).status == "Proposed",
              action_row(db, aid).status)
        ok, code, out = decide(db, aid, "reject")
        check(f"{kind}: rejecting it succeeds", ok and out["status"] == "Rejected", f"{code} {out}")
        check(f"{kind}: ...cancelling the item, recorded by the approver",
              item_row(db, kind, iid).status == "Cancelled"
              and action_row(db, aid).decided_by == "a-admin",
              f"{item_row(db, kind, iid).status}/{action_row(db, aid).decided_by}")
        ok, code, _ = patch(db, kind, iid, OTHER_FIELD[kind])
        check(f"{kind}: ...after which the item is editable", ok, str(code))

    # Auto-approval of an expired proposal stays refused (freshness still gates approve).
    db = fresh_db()
    aid, iid = propose(db, "purchase_order", created_at=datetime.utcnow() - timedelta(days=400))
    with unbound():
        action = db.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
        try:
            ai.agents.apply_decision(db, action, "approve", decided_by="auto-policy",
                                     require_actor=False)
            check("auto-approval of an expired proposal is refused", False, "ACCEPTED")
        except HTTPException as e:
            check("auto-approval of an expired proposal is refused (409)", e.status_code == 409,
                  str(e.status_code))
        db.rollback()


def test_flags_on_lists_and_responses():
    banner("6. awaiting_approval ON LISTS AND PATCH RESPONSES, ONE PREDICATE")
    for kind in KINDS:
        db = fresh_db()
        aid, held = propose(db, kind)
        lookalike = seed_item(db, kind)
        opened = seed_item(db, kind, status="Open" if kind != "purchase_order" else "Received")
        flags = listing(db, kind)
        flag = flags[held]["awaiting_approval"]
        check(f"{kind}: the held row carries {{agent_action_id, agent, expired}}",
              flag == {"agent_action_id": aid, "agent": action_row(db, aid).agent,
                       "expired": False}, str(flag))
        check(f"{kind}: the look-alike and the ordinary row carry null",
              flags[lookalike]["awaiting_approval"] is None
              and flags[opened]["awaiting_approval"] is None,
              f"{flags[lookalike]['awaiting_approval']} {flags[opened]['awaiting_approval']}")
        resp = {"maintenance_task": schemas.MaintenanceTaskResponse,
                "purchase_order": schemas.PurchaseOrderResponse,
                "escalation": schemas.EscalationResponse}[kind]
        ok, code, out = patch(db, kind, lookalike, OTHER_FIELD[kind])
        # Null by construction, not by annotation: a PATCH succeeds only on a row
        # that is not held, and none may be moved INTO its pending status by hand.
        check(f"{kind}: a PATCH response validates against the response model (flag null)",
              ok and resp.model_validate(out).awaiting_approval is None, str(code))

        # A PATCH may not bring a moved item BACK into its pending status: that
        # would revive the Proposed action behind it, over content a non-approver
        # may have rewritten meanwhile (section 12).
        aid2, moved = propose(db, kind)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == moved).first().status = "Cancelled"
            db.commit()
        ok, code, out = patch(db, kind, moved, {"status": PENDING[kind]})
        check(f"{kind}: a PATCH back into the pending status is refused (400); no hold is revived",
              not ok and code == 400 and item_row(db, kind, moved).status == "Cancelled"
              and listing(db, kind)[moved]["awaiting_approval"] is None,
              f"{ok} {code} {out} {item_row(db, kind, moved).status}")

    # Zero agent_actions queries when nothing is in a pending status.
    db = fresh_db()
    for kind in KINDS:
        seed_item(db, kind, status="Open" if kind != "purchase_order" else "Received")
    seen = []

    def _count(conn, cursor, statement, params, context, executemany):
        if "agent_actions" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        for kind in KINDS:
            listing(db, kind)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    check("lists with nothing pending issue ZERO agent_actions queries", not seen, str(len(seen)))

    # A list with many held rows answers in one agent_actions query per list.
    db = fresh_db()
    for _ in range(5):
        propose(db, "maintenance_task")
    seen.clear()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        flags = listing(db, "maintenance_task")
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    check("five held rows are flagged with ONE batched agent_actions query",
          len(seen) == 1 and sum(1 for f in flags.values() if f["awaiting_approval"]) == 5,
          f"queries={len(seen)}")


def test_double_decision_race():
    banner("7. A STALE SECOND DECISION CANNOT OVERWRITE THE FIRST")
    if _URL.startswith("postgresql"):
        eng = engine
        tmp = None
    else:
        tmp = tempfile.mkdtemp()
        eng = create_engine(f"sqlite:///{os.path.join(tmp, 'race.db')}",
                            connect_args={"check_same_thread": False})
    db1 = fresh_db(eng)
    aid, iid = propose(db1, "purchase_order")
    # expire_on_commit=False keeps the loaded row stale across the commit below,
    # which is exactly what a second, slower request holds in memory.
    db2 = sessionmaker(bind=eng, expire_on_commit=False)()
    # db2 has the action loaded as Proposed (a double-click's second request).
    with bound(A):
        stale = db2.query(models.AgentAction).filter(models.AgentAction.id == aid).first()
        check("setup: the second request sees the action as Proposed", stale.status == "Proposed")
    db2.commit()  # end its read transaction; the identity map keeps the stale object
    ok, code, out = decide(db1, aid, "approve")
    check("the first request approves", ok and out["status"] == "Approved", f"{code} {out}")
    with bound(A):
        try:
            agent_routes.reject_agent_action(aid, db=db2, current_user=ADMIN)
            check("the stale reject is refused", False, "ACCEPTED")
        except HTTPException as e:
            check("the stale reject is refused with 400 'Already approved'",
                  e.status_code == 400 and e.detail == "Already approved",
                  f"{e.status_code} {e.detail}")
    db2.close()
    act = action_row(db1, aid)
    check("the record still says Approved by the first approver, matching the PO",
          act.status == "Approved" and act.decided_by == "a-admin"
          and item_row(db1, "purchase_order", iid).status == "Approved",
          f"{act.status}/{act.decided_by}/{item_row(db1, 'purchase_order', iid).status}")

    # The slower request holds the ITEM too (a caller that loaded the PO before
    # deciding). The lock must re-read the row rather than trust the copy in that
    # session's identity map, or it approves a Draft that is already decided.
    aid_s, iid_s = propose(db1, "purchase_order")
    db3 = sessionmaker(bind=eng, expire_on_commit=False)()
    with bound(A):
        stale_action = db3.query(models.AgentAction).filter(models.AgentAction.id == aid_s).first()
        stale_po = db3.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == iid_s).first()
        check("setup: the slower request holds the action Proposed AND the PO Draft",
              stale_action.status == "Proposed" and stale_po.status == "Draft",
              f"{stale_action.status}/{stale_po.status}")
    db3.commit()  # end the read; both stale objects stay in its identity map
    ok, code, out = decide(db1, aid_s, "approve")
    check("the first request approves it", ok and out["status"] == "Approved", f"{code} {out}")
    with bound(A):
        try:
            agent_routes.reject_agent_action(aid_s, db=db3, current_user=SUP)
            check("the stale reject (item held in its session) is refused", False, "ACCEPTED")
        except HTTPException as e:
            check("the stale reject (item held in its session) is refused with 400 'Already approved'",
                  e.status_code == 400 and e.detail == "Already approved",
                  f"{e.status_code} {e.detail}")
    db3.close()
    act = action_row(db1, aid_s)
    check("...and the record is still the first approver's, matching the PO",
          act.status == "Approved" and act.decided_by == "a-admin"
          and item_row(db1, "purchase_order", iid_s).status == "Approved",
          f"{act.status}/{act.decided_by}/{item_row(db1, 'purchase_order', iid_s).status}")

    # withdraw() itself is a compare-and-set.
    aid2, _ = propose(db1, "purchase_order")
    decide(db1, aid2, "reject")
    with unbound():
        n = approvals.withdraw(db1, aid2, A)
        db1.commit()
    act = action_row(db1, aid2)
    check("withdraw() on a decided action changes 0 rows and leaves the decision",
          n == 0 and act.status == "Rejected" and act.decided_by == "a-admin",
          f"n={n} {act.status}/{act.decided_by}")
    aid3 = seed_action(db1, "purchase_order", None)
    with unbound():
        n_other = approvals.withdraw(db1, aid3, B)
        n = approvals.withdraw(db1, aid3, A)
        db1.commit()
    check("withdraw() is tenant-scoped (0 from another tenant) and withdraws once",
          n_other == 0 and n == 1 and action_row(db1, aid3).status == "Cancelled",
          f"{n_other} {n}")
    for s in list(_open):
        s.close()
    _open.clear()
    if tmp:
        eng.dispose()


def test_withdraw_orphaned():
    banner("8. withdraw_orphaned: A TESTED FUNCTION, NOT WIRED TO BOOT")
    db = fresh_db()
    live, _ = propose(db, "purchase_order")
    expired_live, _ = propose(db, "maintenance_task", created_at=datetime.utcnow() - timedelta(days=400))
    unlicensed_tenant_live, _ = propose(db, "escalation", tenant=B)
    set_licence(db, B, "core,operations,factory")
    moved, moved_item = propose(db, "maintenance_task")
    with unbound():
        db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == moved_item).first().status = "Open"
        db.commit()
    deleted, deleted_item = propose(db, "escalation")
    with unbound():
        db.query(models.Escalation).filter(models.Escalation.id == deleted_item).delete(
            synchronize_session=False)
        db.commit()
    no_ref = seed_action(db, "purchase_order", None)
    unknown_kind = seed_action(db, "work_order", 1)
    decided_iid = seed_item(db, "purchase_order", status="Open")
    decided = seed_action(db, "purchase_order", decided_iid, status="Approved")
    other_tenant_orphan = seed_action(db, "purchase_order", None, tenant=B)

    with unbound():
        n_a = approvals.withdraw_orphaned(db, tenant=A)
        db.commit()
    check("scoped to FACTORY_A it withdraws exactly the 4 orphans", n_a == 4, str(n_a))
    for label, aid in (("moved", moved), ("deleted", deleted), ("no ref", no_ref),
                       ("unknown kind", unknown_kind)):
        act = action_row(db, aid)
        check(f"...{label}: Cancelled by {approvals.WITHDRAWN_BY}",
              act.status == "Cancelled" and act.decided_by == approvals.WITHDRAWN_BY,
              f"{act.status}/{act.decided_by}")
    for label, aid, status in (("a live proposal", live, "Proposed"),
                               ("an expired but still-held proposal", expired_live, "Proposed"),
                               ("a decided action", decided, "Approved"),
                               ("another tenant's orphan", other_tenant_orphan, "Proposed"),
                               ("an unlicensed tenant's live proposal", unlicensed_tenant_live,
                                "Proposed")):
        check(f"...{label} is untouched", action_row(db, aid).status == status,
              action_row(db, aid).status)
    with unbound():
        n_again = approvals.withdraw_orphaned(db, tenant=A)
        n_all = approvals.withdraw_orphaned(db)
        db.commit()
    check("idempotent: a second run withdraws 0", n_again == 0, str(n_again))
    check("with no tenant it sweeps every tenant (B's orphan only)",
          n_all == 1 and action_row(db, other_tenant_orphan).status == "Cancelled", str(n_all))
    check("...and still never touches a live proposal",
          action_row(db, unlicensed_tenant_live).status == "Proposed",
          action_row(db, unlicensed_tenant_live).status)

    # O1: nothing calls it at boot or from a script.
    here = os.path.dirname(os.path.abspath(__file__))
    callers = []
    for root, dirs, files in os.walk(here):
        dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "__pycache__", "node_modules")]
        for f in files:
            if f.endswith(".py") and not f.startswith(("test_", "mutate_")) and f != "approvals.py":
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    if "withdraw_orphaned" in fh.read():
                        callers.append(f)
    check("no boot hook or script calls withdraw_orphaned (founder go-ahead needed)",
          not callers, str(callers))


def test_auto_approval_with_no_tenant_bound():
    banner("9. AUTO-APPROVAL STILL WORKS WITH NO TENANT BOUND")
    db = fresh_db()
    with unbound():
        inv = models.InventoryItem(tenant_code=A, item_code="RM-X", item_name="Steel",
                                   category="Raw", unit="kg", current_stock=3, reorder_level=10)
        db.add(inv)
        db.commit()
        ai.agents.draft_reorder_on_inventory_low(
            InventoryLow(tenant_code=A, item_id=inv.id, item_code="RM-X", item_name="Steel",
                         current_stock=3, reorder_level=10), db)
        db.commit()
        po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.item_id == inv.id).first()
        act = db.query(models.AgentAction).filter(models.AgentAction.ref_kind == "purchase_order").first()
    check("the reorder draft is stamped with the EVENT's tenant, not the column default",
          po is not None and po.tenant_code == A, None if po is None else po.tenant_code)
    check("...and auto-approves (PO Approved, action Approved by auto-policy)",
          po is not None and po.status == "Approved" and act.status == "Approved"
          and act.decided_by == "auto-policy",
          f"{None if po is None else po.status}/{act.status if act else None}")

    with unbound():
        ai.agents._propose_task(db, A, "maintenance", task_no="AUTO-MAINT-T-1",
                                machine_id=_machine(db, A), task_type="Predictive (auto)",
                                priority="Critical", summary="s", notes="n", severity="Critical")
        db.commit()
        task = db.query(models.MaintenanceTask).filter(
            models.MaintenanceTask.task_no == "AUTO-MAINT-T-1").first()
    check("an agent task is stamped with the proposing tenant", task.tenant_code == A,
          task.tenant_code)


def test_held_exactly_when_decidable():
    banner("10. INVARIANT: HELD EXACTLY WHEN THE GATE WOULD MOVE THE ITEM")
    cases = 0
    bad = []
    for licence in ("full", "growth", "missing"):
        # Every combination below seeds its own item and action, so one database
        # per licence state is enough; the licence is the only shared input.
        db = fresh_db()
        set_licence(db, A, {"full": "core,operations,factory,intelligence,admin",
                            "growth": "core,operations,factory",
                            "missing": None}[licence])
        for kind in KINDS:
            for item_status in (PENDING[kind], "Open", "Cancelled"):
                for action_status in ("Proposed", "Approved", "Cancelled"):
                    for item_tenant in (A, B):
                        iid = seed_item(db, kind, tenant=item_tenant, status=item_status)
                        aid = seed_action(db, kind, iid, tenant=A, status=action_status)
                        with unbound():
                            item = db.query(MODEL[kind]).filter(MODEL[kind].id == iid).first()
                            action = db.query(models.AgentAction).filter(
                                models.AgentAction.id == aid).first()
                            held = iid in approvals.awaiting_decision(db, MODEL[kind], [item])
                            gate_moves = True
                            try:
                                approvals.authorise(db, action, ADMIN, "approve")
                            except HTTPException:
                                gate_moves = False
                            db.rollback()
                        expected_decidable = (action_status == "Proposed" and item_status == PENDING[kind]
                                              and item_tenant == A)
                        licensed = licence != "growth"
                        cases += 1
                        if gate_moves != expected_decidable:
                            bad.append(("gate", kind, item_status, action_status, item_tenant, licence))
                        if held != (expected_decidable and licensed):
                            bad.append(("held", kind, item_status, action_status, item_tenant, licence))
    check(f"all {cases} combinations agree (held == decidable and licensed)", not bad, str(bad[:5]))


RACE_HOLD_SECONDS = 1.5


def test_simultaneous_decisions_on_postgresql():
    banner("11. TWO DECISIONS AT THE SAME INSTANT: THE ITEM ROW LOCK SERIALISES THEM")
    # Section 7 is a SEQUENTIAL race: the second request starts after the first
    # committed. This one overlaps them for real. Both requests pass the route's
    # status check and the gate's state and actor checks before either reads the
    # item, so the only thing left between them is SELECT ... FOR UPDATE on the
    # item row. Measured with that lock removed, on PostgreSQL 18: 45 of 45 races
    # answered 200 to BOTH approve and reject, and whichever committed last
    # silently overwrote the other's decision.
    if not _URL.startswith("postgresql"):
        print("  SKIP  needs PostgreSQL: SQLite has no row locks, so it cannot hold this race "
              "(verify_pg_approvals.py runs this section on PostgreSQL)")
        return
    import threading

    real_check_item = approvals._check_item
    gates = {}

    def gated_check_item(db, action):
        gate = gates.get(action.id)
        if gate is None:
            return real_check_item(db, action)
        # Barrier 1: both requests are inside the item check before either reads.
        try:
            gate["enter"].wait(timeout=20)
        except threading.BrokenBarrierError:
            gate["enter_broken"] = True
        real_check_item(db, action)
        # Barrier 2: wait for the OTHER request to have read the item as well.
        # With the row lock it cannot until this transaction ends, so the wait
        # times out and this request commits first. Without the lock both read
        # the row as pending, both get past here, and both decide.
        try:
            gate["read"].wait(timeout=RACE_HOLD_SECONDS)
            gate["both_read"] = True
        except threading.BrokenBarrierError:
            pass

    def request(results, key, aid, decision, actor, preload):
        tok = tenancy.set_current_tenant(A)
        session = SessionLocal()
        try:
            # A caller that already has the item on screen (loaded in its session).
            held = [session.query(MODEL[k]).filter(MODEL[k].id == i).first()
                    for k, i in preload]
            fn = (agent_routes.approve_agent_action if decision == "approve"
                  else agent_routes.reject_agent_action)
            results[key] = (200, fn(aid, db=session, current_user=actor)["status"])
            del held
        except HTTPException as e:
            results[key] = (e.status_code, e.detail)
        except Exception as e:  # a deadlock or a stale-data error is a failure too
            results[key] = ("error", f"{type(e).__name__}: {e}")
            session.rollback()
        finally:
            session.close()
            tenancy.reset_current_tenant(tok)

    races = [(kind, ("approve", SUP), ("reject", ADMIN), False) for kind in KINDS]
    # A double click on Approve from a screen that already holds the PO.
    races.append(("purchase_order", ("approve", SUP), ("approve", ADMIN), True))
    approvals._check_item = gated_check_item
    try:
        for kind, first, second, preload in races:
            db = fresh_db()
            aid, iid = propose(db, kind)
            db.close()
            label = f"{kind}: {first[0]} ({first[1]['role']}) vs {second[0]} ({second[1]['role']})" \
                    + (", item already loaded" if preload else "")
            gate = gates[aid] = {"enter": threading.Barrier(2), "read": threading.Barrier(2)}
            results = {}
            spec = {"first": first, "second": second}
            threads = [threading.Thread(target=request,
                                        args=(results, key, aid, decision, actor,
                                              [(kind, iid)] if preload else []))
                       for key, (decision, actor) in spec.items()]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            check(f"{label}: both requests reached the item check together",
                  not gate.get("enter_broken") and len(results) == 2, str(results))
            winners = [k for k, r in results.items() if r[0] == 200]
            losers = [k for k, r in results.items() if r[0] != 200]
            check(f"{label}: exactly one decision is accepted",
                  len(winners) == 1 and len(losers) == 1, str(results))
            check(f"{label}: the second request could not read the item until the first committed",
                  not gate.get("both_read"), "both requests read the row as pending")
            act, item = action_row(db, aid), item_row(db, kind, iid)
            if len(winners) == 1 and len(losers) == 1:
                decision, actor = spec[winners[0]]
                recorded = "Approved" if decision == "approve" else "Rejected"
                check(f"{label}: the refused one gets 400 'Already {recorded.lower()}'",
                      results[losers[0]] == (400, f"Already {recorded.lower()}"),
                      str(results[losers[0]]))
                check(f"{label}: the record is the winner's and matches the item",
                      act.status == recorded and act.decided_by == actor["sub"]
                      and item.status == (APPROVED[kind] if decision == "approve" else "Cancelled"),
                      f"{act.status}/{act.decided_by}/{item.status}")
    finally:
        approvals._check_item = real_check_item
        for s in list(_open):
            s.close()
        _open.clear()


def test_no_hand_move_into_the_pending_status():
    banner("12. NOBODY PUTS AN ITEM INTO ITS PENDING STATUS BY HAND")
    # Measured through main.app before this rule: a legacy orphan (item moved by
    # hand while its action stayed Proposed -- lazy withdrawal leaves these in
    # place) was re-armed by an Operator. PATCH the content (200, not held),
    # PATCH {status: <pending>} (200, now held), and a Supervisor's approval then
    # recorded 'Approved' under the agent's name for content the agent never
    # proposed. The pending status is systemOnly in status-vocab.json: only an
    # agent's proposal writes it. The server now says so too.
    later = (datetime.utcnow() + timedelta(days=30)).date()
    content = {"maintenance_task": {"priority": "Low", "assigned_to": "nobody", "planned_date": later},
               "purchase_order": {"expected_delivery_date": later, "notes": "rewritten"},
               "escalation": {"owner": "nobody", "department": "Nowhere",
                              "resolution_notes": "rewritten"}}
    moved_to = {"maintenance_task": "Open", "purchase_order": "Approved", "escalation": "Open"}
    for kind in KINDS:
        db = fresh_db()
        aid, iid = propose(db, kind)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == iid).first().status = moved_to[kind]
            db.commit()
        ok, code, _ = patch(db, kind, iid, content[kind])
        check(f"{kind}: an Operator may still edit the orphaned item (it is not held)", ok, str(code))
        for actor in (OPER, SUP, ADMIN):
            ok, code, detail = patch(db, kind, iid, {"status": PENDING[kind]}, actor=actor)
            check(f"{kind}: ...but {actor['role']} cannot move it back into '{PENDING[kind]}' (400)",
                  not ok and code == 400 and "agent" in str(detail), f"{ok} {code} {detail}")
        check(f"{kind}: ...the item keeps its status and is not held",
              item_row(db, kind, iid).status == moved_to[kind]
              and listing(db, kind)[iid]["awaiting_approval"] is None,
              item_row(db, kind, iid).status)
        ok, code, _ = decide(db, aid, "approve", actor=SUP)
        act = action_row(db, aid)
        check(f"{kind}: ...so approving the orphan records nothing (409, withdrawn)",
              not ok and code == 409 and act.status == "Cancelled"
              and act.decided_by == approvals.WITHDRAWN_BY, f"{code} {act.status}/{act.decided_by}")

        # Controls: a row ALREADY in its pending status may re-send it (no change).
        lookalike = seed_item(db, kind)
        ok, code, _ = patch(db, kind, lookalike, {"status": PENDING[kind], **OTHER_FIELD[kind]})
        check(f"{kind}: a look-alike already '{PENDING[kind]}' may re-send its own status", ok, str(code))
        ordinary = seed_item(db, kind, status="Open")
        ok, code, _ = patch(db, kind, ordinary, {"status": PENDING[kind]})
        check(f"{kind}: an ordinary row with no proposal cannot be moved into it either (400)",
              not ok and code == 400, str(code))
        ok, code, _ = patch(db, kind, ordinary, {"status": "Cancelled"})
        check(f"{kind}: ...while any other status change still works", ok, str(code))

    # The rule is the vocabulary's, not the lock's: it holds on every plan.
    db = fresh_db()
    set_licence(db, A, "core,operations,factory")
    iid = seed_item(db, "purchase_order", status="Open")
    ok, code, _ = patch(db, "purchase_order", iid, {"status": "Draft"})
    check("a tenant without the Intelligence pack cannot move a PO into Draft by hand either (400)",
          not ok and code == 400, str(code))

    # Creating straight into the pending status is refused too.
    db = fresh_db()
    with unbound():
        supplier = models.Supplier(tenant_code=A, supplier_code="SUP-H", supplier_name="Steel Co")
        db.add(supplier)
        db.commit()
        supplier_id = supplier.id
    today = datetime.utcnow().date()
    creates = {
        "maintenance_task": (factory_ops_routes.create_maintenance_task, schemas.MaintenanceTaskCreate,
                             {"task_no": "MT-HAND", "machine_id": _machine(db, A),
                              "task_type": "Preventive", "assigned_to": "Fitter",
                              "planned_date": today}),
        "purchase_order": (orders_routes.create_purchase_order, schemas.PurchaseOrderCreate,
                           {"po_no": "PO-HAND", "supplier_id": supplier_id, "item_name": "Steel",
                            "order_quantity": 5, "unit": "kg", "expected_delivery_date": today}),
        "escalation": (factory_ops_routes.create_escalation, schemas.EscalationCreate,
                       {"title": "By hand", "severity": "High", "owner": "o", "department": "d"}),
    }
    for kind, (fn, schema, body) in creates.items():
        ok, code, detail = call(A, fn, schema(**body, status=PENDING[kind]), db=db, current_user=ADMIN)
        check(f"{kind}: POST with status '{PENDING[kind]}' is refused (400)",
              not ok and code == 400, f"{ok} {code} {detail}")
        db.rollback()
        ok, code, out = call(A, fn, schema(**body), db=db, current_user=ADMIN)
        check(f"{kind}: ...the same POST with its default status is created",
              ok and out.status != PENDING[kind], f"{code} {out}")


def test_every_proposal_is_reachable_from_approvals():
    banner("13. EVERY PROPOSAL CAN BE REACHED FROM THE APPROVALS LIST")
    # Measured before paging: one held Draft PO whose action was 2 days old, plus
    # 300 newer Proposed actions (orphans, which lazy withdrawal leaves in place).
    # GET /agent-actions?status=Proposed answered 300 rows without the held one,
    # while PATCH on the PO answered 409 "... in Approvals". Now that the list is
    # the only way out for a held item, every row of it must be reachable.
    db = fresh_db()
    now = datetime.utcnow()
    held_aid, held_iid = propose(db, "purchase_order", created_at=now - timedelta(days=2))
    with unbound():
        same_moment = now - timedelta(hours=1)
        db.add_all([models.AgentAction(tenant_code=A, agent="reorder", action_type="x",
                                       summary="orphan", ref_kind="purchase_order", ref_id=None,
                                       status="Proposed", created_at=same_moment)
                    for _ in range(300)])
        db.commit()

    def page(**kw):
        db.expire_all()
        ok, code, rows = call(A, agent_routes.list_agent_actions, status="Proposed", db=db,
                              current_user=ADMIN, **kw)
        assert ok, (code, rows)
        return rows

    first = page()
    check("setup: the default page is full (300) and lacks the held proposal",
          len(first) == 300 and held_aid not in {r["id"] for r in first}, str(len(first)))
    check("setup: ...while the PO list flags it as held by that proposal",
          (listing(db, "purchase_order")[held_iid]["awaiting_approval"] or {}).get("agent_action_id")
          == held_aid)
    second = page(offset=300)
    check("the next page (offset=300) carries the held proposal",
          [r["id"] for r in second] == [held_aid], str([r["id"] for r in second]))
    ids = [r["id"] for r in first + second]
    check("the pages neither repeat nor drop a row, though 300 share one timestamp",
          len(ids) == 301 and len(set(ids)) == 301, f"{len(ids)} rows, {len(set(ids))} distinct")
    check("...because a shared timestamp is ordered by id, newest first (a stable order to page)",
          ids[:300] == sorted(ids[:300], reverse=True), str(ids[:5]))
    check("a smaller page walks the same order",
          [r["id"] for r in page(limit=100, offset=200)] == ids[200:300])
    check("the page size is capped at 300", len(page(limit=5000)) == 300)
    check("a negative offset and a zero limit are clamped, not an error",
          [r["id"] for r in page(offset=-5, limit=0)] == ids[:1])

    # Each action says whether it can still be approved.
    db = fresh_db()
    fresh, _ = propose(db, "purchase_order")
    stale, _ = propose(db, "maintenance_task", created_at=now - timedelta(days=400))
    decided = seed_action(db, "escalation", None, status="Approved",
                          created_at=now - timedelta(days=400))
    rows = {r["id"]: r for r in call(A, agent_routes.list_agent_actions, db=db,
                                     current_user=ADMIN)[2]}
    check("a fresh proposal carries expired: false", rows[fresh].get("expired") is False,
          str(rows[fresh].get("expired")))
    check("an expired proposal carries expired: true (it can only be rejected)",
          rows[stale].get("expired") is True, str(rows[stale].get("expired")))
    check("a decided action is not 'expired' (the flag is about what can still be decided)",
          rows[decided].get("expired") is False, str(rows[decided].get("expired")))


FOUNDER = {"sub": "founder", "role": "Admin", "tenant": "DEFAULT"}
EARLIER = timedelta(hours=1)


def apply_plan(db, tenant, plan):
    """The founder's real apply-plan handler (sets enabled_modules from the manifest)."""
    db.expire_all()
    ok, code, out = call("DEFAULT", platform_routes.apply_plan, tenant, {"plan": plan},
                         db=db, current_user=FOUNDER)
    assert ok, (code, out)


def withdrawals(db, aid=None):
    """Audit rows recording a withdrawal (of agent action `aid`, or of any)."""
    db.expire_all()
    with unbound():
        q = db.query(models.AuditLog).filter(
            models.AuditLog.action == "withdraw_agent_action",
            models.AuditLog.entity_type == "agent_action")
        if aid is not None:
            q = q.filter(models.AuditLog.entity_id == aid)
        return q.order_by(models.AuditLog.id).all()


REWRITE = {"maintenance_task": {"priority": "Low", "assigned_to": "nobody",
                                "planned_date": (datetime.utcnow() + timedelta(days=30)).date()},
           "purchase_order": {"expected_delivery_date": (datetime.utcnow() + timedelta(days=30)).date(),
                              "notes": "rewritten"},
           "escalation": {"owner": "nobody", "department": "Nowhere",
                          "resolution_notes": "rewritten"}}


def rewritten(kind, item):
    return {"maintenance_task": lambda: item.priority == "Low" and item.assigned_to == "nobody",
            "purchase_order": lambda: item.notes == "rewritten",
            "escalation": lambda: item.owner == "nobody"}[kind]()


def test_a_plan_change_cannot_rearm_an_edited_proposal():
    banner("14. A PLAN CHANGE CANNOT RE-ARM A PROPOSAL A HUMAN HAS CHANGED")
    # Measured through main.app (verifier round 2). Tenant on the growth plan:
    # nothing is held, because nobody there can reach Approvals. The maintenance
    # agent proposed a Critical task; an Operator PATCHed it to priority Low,
    # assigned 'nobody', planned 2030, re-sending status Proposed (200). The
    # founder applied the enterprise plan (the task was now held), and a
    # Supervisor's approval answered 200: 'Approved' was recorded for "Open a
    # Critical maintenance task" over content the agent never proposed. The same
    # edits were open on escalations and Draft POs. A change or delete of an item
    # that is unheld ONLY because of the licence clause now withdraws the
    # proposal in the same transaction, recorded against whoever made it.
    for kind in KINDS:
        db = fresh_db()
        apply_plan(db, A, "growth")
        aid, iid = propose(db, kind)
        listing(db, kind)
        check(f"{kind}: growth plan: reading the list withdraws nothing",
              action_row(db, aid).status == "Proposed", action_row(db, aid).status)
        ok, code, _ = patch(db, kind, iid, {"status": PENDING[kind], **REWRITE[kind]})
        check(f"{kind}: growth plan: an Operator's PATCH keeping '{PENDING[kind]}' still succeeds "
              "(today's behaviour)", ok, str(code))
        act = action_row(db, aid)
        check(f"{kind}: ...and the proposal it rewrote is withdrawn by that same write",
              act.status == "Cancelled" and act.decided_by == approvals.WITHDRAWN_BY
              and act.decided_at is not None, f"{act.status}/{act.decided_by}")
        rows = withdrawals(db, aid)
        check(f"{kind}: ...recorded against the Operator who made the change, in this tenant",
              len(rows) == 1 and rows[0].actor == OPER["sub"] and rows[0].tenant_code == A,
              str([(r.actor, r.tenant_code) for r in rows]))
        apply_plan(db, A, "enterprise")
        check(f"{kind}: after the upgrade to enterprise the rewritten item is NOT held",
              listing(db, kind)[iid]["awaiting_approval"] is None,
              str(listing(db, kind)[iid]["awaiting_approval"]))
        ok, code, detail = decide(db, aid, "approve", actor=SUP)
        act, item = action_row(db, aid), item_row(db, kind, iid)
        check(f"{kind}: ...so a Supervisor's approval records nothing (400 'Already cancelled')",
              not ok and code == 400 and act.status == "Cancelled", f"{ok} {code} {detail} {act.status}")
        check(f"{kind}: ...and the item keeps the human's content, unmoved",
              item.status == PENDING[kind] and rewritten(kind, item), item.status)
        ok, code, _ = patch(db, kind, iid, OTHER_FIELD[kind])
        check(f"{kind}: ...and stays an ordinary, editable row", ok, str(code))

    # A DELETE on the growth plan withdraws too (the proposal can no longer act).
    db = fresh_db()
    apply_plan(db, A, "growth")
    for kind in KINDS:
        aid, iid = propose(db, kind)
        ok, code, _ = delete(db, kind, iid)
        check(f"{kind}: growth plan: an Admin's DELETE succeeds and withdraws the proposal, "
              "recorded against the Admin",
              ok and action_row(db, aid).status == "Cancelled"
              and [r.actor for r in withdrawals(db, aid)] == [ADMIN["sub"]],
              f"{code} {action_row(db, aid).status} {[r.actor for r in withdrawals(db, aid)]}")

    # The withdrawal belongs to the edit's own transaction: a PATCH refused after
    # the lock (here the receipt check) leaves the proposal live.
    db = fresh_db()
    apply_plan(db, A, "growth")
    aid, iid = propose(db, "purchase_order")
    ok, code, detail = patch(db, "purchase_order", iid, {"received_quantity": 11})
    db.rollback()   # what the request's session teardown does after the 400
    check("growth plan: a PATCH refused later in the handler (400) withdraws nothing",
          not ok and code == 400 and action_row(db, aid).status == "Proposed"
          and not withdrawals(db, aid), f"{ok} {code} {detail} {action_row(db, aid).status}")

    # Controls on the growth plan: nothing to withdraw, nothing written.
    db = fresh_db()
    apply_plan(db, A, "growth")
    for kind in KINDS:
        lookalike = seed_item(db, kind)
        ok, code, _ = patch(db, kind, lookalike, OTHER_FIELD[kind])
        check(f"{kind}: growth plan: a look-alike (no proposal) is edited with no withdrawal",
              ok and not withdrawals(db), str(code))
        decided_iid = seed_item(db, kind)
        decided = seed_action(db, kind, decided_iid, status="Rejected")
        ok, code, _ = patch(db, kind, decided_iid, OTHER_FIELD[kind])
        check(f"{kind}: growth plan: a row whose proposal is already decided keeps that decision",
              ok and action_row(db, decided).status == "Rejected" and not withdrawals(db),
              f"{code} {action_row(db, decided).status}")
        aid, iid = propose(db, kind)
        ok, code, _ = patch(db, kind, iid, MOVE[kind], actor=B_ADMIN, tenant=B)
        check(f"{kind}: another tenant's PATCH is 404 and withdraws nothing",
              code == 404 and action_row(db, aid).status == "Proposed" and not withdrawals(db, aid),
              f"{code} {action_row(db, aid).status}")

    # A proposal nobody touched is still re-armed by the upgrade and decided
    # normally: only human edits end a proposal.
    db = fresh_db()
    apply_plan(db, A, "growth")
    aid, iid = propose(db, "maintenance_task")
    apply_plan(db, A, "enterprise")
    ok, code, _ = patch(db, "maintenance_task", iid, {"status": "Open"})
    check("an untouched growth-plan proposal is held once the plan reaches Approvals (409)",
          code == 409 and not withdrawals(db, aid), str(code))
    ok, code, out = decide(db, aid, "approve", actor=SUP)
    check("...and is approved normally, moving its item",
          ok and out["status"] == "Approved" and item_row(db, "maintenance_task", iid).status == "Open",
          f"{code} {out}")

    # On a plan that reaches Approvals the lock refuses and withdraws nothing.
    db = fresh_db()
    apply_plan(db, A, "enterprise")
    aid, iid = propose(db, "escalation")
    ok, code, _ = patch(db, "escalation", iid, REWRITE["escalation"])
    check("enterprise plan: the held item's PATCH is 409 and leaves the proposal live, unaudited",
          code == 409 and action_row(db, aid).status == "Proposed" and not withdrawals(db, aid),
          f"{code} {action_row(db, aid).status}")


def test_a_proposal_never_holds_a_newer_row_with_its_id():
    banner("15. A PROPOSAL NEVER HOLDS A NEWER ROW THAT REUSES ITS ITEM'S ID")
    # Measured through main.app on SQLite (verifier round 2). A Draft PO (id 1)
    # and its proposal #2 "Draft a PO for Steel (90 kg)"; purchase orders
    # bulk-deleted, as reseed_inventory.py does (agent_actions untouched); a new
    # Copper Draft PO took id 1 again with proposal #3. GET /purchase-orders showed
    # the Copper PO held by #2, and approving #2 answered 200: #2 recorded
    # Approved and the Copper PO moved, while #3 was left orphaned. SQLite
    # reuses the highest deleted rowid; PostgreSQL sequences do not, so here the
    # id is forced to make the case engine-independent. An agent writes its item
    # before its proposal, so a proposal names only a row created no later than
    # itself.
    earlier = datetime.utcnow() - EARLIER
    for kind in KINDS:
        db = fresh_db()
        old_aid, old_iid = propose(db, kind, created_at=earlier)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == old_iid).delete(synchronize_session=False)
            db.commit()
        new_iid = seed_item(db, kind, item_id=old_iid)
        new_aid = seed_action(db, kind, new_iid)
        check(f"{kind}: setup: the new row carries the deleted row's id", new_iid == old_iid,
              f"{new_iid} vs {old_iid}")
        flag = listing(db, kind)[new_iid]["awaiting_approval"]
        check(f"{kind}: the new row is flagged as held by ITS OWN proposal, not the stale one",
              flag is not None and flag["agent_action_id"] == new_aid, f"{flag} (own #{new_aid})")
        ok, code, detail = patch(db, kind, new_iid, MOVE[kind])
        check(f"{kind}: ...and its 409 names its own proposal",
              code == 409 and f"#{new_aid}" in str(detail), f"{code} {detail}")
        ok, code, detail = decide(db, old_aid, "approve")
        check(f"{kind}: approving the STALE proposal is refused (409) and withdraws it",
              not ok and code == 409 and action_row(db, old_aid).status == "Cancelled",
              f"{ok} {code} {detail} {action_row(db, old_aid).status}")
        check(f"{kind}: ...the new row did not move and its own proposal is still live",
              item_row(db, kind, new_iid).status == PENDING[kind]
              and action_row(db, new_aid).status == "Proposed",
              f"{item_row(db, kind, new_iid).status}/{action_row(db, new_aid).status}")
        ok, code, out = decide(db, new_aid, "approve")
        check(f"{kind}: ...and its own proposal still decides it",
              ok and out["status"] == "Approved" and item_row(db, kind, new_iid).status == APPROVED[kind],
              f"{code} {out}")

    # The orphan sweep reads the same rule.
    db = fresh_db()
    old_aid, old_iid = propose(db, "purchase_order", created_at=earlier)
    with unbound():
        db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == old_iid).delete(
            synchronize_session=False)
        db.commit()
    new_aid = seed_action(db, "purchase_order", seed_item(db, "purchase_order", item_id=old_iid))
    with unbound():
        n = approvals.withdraw_orphaned(db, tenant=A)
        db.commit()
    check("withdraw_orphaned withdraws the stale proposal and leaves the new one live",
          n == 1 and action_row(db, old_aid).status == "Cancelled"
          and action_row(db, new_aid).status == "Proposed", str(n))

    # The agent's own write: item and proposal at the same instant still match.
    db = fresh_db()
    moment = datetime.utcnow() - timedelta(minutes=5)
    iid = seed_item(db, "maintenance_task", created_at=moment)
    aid = seed_action(db, "maintenance_task", iid, created_at=moment)
    flag = listing(db, "maintenance_task")[iid]["awaiting_approval"]
    check("an item created at the same instant as its proposal is held by it",
          flag is not None and flag["agent_action_id"] == aid, str(flag))

    # An item whose creation time is unknown cannot be shown to be the proposal's.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order")
    with unbound():
        db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == iid).update(
            {models.PurchaseOrder.created_at: None}, synchronize_session=False)
        db.commit()
    check("an item with no created_at is not held",
          listing(db, "purchase_order")[iid]["awaiting_approval"] is None)
    ok, code, detail = decide(db, aid, "approve")
    check("...and the refusal states the true cause, not an id reuse that never happened",
          "no creation time" in str(detail) and "created after the proposal" not in str(detail),
          str(detail))
    audit = withdrawals(db, aid)
    check("...and so does the withdrawal's audit row",
          len(audit) == 1 and "no creation time" in (audit[0].details or "")
          and "created after the proposal" not in (audit[0].details or ""),
          str([a.details for a in audit]))
    check("...and no decision is recorded against it (409, withdrawn)",
          code == 409 and action_row(db, aid).status == "Cancelled"
          and item_row(db, "purchase_order", iid).status == "Draft",
          f"{code} {action_row(db, aid).status}")


def test_a_withdrawal_names_who_caused_it():
    banner("16. EVERY WITHDRAWAL RECORDS WHO CAUSED IT")
    # Verifier round 2: when an approver's decision found the item moved, the
    # route withdrew the proposal with decided_by='system-withdrawn' and wrote
    # nothing else; only the request log line named the user. The durable record
    # could not say who. The withdrawal now stages an AuditLog row in its own
    # transaction, only when the compare-and-set changed the row.
    for kind in KINDS:
        db = fresh_db()
        aid, iid = propose(db, kind)
        with unbound():
            db.query(MODEL[kind]).filter(MODEL[kind].id == iid).first().status = "In Progress"
            db.commit()
        ok, code, detail = decide(db, aid, "reject", actor=SUP)
        rows = withdrawals(db, aid)
        check(f"{kind}: the Supervisor whose decision found the item moved is on the record",
              code == 409 and len(rows) == 1 and rows[0].actor == SUP["sub"]
              and rows[0].tenant_code == A and "Nothing was decided" in (rows[0].details or ""),
              f"{code} {[(r.actor, r.tenant_code, r.details) for r in rows]}")
        check(f"{kind}: ...and the action itself still reads as withdrawn, not as their decision",
              action_row(db, aid).status == "Cancelled"
              and action_row(db, aid).decided_by == approvals.WITHDRAWN_BY,
              f"{action_row(db, aid).status}/{action_row(db, aid).decided_by}")

    # Refusals before the item check cause no withdrawal and so no row.
    db = fresh_db()
    aid, iid = propose(db, "purchase_order")
    with unbound():
        db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == iid).first().status = "Open"
        db.commit()
    decide(db, aid, "approve", actor=OPER)
    decide(db, aid, "approve", actor=B_ADMIN, tenant=B)
    check("a refused Operator and another tenant's Admin write no withdrawal row",
          action_row(db, aid).status == "Proposed" and not withdrawals(db), str(len(withdrawals(db))))

    # The compare-and-set's loser writes no row: one withdrawal, one record.
    with unbound():
        first = approvals.withdraw(db, aid, A, by=SUP["sub"], reason="first")
        db.commit()
        second = approvals.withdraw(db, aid, A, by=ADMIN["sub"], reason="second")
        db.commit()
    rows = withdrawals(db, aid)
    check("a second withdraw() changes nothing and records nothing",
          first == 1 and second == 0 and [r.actor for r in rows] == [SUP["sub"]],
          f"{first} {second} {[r.actor for r in rows]}")

    # The sweep (not wired to boot) records itself as the system.
    db = fresh_db()
    orphan = seed_action(db, "purchase_order", None)
    with unbound():
        approvals.withdraw_orphaned(db, tenant=A)
        db.commit()
    rows = withdrawals(db, orphan)
    check("withdraw_orphaned records 'system' against each proposal it withdraws, in its tenant",
          [(r.actor, r.tenant_code) for r in rows] == [("system", A)],
          str([(r.actor, r.tenant_code) for r in rows]))


def test_a_withdrawal_is_committed_not_just_staged():
    banner("17. A WITHDRAWAL SURVIVES A ROLLBACK OF THE SESSION THAT MADE IT")
    # Review finding: every withdrawal assertion above reads back through the
    # handler's own session, which sees its own UNCOMMITTED update and the
    # autoflushed audit row. Deleting the route's db.commit() after
    # approvals.withdraw left every suite green. Rolling the session back after
    # the 409 discards anything uncommitted, so only a durable withdrawal and a
    # durable audit row survive this check. (A second session is no help here:
    # the test engine shares one SQLite connection, uncommitted writes included.)
    for decision in ("approve", "reject"):
        db = fresh_db()
        aid, iid = propose(db, "maintenance_task")
        with unbound():
            db.query(MODEL["maintenance_task"]).filter(
                MODEL["maintenance_task"].id == iid).first().status = "In Progress"
            db.commit()
        ok, code, detail = decide(db, aid, decision)
        check(f"{decision} on a moved item is refused with 409", not ok and code == 409, f"{code} {detail}")
        db.rollback()
        act = action_row(db, aid)
        check(f"...after a rollback the withdrawal is still there ({decision})",
              act.status == "Cancelled" and act.decided_by == approvals.WITHDRAWN_BY,
              f"{act.status}/{act.decided_by}")
        check(f"...and so is its audit row ({decision})", len(withdrawals(db, aid)) == 1,
              str(withdrawals(db, aid)))


if __name__ == "__main__":
    test_every_bypass_is_refused()
    test_a_decision_never_contradicts_the_item()
    test_controls_stay_editable()
    test_licence_clause()
    test_expired_proposals()
    test_flags_on_lists_and_responses()
    test_double_decision_race()
    test_withdraw_orphaned()
    test_auto_approval_with_no_tenant_bound()
    test_held_exactly_when_decidable()
    test_simultaneous_decisions_on_postgresql()
    test_no_hand_move_into_the_pending_status()
    test_every_proposal_is_reachable_from_approvals()
    test_a_plan_change_cannot_rearm_an_edited_proposal()
    test_a_proposal_never_holds_a_newer_row_with_its_id()
    test_a_withdrawal_names_who_caused_it()
    test_a_withdrawal_is_committed_not_just_staged()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("AN AGENT PROPOSAL HOLDS ITS ITEM UNTIL IT IS DECIDED")
