"""The command centre's Work Orders KPI missed the ones being worked.

THE DEFECT
----------
`/analytics/factory-command-center` publishes `active_work_orders`, rendered as
`DigitalTwinSection.tsx:167` "Work Orders". It counted:

    models.WorkOrder.status.in_(["Running", "Planned"])

`factory_simulator.py:279` writes the work-order book like this:

    statuses = ["Planned", "In Progress", "In Progress", "In Progress",
                "Completed", "On Hold"]

So on a seeded plant **half the book is "In Progress"** and none of it is
"Running" — nothing in this repository writes that word to a WorkOrder except
the UI dropdown. The KPI counted the Planned sixth and called it the active
work. "On Hold" — outstanding work nobody has closed — was missing too.

THE SAME PROBLEM WAS ALREADY FOUND AND FIXED ONE ENDPOINT AWAY
---------------------------------------------------------------
`analytics_routes.py:448`, in `/analytics/work-orders`, carries this comment:

    # "Running" and "In Progress" are two spellings of the SAME state (a work
    # order actively being worked) written by two code paths in this app: the
    # UI status dropdown (WorkOrdersSection) writes "Running", the factory
    # simulator ... and the e2e sim write "In Progress". Reading the "Running"
    # bucket alone reported 0 in-progress work orders on a seeded/simulated
    # plant while those rows sat plainly in the work-order table below it

That fix folded the synonym for that card. The command centre, 570 lines below
in the same file, kept the whitelist — and its own comment explains why it
survived a rewrite:

    # each predicate is written to keep the numbers BYTE-IDENTICAL to the
    # Python versions

The SQL rewrite faithfully preserved the bug, which is exactly what
byte-identical asks for. Nothing was wrong with that intent; what was missing
is that the vocabulary had no single home to be rewritten *against*.

FOUR DEFINITIONS OF "AN ACTIVE WORK ORDER"
--------------------------------------------
    analytics_routes:1020   IN ("Running", "Planned")            <- the defect
    analytics_routes:462    Running + In Progress folded          <- correct
    ai/flow.py:70           NOT IN _CLOSED, case/space/NULL-safe   <- correct
    predictive_engine:33    ("Running", "Delayed")                 <- matches
                                                                    NOTHING

THE FIX
-------
`work_order_status.py`, modelled on `machine_status.py`: the terminal set, and
everything else is open. Taken from `ai/flow.py`'s definition rather than
invented, because that one was already right — and flow now imports it, so the
rule has one home instead of a new second one.

Deliberately a COMPLEMENT, not a whitelist. Every defect in this family came
from a whitelist missing a word: "In Progress" here, "In Progress" in #568,
"Partially Received" in #569, "Corrective" in #576. A closed-set complement
cannot miss a word nobody thought of; it can only mis-classify one somebody
deliberately added to the terminal list.

NOT IN THIS PR: `predictive_engine.ACTIVE_WORK_ORDER_STATUSES`. It is dead on
any seeded plant for the same reason, but repairing it changes published
predictive RISK SCORES, which deserves its own change and its own measurement
rather than riding along with a KPI fix.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_work_order_active.py
"""
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
import work_order_status
from database import Base

T = "WOACTIVE"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []

# factory_simulator.py:279 verbatim, plus a NULL. This IS the seeded book.
SIM_STATUSES = ["Planned", "In Progress", "In Progress", "In Progress",
                "Completed", "On Hold"]
ROWS = [(f"WO-{i}", s) for i, s in enumerate(SIM_STATUSES)] + [("WO-NULL", None)]


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    m = models.Machine(tenant_code=T, name="CNC-01", site="P1", status="Running",
                       utilization=80, downtime="0 min")
    db.add(m)
    db.flush()
    for no, status in ROWS:
        w = models.WorkOrder(tenant_code=T, work_order_no=no, part_number="SHAFT-001",
                             batch_number="B1", machine_id=m.id,
                             target_quantity=10, actual_quantity=0)
        if status is not None:
            w.status = status
        db.add(w)
    db.commit()
    db.execute(text("UPDATE work_orders SET status = NULL WHERE work_order_no='WO-NULL'"))
    db.commit()
    assert db.execute(text("SELECT status FROM work_orders WHERE work_order_no='WO-NULL'")
                      ).scalar() is None, "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    cc = analytics_routes.get_factory_command_center(db, USER)

    print("=" * 74)
    print("1. THE WORK ORDERS BEING WORKED ARE COUNTED AS ACTIVE")
    print("=" * 74)
    # Planned + 3x In Progress + On Hold + NULL = 6 open. Only Completed is not.
    check(f"active_work_orders = {cc['active_work_orders']}, not just the "
          f"Planned one", cc["active_work_orders"] == 6,
          str(cc["active_work_orders"]))
    check("...which is every row that is not closed",
          cc["active_work_orders"] == sum(1 for _, s in ROWS
                                          if not work_order_status.is_closed(s)),
          str(cc["active_work_orders"]))

    print()
    print("=" * 74)
    print("2. THE VOCABULARY IS A COMPLEMENT, NOT A WHITELIST")
    print("=" * 74)
    check("'In Progress' — what the simulator writes 3 times in 6 — is active",
          not work_order_status.is_closed("In Progress"))
    check("'Running' — what the UI dropdown writes — is active too",
          not work_order_status.is_closed("Running"))
    check("'On Hold' is open work nobody has closed",
          not work_order_status.is_closed("On Hold"))
    check("a NULL status is active, not silently dropped",
          not work_order_status.is_closed(None))
    check("'Completed' is closed", work_order_status.is_closed("Completed"))
    check("'Cancelled' is closed", work_order_status.is_closed("Cancelled"))
    check("case and padding do not decide it",
          work_order_status.is_closed("  COMPLETED  "))
    # A word nobody has thought of yet must default to ACTIVE. That is the whole
    # argument for a complement: every defect in this family came from a
    # whitelist missing a word.
    check("an unknown word defaults to active, not dropped",
          not work_order_status.is_closed("Awaiting jig"))

    print()
    print("=" * 74)
    print("3. THE SQL AND THE PYTHON AGREE ROW-FOR-ROW")
    print("=" * 74)
    sql_ids = {w.id for w in db.query(models.WorkOrder)
               .filter(work_order_status.open_clause()).all()}
    py_ids = {w.id for w in db.query(models.WorkOrder).all()
              if not work_order_status.is_closed(w.status)}
    check(f"open_clause() selects exactly what is_closed() rejects "
          f"({len(sql_ids)} rows)", sql_ids == py_ids,
          f"sql={sorted(sql_ids)} python={sorted(py_ids)}")

    print()
    print("=" * 74)
    print("4. ai/flow.py IS UNCHANGED — IT WAS ALREADY RIGHT")
    print("=" * 74)
    # flow's private _CLOSED became the shared one. Its behaviour must not move:
    # this fix removes a duplicate, it does not change the WIP board.
    from ai import flow
    check("flow uses the shared closed set",
          set(flow._CLOSED) == set(work_order_status.CLOSED_STATUSES),
          f"{sorted(flow._CLOSED)} vs {sorted(work_order_status.CLOSED_STATUSES)}")
    f = flow.build_wip_aging(db, T)
    check(f"the WIP board still sees the same 6 open orders ({f['open']})",
          f["open"] == 6, str(f["open"]))

    print()
    print("=" * 74)
    print("5. THE SIBLING CARD STILL RECONCILES")
    print("=" * 74)
    # /analytics/work-orders folded the synonym in its own way (#444-era). It
    # must still add up — this change must not have disturbed it.
    wo = analytics_routes.get_work_order_analytics(db, USER)
    parts = wo["planned"] + wo["running"] + wo["completed"] + wo["delayed"]
    check(f"running counts all three In Progress rows ({wo['running']})",
          wo["running"] == 3, str(wo["running"]))
    check(f"...and its buckets still describe the book "
          f"({parts} of {wo['total_work_orders']})",
          parts <= wo["total_work_orders"], f"{parts} vs {wo['total_work_orders']}")

    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f_ in failures:
            print(f"  - {f_}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_work_order_active():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
