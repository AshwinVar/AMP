"""Material issued "for WO-100" is in WO-100's trace.

THE DEFECT
----------
An issue slip carries a free-text job reference (`work_order_ref`): typed on the
Enterprise Inventory screen, stored, echoed in the list, and read by nothing.
The issue wrote its stock transaction with `reference=slip_no`, while the
work-order trace (ai/trace.build_work_order_trace) links material to a job
through `InventoryTransaction.reference == work_order_no` -- its own comment
says "the manual routes stamp the work-order number into the transaction
reference". The enterprise slip did not. So an operator who typed the job
number and issued the steel saw it in no job's genealogy, and the sibling field
on the same module (`purchase_order_ref`, resolved against `po_no` for supplier
performance) showed the pattern was known.

THE RULE THIS FILE PINS
-----------------------
  1. A slip whose reference names a work order in this tenant: the issue's
     transaction references that work order, the note names the slip, and the
     job's trace lists the item as consumed.
  2. A reference to a job AMP does not know: the transaction keeps the slip
     number, the note says the job is not in AMP, and no trace changes. The
     match is exact after trimming: "WO-10" does not resolve to "WO-100".
  3. The list says which references resolved (`work_order_no`,
     `work_order_resolved`), in one query for the page, and the screen shows
     an unresolved reference as such.
  4. Another tenant's work order with the same number is not this tenant's: a
     slip in TB naming "WO-100" resolves to TB's job, and TA's trace does not
     see TB's material.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_issue_slip_counts_against_its_job.py
"""
import os
import sys

from fastapi import Response
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import enterprise_inventory_routes as eir
import models
import tenancy
from ai import trace
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)(), engine


ADMIN = {"role": "Admin", "sub": "ta-admin", "tenant": "TA"}


def seed(db):
    tok = tenancy.set_current_tenant(None)
    for tenant, machine_name in (("TA", "PRESS-01"), ("TB", "LATHE-02")):
        m = models.Machine(tenant_code=tenant, site="P1", name=machine_name, status="Running", utilization=70)
        db.add(m)
        db.flush()
        db.add(models.WorkOrder(tenant_code=tenant, work_order_no="WO-100", part_number="P-1", batch_number="B-1",
                                machine_id=m.id, target_quantity=100, actual_quantity=0, status="In Progress"))
        db.add(models.WorkOrder(tenant_code=tenant, work_order_no="WO-10", part_number="P-2", batch_number="B-2",
                                machine_id=m.id, target_quantity=10, actual_quantity=0, status="Planned"))
        db.add(models.InventoryItem(tenant_code=tenant, item_code=f"STEEL-{tenant}", item_name="Steel sheet",
                                    category="Raw", unit="pcs", current_stock=500, reorder_level=10))
    db.commit()
    tenancy.reset_current_tenant(tok)


def slip(db, tenant, ref, qty=20):
    """A slip created the way the screen creates one, approved and issued the way the routes do it."""
    tok = tenancy.set_current_tenant(tenant)
    try:
        item = db.query(models.InventoryItem).first()
        user = dict(ADMIN, tenant=tenant)
        created = eir.create_issue_slip({"item_id": item.id, "requested_qty": qty, "work_order_ref": ref,
                                         "requested_by": "op"}, db=db, current_user=user)
        eir.approve_issue_slip(created["id"], db=db, current_user=user)
        eir.issue_slip(created["id"], db=db, current_user=user)
        row = db.query(models.MaterialIssueSlip).filter(models.MaterialIssueSlip.id == created["id"]).first()
        txn = (db.query(models.InventoryTransaction).filter(models.InventoryTransaction.notes.like(f"%{row.slip_no}%"))
               .first())
        return row, txn
    finally:
        tenancy.reset_current_tenant(tok)


def traced(db, tenant, wo_no):
    tok = tenancy.set_current_tenant(tenant)
    try:
        return trace.build_work_order_trace(db, tenant, wo_no)
    finally:
        tenancy.reset_current_tenant(tok)


def mats(t):
    """{item_code: row} from a trace's material genealogy (build_work_order_trace: materials.rows)."""
    return {m["item_code"]: m for m in ((t or {}).get("materials") or {}).get("rows", [])}


def listed(db, tenant):
    tok = tenancy.set_current_tenant(tenant)
    try:
        return eir.get_issue_slips(Response(), 100, 0, db=db, current_user=dict(ADMIN, tenant=tenant))
    finally:
        tenancy.reset_current_tenant(tok)


def main_():
    db, engine = session()
    seed(db)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _log(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())

    print("=" * 74)
    print("1. A SLIP THAT NAMES A WORK ORDER IN THIS TENANT COUNTS AGAINST IT")
    print("=" * 74)
    row, txn = slip(db, "TA", "WO-100", qty=20)
    check("the slip is issued", row.status == "Issued" and row.issued_qty == 20, str(row.status))
    check("the stock transaction references the WORK ORDER, not the slip", txn is not None and txn.reference == "WO-100",
          str(getattr(txn, "reference", None)))
    check("...and its note names the slip and the job", txn is not None and row.slip_no in (txn.notes or "")
          and "WO-100" in (txn.notes or ""), str(getattr(txn, "notes", None)))
    m1 = mats(traced(db, "TA", "WO-100"))
    check("WO-100's trace lists the steel as consumed, 20", m1.get("STEEL-TA", {}).get("consumed") == 20,
          str({k: v.get("consumed") for k, v in m1.items()}))

    print()
    print("=" * 74)
    print("2. A REFERENCE AMP DOES NOT KNOW KEEPS THE SLIP NUMBER AND SAYS SO; THE MATCH IS EXACT")
    print("=" * 74)
    row2, txn2 = slip(db, "TA", "JOB-EXT-7", qty=5)
    check("the transaction references the slip", txn2 is not None and txn2.reference == row2.slip_no,
          str(getattr(txn2, "reference", None)))
    check("...and the note says the job is not in AMP and not in any trace",
          txn2 is not None and "not a work order in AMP" in (txn2.notes or "") and "JOB-EXT-7" in (txn2.notes or ""),
          str(getattr(txn2, "notes", None)))
    row3, txn3 = slip(db, "TA", "WO-10 ", qty=3)                       # trailing space: trimmed
    check("a trimmed reference resolves ('WO-10 ' -> WO-10)", txn3 is not None and txn3.reference == "WO-10",
          str(getattr(txn3, "reference", None)))
    row4, txn4 = slip(db, "TA", "WO-1", qty=2)                         # a prefix of WO-10 and WO-100
    check("a prefix is not a match: 'WO-1' resolves to nothing", txn4 is not None and txn4.reference == row4.slip_no,
          str(getattr(txn4, "reference", None)))
    row5, txn5 = slip(db, "TA", "", qty=1)
    check("no reference: the slip number and 'unspecified job'", txn5 is not None and txn5.reference == row5.slip_no
          and "unspecified job" in (txn5.notes or ""), str(getattr(txn5, "notes", None)))
    m2 = mats(traced(db, "TA", "WO-100"))
    check("WO-100's trace still says 20 (the unresolved slips changed no trace)",
          m2.get("STEEL-TA", {}).get("consumed") == 20, str(m2.get("STEEL-TA")))
    m10 = mats(traced(db, "TA", "WO-10"))
    check("WO-10's trace says 3", m10.get("STEEL-TA", {}).get("consumed") == 3, str(m10.get("STEEL-TA")))

    print()
    print("=" * 74)
    print("3. THE LIST SAYS WHICH REFERENCES RESOLVED, IN ONE QUERY FOR THE PAGE")
    print("=" * 74)
    statements.clear()
    rows = listed(db, "TA")
    by_ref = {r["work_order_ref"]: r for r in rows}
    check("WO-100 resolved", by_ref["WO-100"]["work_order_resolved"] is True and by_ref["WO-100"]["work_order_no"] == "WO-100")
    check("JOB-EXT-7 not resolved", by_ref["JOB-EXT-7"]["work_order_resolved"] is False
          and by_ref["JOB-EXT-7"]["work_order_no"] is None)
    check("'WO-1' not resolved (exact match)", by_ref["WO-1"]["work_order_resolved"] is False)
    check("'' not resolved", by_ref[""]["work_order_resolved"] is False and by_ref[""]["work_order_no"] is None)
    wo_selects = [s for s in statements if "from work_orders" in s]
    check("one work-order lookup for the whole page", len(wo_selects) == 1, str(len(wo_selects)))
    ui = os.path.join(HERE, "..", "frontend", "components", "EnterpriseInventory.tsx")
    with open(ui, encoding="utf-8") as f:
        src = f.read()
    check("the screen shows an unresolved reference as such",
          "work_order_resolved === false" in src and "not a work order in AMP" in src)

    print()
    print("=" * 74)
    print("4. ANOTHER TENANT'S WO-100 IS NOT THIS TENANT'S")
    print("=" * 74)
    rowb, txnb = slip(db, "TB", "WO-100", qty=7)
    check("TB's slip resolves (to TB's WO-100)", txnb is not None and txnb.reference == "WO-100")
    mats_ta = mats(traced(db, "TA", "WO-100"))
    check("TA's WO-100 trace still says 20: TB's material is not in it",
          mats_ta.get("STEEL-TA", {}).get("consumed") == 20 and "STEEL-TB" not in mats_ta, str(mats_ta))
    mats_tb = mats(traced(db, "TB", "WO-100"))
    check("TB's WO-100 trace says 7 of TB's steel and nothing of TA's",
          mats_tb.get("STEEL-TB", {}).get("consumed") == 7 and "STEEL-TA" not in mats_tb, str(mats_tb))

    event.remove(engine, "before_cursor_execute", _log)
    db.close()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_issue_slip_counts_against_its_job():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
