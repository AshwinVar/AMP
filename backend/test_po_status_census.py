"""The purchasing card showed a PO total larger than its own parts.

THE DEFECT
----------
`/analytics/purchasing` publishes `purchase_orders` — the total — beside a
status breakdown, and `PurchasingSection.tsx:94-99` renders them on one row:

    POs   Open   Partial   Received   Overdue   Ordered   Received Qty   Rate

The breakdown is built by looking up four exact strings in a GROUP BY:

    "open":      status_counts.get("Open", 0)
    "partial":   status_counts.get("Partial", 0)
    "received":  status_counts.get("Received", 0)
    "cancelled": status_counts.get("Cancelled", 0)

Nothing in AMP writes `"Partial"`. The words actually written are:

    factory_simulator.py:404  ["Open", "Open", "Partially Received",
                               "Received", "Overdue"]
    ai/agents.py:262          "Draft"        (the Reorder agent's proposal)
    ai/agents.py:127          "Approved" on accept, "Cancelled" on reject
    models.py:343             Column(String, default="Open"), nullable

So on the seeded/simulated plant, of nine POs: three Open, two **Partially
Received**, two Received, two **Overdue**. The card reads

    POs 9   Open 3   Partial 0   Received 2

— four purchase orders in no bucket at all, and a `Partial` KPI that is
structurally incapable of being anything but zero however much of the book is
partly delivered. Every PO the Reorder agent has drafted is invisible the same
way, which is worse: those are the ones waiting on a human decision.

`overdue` is NOT part of that partition and is not being changed here. It is a
date-based cross-cut (`expected_delivery_date < today` on a non-terminal PO),
so a PO can be Open and overdue at once. It answers a different question.

THE FIX
-------
One vocabulary, `ai/supply.STATUS_BUCKETS`, mapping every status word this
repository actually writes onto the bucket it belongs in — matched lowercased
and trimmed, like the sets already beside it — plus an explicit `other` bucket
for anything unrecognised, including NULL. Every mapping below is grounded in a
writer in this repo, not invented:

    draft      Draft                      (agent proposal, not yet an order)
    open       Open, Approved, Overdue    (outstanding with a supplier)
    partial    Partially Received, Partial
    received   Received, Closed, Completed, Complete, Delivered
    cancelled  Cancelled, Canceled
    other      anything else, and NULL

`Overdue` folds into `open` because as a STATUS word it means an outstanding
order that is late — it is open. The card's separate `Overdue` KPI still
reports lateness from the date, so nothing is lost and the buckets stop
double-counting the same idea in two shapes.

`Draft` gets its own bucket rather than folding into `open`: an agent proposal
nobody has approved is not a commitment to a supplier, and burying it in `open`
would overstate committed spend on the card a buyer reads.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_po_status_census.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import orders_routes
import tenancy
from ai import supply
from database import Base

T = "POCENSUS"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []

# One PO per status word any writer in this repo produces. The NULL row is
# written separately in SQL — see seed().
ROWS = [
    ("PO-OPEN", "Open"),
    ("PO-APPROVED", "Approved"),            # ai/agents.py:127, on approval
    ("PO-OVERDUE", "Overdue"),              # factory_simulator.py:404
    ("PO-PARTIALLY", "Partially Received"),  # factory_simulator.py:404
    ("PO-RECEIVED", "Received"),
    ("PO-CANCELLED", "Cancelled"),          # ai/agents.py:127, on rejection
    ("PO-DRAFT", "Draft"),                  # ai/agents.py:262
    ("PO-WEIRD", "Awaiting customs"),       # nothing writes this; `other` must exist
    ("PO-NULL", None),
]


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
    sup = models.Supplier(tenant_code=T, supplier_code="SUP-1", supplier_name="Acme",
                          contact_person="R", phone="1", email="a@b.c",
                          category="Raw", status="Active")
    db.add(sup)
    db.flush()
    # Every PO is due in the FUTURE, so `overdue` is 0 throughout and cannot
    # quietly account for a bucket gap.
    due = datetime.utcnow().date() + timedelta(days=30)
    for po_no, status in ROWS:
        po = models.PurchaseOrder(tenant_code=T, po_no=po_no, supplier_id=sup.id,
                                  item_name="Bar stock", order_quantity=100,
                                  received_quantity=0, unit="kg",
                                  expected_delivery_date=due)
        if status is not None:
            po.status = status
        db.add(po)
    db.commit()
    # status is Column(String, default="Open"), and a SQLAlchemy column default
    # is applied on INSERT when the attribute is None — constructing with
    # status=None silently stores "Open" and the NULL case is never exercised.
    db.execute(text("UPDATE purchase_orders SET status = NULL WHERE po_no = 'PO-NULL'"))
    db.commit()
    assert db.execute(text("SELECT status FROM purchase_orders WHERE po_no='PO-NULL'")
                      ).scalar() is None, "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    a = orders_routes.get_purchasing_analytics(db, USER)

    print("=" * 74)
    print("1. THE PARTS ADD UP TO THE TOTAL")
    print("=" * 74)
    buckets = ("draft", "open", "partial", "received", "cancelled", "other")
    parts = sum(a[b] for b in buckets)
    check(f"purchase_orders = {a['purchase_orders']} (every seeded row)",
          a["purchase_orders"] == len(ROWS), str(a["purchase_orders"]))
    check(f"draft+open+partial+received+cancelled+other == purchase_orders "
          f"({parts} vs {a['purchase_orders']})",
          parts == a["purchase_orders"],
          " ".join(f"{b}={a[b]}" for b in buckets))

    print()
    print("=" * 74)
    print("2. EVERY WORD ANY WRITER PRODUCES LANDS SOMEWHERE REAL")
    print("=" * 74)
    # The bucket that could only ever be zero. "Partial" is not a word this
    # repository writes; "Partially Received" is, and a fifth of the simulated
    # book carries it.
    check("a 'Partially Received' PO counts as partial", a["partial"] == 1,
          str(a["partial"]))
    # Open, Approved and Overdue are all outstanding orders with a supplier.
    check("Open + Approved + Overdue are all open (3)", a["open"] == 3,
          str(a["open"]))
    check("the agent's unapproved Draft is its own bucket, not folded into open",
          a["draft"] == 1, str(a["draft"]))
    check("Received is received", a["received"] == 1, str(a["received"]))
    check("Cancelled is cancelled", a["cancelled"] == 1, str(a["cancelled"]))
    check("an unrecognised word and a NULL fall to 'other' (2), rather than "
          "vanishing", a["other"] == 2, str(a["other"]))

    print()
    print("=" * 74)
    print("3. THE OVERDUE CROSS-CUT IS UNTOUCHED")
    print("=" * 74)
    # `overdue` is date-based and orthogonal: a PO can be Open AND overdue.
    # Every fixture row is due in 30 days, so it must be 0 even though one row
    # carries the literal status word "Overdue".
    check("overdue = 0 — nothing is past its date, whatever the status says",
          a["overdue"] == 0, str(a["overdue"]))
    row = db.query(models.PurchaseOrder).filter(
        models.PurchaseOrder.po_no == "PO-OPEN").first()
    row.expected_delivery_date = datetime.utcnow().date() - timedelta(days=5)
    db.commit()
    a2 = orders_routes.get_purchasing_analytics(db, USER)
    check("...and it counts a genuinely past-due open PO", a2["overdue"] == 1,
          str(a2["overdue"]))
    check("...while the status buckets do not move (it is still Open)",
          a2["open"] == a["open"], f"{a2['open']} vs {a['open']}")
    check("...and the parts still add up",
          sum(a2[b] for b in buckets) == a2["purchase_orders"],
          " ".join(f"{b}={a2[b]}" for b in buckets))

    print()
    print("=" * 74)
    print("4. THE VOCABULARY IS ONE MAPPING, MATCHED FORGIVINGLY")
    print("=" * 74)
    check("'Partially Received' -> partial",
          supply.status_bucket("Partially Received") == "partial",
          supply.status_bucket("Partially Received"))
    check("case and padding do not decide it",
          supply.status_bucket("  RECEIVED  ") == "received",
          supply.status_bucket("  RECEIVED  "))
    check("'canceled' (one l) -> cancelled",
          supply.status_bucket("canceled") == "cancelled",
          supply.status_bucket("canceled"))
    check("NULL -> other", supply.status_bucket(None) == "other",
          supply.status_bucket(None))
    check("an unknown word -> other, never silently dropped",
          supply.status_bucket("Awaiting customs") == "other",
          supply.status_bucket("Awaiting customs"))
    # The mapping DERIVES its received/cancelled/late halves from the sets
    # `_state()` already classifies by, so there is one list of what "received"
    # means rather than two. Asserted directly: a fixture cannot pin it, because
    # it would need a row per synonym, and mutation testing showed exactly that
    # — replacing the derivation with a single literal "received" entry survived
    # until this check existed.
    check("every RECEIVED_STATUSES word buckets as received",
          {supply.status_bucket(s) for s in supply.RECEIVED_STATUSES} == {"received"},
          str({s: supply.status_bucket(s) for s in supply.RECEIVED_STATUSES}))
    check("every CANCELLED_STATUSES word buckets as cancelled",
          {supply.status_bucket(s) for s in supply.CANCELLED_STATUSES} == {"cancelled"},
          str({s: supply.status_bucket(s) for s in supply.CANCELLED_STATUSES}))
    check("every LATE_STATUSES word buckets as open — an overdue order is still "
          "outstanding, and lateness is reported from the date instead",
          {supply.status_bucket(s) for s in supply.LATE_STATUSES} == {"open"},
          str({s: supply.status_bucket(s) for s in supply.LATE_STATUSES}))
    # And the two classifiers must not disagree about the same word.
    today_ = datetime.utcnow().date()
    clash = [w for w in supply.STATUS_BUCKETS
             if (supply.status_bucket(w) == "cancelled")
             != (supply._state(_po(w, 100, 0, today_ + timedelta(days=30)), today_)
                 == "cancelled")]
    check("status_bucket and _state agree on which words mean cancelled",
          not clash, str(clash))
    # A bucket key that no status maps to is a KPI that can only read zero —
    # the defect this file exists for. Every published bucket must be reachable.
    reachable = {supply.status_bucket(w) for w in supply.STATUS_BUCKETS}
    check(f"every published bucket is reachable from some real word "
          f"({sorted(reachable)})",
          set(buckets) - {"other"} <= reachable, str(sorted(reachable)))

    print()
    print("=" * 74)
    print("5. THE SIMULATED PLANT, WHICH IS WHERE THIS SHOWED")
    print("=" * 74)
    # factory_simulator.py:404 verbatim. Nine POs, cycling this list, is what a
    # demo or a fresh tenant actually contains.
    sim = ["Open", "Open", "Partially Received", "Received", "Overdue"]
    counted = sum(1 for i in range(1, 10)
                  if supply.status_bucket(sim[i % len(sim)]) != "other")
    check("all nine simulated POs land in a real bucket (none in 'other')",
          counted == 9, str(counted))

    tenancy.reset_current_tenant(tok)
    db.close()

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


class _po:
    """A stand-in PO, so the classifier checks do not need extra rows."""

    def __init__(self, status, ordered, received, due):
        self.status = status
        self.order_quantity = ordered
        self.received_quantity = received
        self.expected_delivery_date = due


def test_po_status_census():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
