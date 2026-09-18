"""A goods receipt's quantities decide its inspection result, not a dropdown.

THE DEFECT
----------
POST /grns stored each line's received, accepted and rejected quantities and an
`inspection_status` exactly as the client sent them, with nothing tying them
together, and accept_grn adds each line's `accepted_qty` to stock whatever its
status says. The GRN form has no rejected-quantity input at all (it always sends
`rejected_qty: 0`) and a status dropdown that defaults to "Accepted". So:

* received 100, accepted 80, the dropdown left at its default -> stored as
  "Accepted", 0 rejected: 20 rejected units vanished from the record;
* the dropdown set to "Rejected", accepted left at 100 -> stored "Rejected",
  and accepting the GRN put all 100 into stock under a "Rejected" badge;
* accepted greater than received was stored, and put into stock.

THE RULE
--------
The quantities are the truth. accepted may not exceed received (400); rejected is
received - accepted; the inspection result is "Accepted" when everything
received was accepted, "Rejected" when nothing was, else "Partial". A client's
rejected_qty and inspection_status are not believed. Every stored line then
satisfies accepted + rejected = received, with a status that matches.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_grn_quantities_decide_inspection.py
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import enterprise_inventory_routes as eir
import models
import tenancy
from database import Base

USER = {"sub": "sup", "tenant": "DEFAULT", "role": "Supervisor"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.InventoryItem(id=1, item_code="BOLT-M8", item_name="Bolt M8", category="Fasteners",
                                current_stock=0, reorder_level=10, unit="pcs", location="A1"))
    db.commit()
    return db


def _grn(db, **line):
    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        return eir.create_grn(payload={"supplier_name": "Acme", "items": [{"item_id": 1, **line}]},
                              db=db, current_user=USER)
    finally:
        tenancy.reset_current_tenant(tok)


def _accept(db, gid):
    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        return eir.accept_grn(gid, db=db, current_user=USER)
    finally:
        tenancy.reset_current_tenant(tok)


def _line(db, gid):
    return db.query(models.GRNItem).filter(models.GRNItem.grn_id == gid).one()


def _stock(db):
    db.expire_all()
    return db.query(models.InventoryItem).get(1).current_stock


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. WHAT THE FORM SENDS FOR A PARTIAL ACCEPTANCE")
    db = _session()
    # The form: no rejected input (always 0), the status dropdown at its default.
    g = _grn(db, received_qty=100, accepted_qty=80, rejected_qty=0, inspection_status="Accepted")
    line = _line(db, g["id"])
    check("the 20 not accepted are recorded as rejected", line.rejected_qty == 20, str(line.rejected_qty))
    check("...and the line reads Partial, not Accepted", line.inspection_status == "Partial",
          line.inspection_status)
    _accept(db, g["id"])
    check("accepting it puts the 80 accepted into stock", _stock(db) == 80, str(_stock(db)))
    db.close()

    section("2. A 'REJECTED' LABEL CANNOT SIT ON STOCK THAT WAS TAKEN IN")
    db = _session()
    g = _grn(db, received_qty=100, accepted_qty=100, rejected_qty=0, inspection_status="Rejected")
    line = _line(db, g["id"])
    check("100 of 100 accepted reads Accepted, whatever the dropdown said",
          (line.inspection_status, line.rejected_qty) == ("Accepted", 0),
          f"{line.inspection_status}, {line.rejected_qty}")
    db.close()
    db = _session()
    g = _grn(db, received_qty=50, accepted_qty=0)
    line = _line(db, g["id"])
    check("nothing accepted reads Rejected, with all 50 rejected",
          (line.inspection_status, line.rejected_qty) == ("Rejected", 50),
          f"{line.inspection_status}, {line.rejected_qty}")
    _accept(db, g["id"])
    check("...and accepting the GRN adds nothing to stock", _stock(db) == 0, str(_stock(db)))
    db.close()

    section("3. MORE ACCEPTED THAN ARRIVED IS REFUSED")
    db = _session()
    try:
        _grn(db, received_qty=10, accepted_qty=12)
        refused, detail = False, "stored"
    except HTTPException as e:
        refused, detail = e.status_code == 400, str(e.detail)
    check("accepted 12 of 10 received is a 400", refused, detail)
    check("...that names both numbers", "12" in detail and "10" in detail, detail)
    check("...and nothing was written",
          db.query(models.GoodsReceiptNote).count() == 0 and db.query(models.GRNItem).count() == 0)
    db.close()

    section("4. A CLIENT'S REJECTED COUNT IS NOT BELIEVED OVER THE QUANTITIES")
    db = _session()
    g = _grn(db, received_qty=10, accepted_qty=8, rejected_qty=5)
    line = _line(db, g["id"])
    check("received 10, accepted 8 records 2 rejected, not the 5 sent", line.rejected_qty == 2,
          str(line.rejected_qty))
    db.close()

    section("5. EVERY STORED LINE ADDS UP, WITH A STATUS THAT MATCHES")
    db = _session()
    for received, accepted in ((10, 10), (10, 0), (10, 3), (0, 0)):
        _grn(db, received_qty=received, accepted_qty=accepted, inspection_status="Accepted")
    bad = []
    for ln in db.query(models.GRNItem).all():
        expected = ("Accepted" if ln.accepted_qty == ln.received_qty
                    else "Rejected" if ln.accepted_qty == 0 else "Partial")
        if ln.accepted_qty + ln.rejected_qty != ln.received_qty or ln.inspection_status != expected:
            bad.append((ln.received_qty, ln.accepted_qty, ln.rejected_qty, ln.inspection_status))
    check("accepted + rejected = received, and the status is the quantities'", not bad, str(bad))
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


def test_grn_quantities_decide_inspection():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
