"""Cancelling a purchase order damaged the supplier's reliability score.

THE DEFECT
----------
`ai/supply.py:_state()` classified every purchase order by receipt state, and
had no idea what "Cancelled" meant:

    RECEIVED_STATUSES = {"received", "closed", "completed", "complete", "delivered"}
    LATE_STATUSES = {"overdue"}
    ...
    due = po.expected_delivery_date
    if (due - today).days < 0:
        return "late"

So a CANCELLED purchase order past its expected date came back as **late** —
and "late" is load-bearing in three separate places:

1. `reliability_rate = received / (received + late)`, plant-wide and per
   supplier. A cancelled PO lands in the DENOMINATOR, so withdrawing an order
   lowers that supplier's measured reliability. The buyer cancels; the supplier
   takes the mark.

2. `by_supplier.sort(key=(late, at_risk, -receipt_rate), reverse=True)` — "worst
   first". Cancelled POs push a supplier up the worst-suppliers list, which is
   the list a buyer uses to decide who to stop buying from.

3. The chase list — `state in ("late", "at_risk")` — tells the buyer to chase a
   PO nobody is waiting for, with `days_to_due` counting up forever.

And `upcoming` counted "not-yet-received POs due in the next 7 days" as
`_state(p) != "received"`, so cancelled orders were reported as inbound load
that will never arrive.

It also disagreed with the rest of AMP. `orders_routes.py:690` and `:750` — the
`/analytics/purchasing` overdue count and the overdue-PO escalation generator —
both treat `("Received", "Cancelled")` as terminal, so the same cancelled PO is
"not overdue" on the purchasing card and "late" on the supply outlook, on the
same plant at the same moment.

THE FIX
-------
`_state()` gains a `cancelled` state (CANCELLED_STATUSES, matched lowercase like
the sets beside it, so "canceled" and "  Cancelled  " land too). It is not
received — nothing arrived — and it is not late, because nobody is waiting.

`cancelled` is published in the state counts, because `total` is published
beside them: a census that does not partition its vocabulary is a total that
does not add up. It stays out of the reliability denominator, the chase list and
the upcoming-inbound count.

Deliberately NOT changed: `receipt_rate` is units received over units ordered
across the whole book, and `/analytics/purchasing` computes its own the same way
over the same rows. Changing one and not the other would trade this defect for a
different disagreement.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_cancelled_po_not_late.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import orders_routes
import tenancy
from ai import supply
from database import Base

T = "CANPO"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []


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
    sup = models.Supplier(tenant_code=T, supplier_code="SUP-ACME",
                          supplier_name="Acme Steel",
                          contact_person="R", phone="1", email="a@b.c",
                          category="Raw material", status="Active")
    db.add(sup)
    db.flush()
    today = datetime.utcnow().date()
    # One supplier, four POs, all past due, differing only in status. That
    # isolates the classification: any difference in the numbers below is the
    # status word and nothing else.
    rows = [
        ("PO-RECEIVED", "Received", 100, 100, today - timedelta(days=10)),
        ("PO-LATE", "Open", 100, 0, today - timedelta(days=10)),
        ("PO-CANCELLED", "Cancelled", 100, 0, today - timedelta(days=10)),
        ("PO-ONTRACK", "Open", 100, 0, today + timedelta(days=30)),
    ]
    for po_no, status, ordered, received, due in rows:
        db.add(models.PurchaseOrder(
            tenant_code=T, po_no=po_no, supplier_id=sup.id, item_name="Bar stock",
            order_quantity=ordered, received_quantity=received, unit="kg",
            status=status, expected_delivery_date=due))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    today = datetime.utcnow().date()
    pos = {p.po_no: p for p in db.query(models.PurchaseOrder).all()}

    print("=" * 74)
    print("1. A WITHDRAWN ORDER IS NOT A LATE ONE")
    print("=" * 74)
    check("a Cancelled PO past its date is 'cancelled', not 'late'",
          supply._state(pos["PO-CANCELLED"], today) == "cancelled",
          supply._state(pos["PO-CANCELLED"], today))
    check("...a genuinely outstanding overdue PO is still 'late'",
          supply._state(pos["PO-LATE"], today) == "late",
          supply._state(pos["PO-LATE"], today))
    check("...a fully received one is still 'received'",
          supply._state(pos["PO-RECEIVED"], today) == "received",
          supply._state(pos["PO-RECEIVED"], today))
    check("...and one not yet due is still 'on_track'",
          supply._state(pos["PO-ONTRACK"], today) == "on_track",
          supply._state(pos["PO-ONTRACK"], today))
    check("vocabulary drift is handled like the sets beside it ('canceled')",
          supply._state(_as(pos["PO-CANCELLED"], "canceled"), today) == "cancelled")
    check("...and case and padding do not decide it",
          supply._state(_as(pos["PO-CANCELLED"], "  CANCELLED  "), today) == "cancelled")
    # Ordering. A cancelled PO whose goods actually ARRIVED was classified
    # `received` before this rule existed and must stay that way: the quantity
    # is evidence of what physically happened, the status word is a label added
    # afterwards, and reliability_rate asks whether the supplier delivered.
    # They did. Ranking cancelled first would take that delivery out of the
    # numerator — a change beyond the defect. Mutation testing forced this to be
    # decided rather than assumed: with the fixture's cancelled PO at 0
    # received, both orderings gave identical answers, so the mutation that
    # reordered them survived.
    past = today - timedelta(days=10)
    check("a cancelled PO whose goods DID arrive in full is still 'received'",
          supply._state(_po("Cancelled", 100, 100, past), today) == "received",
          supply._state(_po("Cancelled", 100, 100, past), today))
    check("...but one that arrived only partly is cancelled, not late",
          supply._state(_po("Cancelled", 100, 40, past), today) == "cancelled",
          supply._state(_po("Cancelled", 100, 40, past), today))

    print()
    print("=" * 74)
    print("2. THE SUPPLIER IS NOT MARKED DOWN FOR THE BUYER'S DECISION")
    print("=" * 74)
    s = supply.build_supply_summary(db, T)
    # Two POs have come due and resolved: one received, one genuinely late.
    # 1 of 2 = 50%. With the cancelled one counted late it was 1 of 3 = 33%.
    check(f"plant reliability is 50% (1 received of 2 resolved), not 33%",
          s["reliability_rate"] == 50, str(s["reliability_rate"]))
    check(f"resolved = 2, not 3 (a cancelled PO never came due)",
          s["resolved"] == 2, str(s["resolved"]))
    acme = next(x for x in s["by_supplier"] if x["supplier"] == "Acme Steel")
    check("the supplier's own reliability is 50% too",
          acme["reliability_rate"] == 50, str(acme["reliability_rate"]))
    check("the supplier is credited with exactly one late PO, not two",
          acme["late"] == 1, str(acme["late"]))

    print()
    print("=" * 74)
    print("3. NOBODY IS ASKED TO CHASE AN ORDER THAT WAS WITHDRAWN")
    print("=" * 74)
    chased = [c["po_no"] for c in s["chase"]]
    check("the genuinely late PO is on the chase list",
          "PO-LATE" in chased, str(chased))
    check("the CANCELLED one is not", "PO-CANCELLED" not in chased, str(chased))
    check("...nor is the received one", "PO-RECEIVED" not in chased, str(chased))

    print()
    print("=" * 74)
    print("4. THE STATE COUNTS STILL ADD UP TO THE TOTAL")
    print("=" * 74)
    # `total` is published beside the breakdown, so the breakdown has to
    # partition the vocabulary — otherwise the card shows a total that is
    # larger than its own parts and nothing on screen explains the gap.
    parts = s["received"] + s["on_track"] + s["at_risk"] + s["late"] + s["cancelled"]
    check(f"received+on_track+at_risk+late+cancelled == total ({parts} vs {s['total']})",
          parts == s["total"], f"{parts} vs {s['total']}")
    check("cancelled is reported, not silently dropped", s["cancelled"] == 1,
          str(s.get("cancelled")))
    check("late is 1", s["late"] == 1, str(s["late"]))

    print()
    print("=" * 74)
    print("5. A WITHDRAWN ORDER IS NOT INBOUND LOAD")
    print("=" * 74)
    # Move the cancelled PO into the upcoming window; it must not be counted as
    # stock about to arrive.
    tomorrow = today + timedelta(days=1)
    pos["PO-CANCELLED"].expected_delivery_date = tomorrow
    pos["PO-ONTRACK"].expected_delivery_date = tomorrow
    db.commit()
    s2 = supply.build_supply_summary(db, T)
    day = next(d for d in s2["upcoming"] if d["date"] == tomorrow.isoformat())
    check("tomorrow's inbound load counts the live PO only (1, not 2)",
          day["pos"] == 1, str(day["pos"]))
    d2 = supply.build_supplier_detail(db, T, "Acme Steel")
    day2 = next(d for d in d2["upcoming"] if d["date"] == tomorrow.isoformat())
    check("...and the supplier drill-down agrees", day2["pos"] == 1, str(day2["pos"]))

    print()
    print("=" * 74)
    print("6. THE DRILL-DOWN AND /analytics/purchasing AGREE")
    print("=" * 74)
    check("the drill-down counts one late PO", d2["late"] == 1, str(d2["late"]))
    check("...and reports the cancelled one separately",
          d2["cancelled"] == 1, str(d2.get("cancelled")))
    check("its parts add up to its total",
          d2["received"] + d2["on_track"] + d2["at_risk"] + d2["late"]
          + d2["cancelled"] == d2["total"], str(d2))
    check("the drill-down's reliability matches the summary's",
          d2["reliability_rate"] == acme["reliability_rate"],
          f"detail={d2['reliability_rate']} summary={acme['reliability_rate']}")
    # The other half of the disagreement: /analytics/purchasing has always
    # excluded Cancelled from overdue. Both sides must now name the same PO.
    pos["PO-CANCELLED"].expected_delivery_date = today - timedelta(days=10)
    pos["PO-ONTRACK"].expected_delivery_date = today + timedelta(days=30)
    db.commit()
    purch = orders_routes.get_purchasing_analytics(db, USER)
    s3 = supply.build_supply_summary(db, T)
    check(f"/analytics/purchasing overdue = {purch['overdue']} and the supply "
          f"outlook late = {s3['late']}",
          purch["overdue"] == s3["late"], f"purchasing={purch['overdue']} supply={s3['late']}")

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
    """A stand-in PO, so the classification checks do not have to write and roll
    back extra rows and disturb the census assertions above."""

    def __init__(self, status, ordered, received, due):
        self.status = status
        self.order_quantity = ordered
        self.received_quantity = received
        self.expected_delivery_date = due


def _as(po, status):
    """The same PO with a different status word."""
    return _po(status, po.order_quantity, po.received_quantity,
               po.expected_delivery_date)


def test_cancelled_po_not_late():
    """The pytest entry point — see test_open_escalation_one_rule.py for why a
    suite exposing only main() contributes nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
