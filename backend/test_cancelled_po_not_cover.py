"""A purchase order the buyer cancelled still counted as stock on its way.

THE DEFECT
----------
`ai/coverage.build_part_runway` answers the question a buyer acts on: this part
runs dry on the 14th — is anything landing before then? It reads the part's POs
and skips only the ones already received:

    po_state = _po_state(p, today)
    if po_state == "received":
        continue                     # already in stock, not future cover
    outstanding = max(0, (p.order_quantity or 0) - (p.received_quantity or 0))
    inbound_units += outstanding

Everything else becomes future cover — including a **cancelled** PO. The verdict
below is computed from that list, so a withdrawn order dated before the stockout
produces `cover_verdict = "covered"`, and `PartRunwayDrawer.tsx:82` renders:

    Covered by inbound
    600 kg on order, first landing 12 Mar — before the 14 Mar stockout.

The buyer reads that and does not reorder. Nothing arrives, because the order
was cancelled. This is the one defect in this family with a physical
consequence: a stockout, and a line that stops.

`ai/supply.py` — which this module already imports and whose docstring here says
"ai.supply owns the receipt-state definition" — has carried the answer since
#566:

    # States where nothing is going to arrive, so the PO is not inbound load.
    _NOT_INBOUND = ("received", "cancelled")

`build_supply_summary` and `build_supplier_detail` both use it for exactly this
question ("is this PO inbound load?"). The coverage drill-down asks the same
question and answered it itself, one state short.

NOTE ON HISTORY: this predates #566. Before that, `_state` returned "late" or
"on_track" for a cancelled PO, so it was counted then too — #566 gave the state
a name without teaching this consumer to use it. That is the shape of the whole
family: the rule got a home, and a caller kept its own copy.

THE FIX
-------
Skip `_NOT_INBOUND`, not just `"received"`. One import, and the rule has one
home for every consumer of it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_cancelled_po_not_cover.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import coverage
from database import Base

T = "COVER"
ITEM = "RM-STEEL-001"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(po_status):
    """A part burning 20/day with 100 in stock — five days of cover — and one
    purchase order for 600 due in three days, in the given status.

    Five days of cover with a PO landing on day three is the exact situation the
    drawer exists for: the answer flips on whether that PO is real."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    sup = models.Supplier(tenant_code=T, supplier_code="S1", supplier_name="Acme",
                          contact_person="R", phone="1", email="a@b.c",
                          category="Raw", status="Active")
    item = models.InventoryItem(tenant_code=T, item_code=ITEM, item_name="Bar stock",
                                category="Raw", unit="kg", current_stock=100,
                                reorder_level=10)
    db.add_all([sup, item])
    db.flush()
    now = datetime.utcnow()
    # 20/day out for the last 14 days -> daily_burn 20, cover 5 days.
    for i in range(14):
        db.add(models.InventoryTransaction(
            tenant_code=T, item_id=item.id, transaction_type="Issue",
            quantity=20, reference="WO", created_at=now - timedelta(days=i)))
    if po_status is not None:
        db.add(models.PurchaseOrder(
            tenant_code=T, po_no="PO-1", supplier_id=sup.id, item_id=item.id,
            item_name="Bar stock", order_quantity=600, received_quantity=0,
            unit="kg", status=po_status,
            expected_delivery_date=now.date() + timedelta(days=3)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def runway(db):
    tok = tenancy.set_current_tenant(T)
    try:
        return coverage.build_part_runway(db, T, ITEM)
    finally:
        tenancy.reset_current_tenant(tok)


def main():
    print("=" * 74)
    print("1. A WITHDRAWN ORDER IS NOT STOCK ON ITS WAY")
    print("=" * 74)
    db = seed("Cancelled")
    r = runway(db)
    print(f"      verdict={r['cover_verdict']}  inbound_units={r['inbound_units']}"
          f"  inbound={[p['po_no'] for p in r['inbound']]}")
    check("a cancelled PO is not counted as inbound units",
          r["inbound_units"] == 0, str(r["inbound_units"]))
    check("...nor listed as something that will arrive",
          r["inbound"] == [], str([p["po_no"] for p in r["inbound"]]))
    check("...so the verdict is 'nothing on order', not 'covered'",
          r["cover_verdict"] == "no_inbound", r["cover_verdict"])
    db.close()

    print()
    print("=" * 74)
    print("2. A LIVE ORDER STILL COVERS THE PART — THE CONTROL")
    print("=" * 74)
    # Without this, "skip everything" would pass section 1 while destroying the
    # feature. Same part, same dates, one word different.
    db = seed("Open")
    r = runway(db)
    print(f"      verdict={r['cover_verdict']}  inbound_units={r['inbound_units']}")
    check("an Open PO is inbound cover", r["inbound_units"] == 600,
          str(r["inbound_units"]))
    check("...and lands before the stockout, so the part is covered",
          r["cover_verdict"] == "covered", r["cover_verdict"])
    db.close()

    print()
    print("=" * 74)
    print("3. THE OTHER STATES ARE UNCHANGED")
    print("=" * 74)
    # received: already in stock, must not be double-counted as future cover.
    db = seed("Received")
    r = runway(db)
    check("a Received PO is not future cover (unchanged behaviour)",
          r["inbound_units"] == 0 and r["cover_verdict"] == "no_inbound",
          f"{r['inbound_units']} {r['cover_verdict']}")
    db.close()
    # An overdue-but-live PO is still coming, just late. It must stay.
    db = seed("Overdue")
    r = runway(db)
    check("an Overdue PO is still inbound — late is not withdrawn",
          r["inbound_units"] == 600, str(r["inbound_units"]))
    db.close()
    db = seed(None)
    r = runway(db)
    check("no PO at all -> no_inbound", r["cover_verdict"] == "no_inbound",
          r["cover_verdict"])
    db.close()

    print()
    print("=" * 74)
    print("4. THE RULE HAS ONE HOME")
    print("=" * 74)
    from ai import supply
    check("ai.supply._NOT_INBOUND is what coverage skips",
          set(coverage._NOT_INBOUND) == set(supply._NOT_INBOUND),
          f"{coverage._NOT_INBOUND} vs {supply._NOT_INBOUND}")
    check("...and it contains both states where nothing arrives",
          set(supply._NOT_INBOUND) == {"received", "cancelled"},
          str(supply._NOT_INBOUND))
    # The states that DO arrive must not have been swept in with them.
    today = datetime.utcnow().date()

    class _Po:
        def __init__(s, st):
            s.status, s.order_quantity, s.received_quantity = st, 100, 0
            s.expected_delivery_date = today + timedelta(days=3)
    for word in ("Open", "Approved", "Partially Received", "Overdue"):
        st = supply._state(_Po(word), today)
        check(f"'{word}' -> '{st}', which is still inbound",
              st not in supply._NOT_INBOUND, st)

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_cancelled_po_not_cover():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
