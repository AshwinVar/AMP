"""Inventory summary read-model tests (ADR-0007).

Supply risk over inventory_items + purchase_orders: the items at/below their
reorder level (worst coverage first), the out-of-stock count, and the Reorder
agent's drafted POs still awaiting approval (AUTO-PO-*, status Draft).

Run:  python backend/test_inventory.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import inventory


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(code, stock, reorder, name=None):
    return models.InventoryItem(
        item_code=code, item_name=name or code, category="raw", unit="pcs",
        supplier="Acme", current_stock=stock, reorder_level=reorder)


def _auto_po(item_id, status="Draft"):
    return models.PurchaseOrder(
        po_no=f"AUTO-PO-{item_id}-123", item_id=item_id, item_name=f"item-{item_id}",
        order_quantity=50, unit="pcs", expected_delivery_date=date.today(), status=status)


def test_inventory_summary_flags_risk_and_agent_drafts():
    db = _fresh_session()
    db.add_all([
        _item("BOLT-01", stock=0, reorder=100),     # out of stock, at risk (0% coverage)
        _item("NUT-02", stock=40, reorder=100),      # at risk (40% coverage)
        _item("WIRE-03", stock=90, reorder=100),     # at risk (90% coverage)
        _item("OIL-04", stock=500, reorder=100),     # healthy -> not at risk
        _item("MISC-05", stock=0, reorder=0),        # no reorder policy -> not a signal
    ])
    # the Reorder agent's drafts (AUTO-PO), plus noise that must not count
    db.add_all([
        _auto_po(1, "Draft"),                        # pending agent draft
        _auto_po(2, "Draft"),                        # pending agent draft
        _auto_po(3, "Approved"),                     # already approved -> not pending
        models.PurchaseOrder(po_no="PO-9001", item_id=2, item_name="manual", order_quantity=10,
                             unit="pcs", expected_delivery_date=date.today(), status="Draft"),  # manual -> excluded
    ])
    db.commit()

    s = inventory.build_inventory_summary(db, "DEFAULT")
    assert s["total_items"] == 5
    assert s["at_risk"] == 3                          # BOLT/NUT/WIRE; OIL healthy, MISC no policy
    assert s["out_of_stock"] == 1                     # BOLT-01
    # worst coverage first: BOLT (0%) -> NUT (40%) -> WIRE (90%)
    assert [i["item_code"] for i in s["items"]] == ["BOLT-01", "NUT-02", "WIRE-03"]
    assert s["items"][0]["out_of_stock"] is True and s["items"][0]["coverage"] == 0
    assert s["items"][1]["coverage"] == 40
    # only AUTO-PO drafts awaiting approval are the agent's pending queue
    assert s["auto_pos_pending"] == 2
    assert all(p["po_no"].startswith("AUTO-PO-") for p in s["auto_pos"])

    # empty inventory -> zeros, no crash
    empty = inventory.build_inventory_summary(_fresh_session(), "DEFAULT")
    assert empty["total_items"] == 0 and empty["at_risk"] == 0 and empty["items"] == []
    assert empty["auto_pos_pending"] == 0


if __name__ == "__main__":
    test_inventory_summary_flags_risk_and_agent_drafts()
    print("INVENTORY OK: at-risk items (worst coverage first) + out-of-stock + Reorder-agent drafts; empty-safe")
