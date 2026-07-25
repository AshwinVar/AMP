"""Enterprise-inventory state guards: mutations move stock at most once.

accept_grn adds each line's accepted_qty to stock; approve->issue deducts stock.
Both must be guarded by the record's status, or a repeat call silently inflates
(GRN) or double-deducts (issue slip) inventory.

Run:  python backend/test_enterprise_inventory_state.py
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import enterprise_inventory_routes as eir
from database import Base

_ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _stock(db, item_id=1):
    return db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first().current_stock


def test_accept_grn_is_idempotent_no_double_stock():
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=0))
    db.add(models.GoodsReceiptNote(id=1, grn_no="GRN-1", supplier_name="Acme",
                                   received_by="rx", status="Draft"))
    db.add(models.GRNItem(grn_id=1, item_id=1, received_qty=10, accepted_qty=10))
    db.commit()

    r = eir.accept_grn(1, db=db, current_user=_ADMIN)
    assert r["status"] == "Accepted"
    assert _stock(db) == 10

    try:
        eir.accept_grn(1, db=db, current_user=_ADMIN)          # already processed
        assert False, "re-accepting a processed GRN should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db) == 10                                     # NOT 20
    assert db.query(models.InventoryTransaction).count() == 1   # one Receive, not two
    print("PASS accept_grn is idempotent — stock added once, not per call")


def test_issued_slip_cannot_be_re_approved_and_re_issued():
    db = _sess()
    db.add(models.InventoryItem(id=1, item_code="IC-1", item_name="Widget", category="Raw",
                                unit="pc", current_stock=100))
    db.add(models.MaterialIssueSlip(id=1, slip_no="MIS-1", item_id=1, requested_qty=10,
                                    requested_by="op", status="Pending"))
    db.commit()

    eir.approve_issue_slip(1, db=db, current_user=_ADMIN)
    eir.issue_slip(1, db=db, current_user=_ADMIN)
    assert _stock(db) == 90                                     # issued once

    try:
        eir.approve_issue_slip(1, db=db, current_user=_ADMIN)  # Issued -> cannot re-approve
        assert False, "re-approving an Issued slip should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db) == 90                                     # NOT 80
    print("PASS an Issued slip can't be re-approved -> no double stock deduction")


if __name__ == "__main__":
    test_accept_grn_is_idempotent_no_double_stock()
    test_issued_slip_cannot_be_re_approved_and_re_issued()
    print("ENTERPRISE-INVENTORY STATE OK: GRN accept + slip approve move stock at most once")
