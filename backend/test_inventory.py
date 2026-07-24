"""Inventory summary read-model tests (ADR-0007).

Supply risk over inventory_items + purchase_orders: the items at/below their
reorder level (worst coverage first), the out-of-stock count, and the Reorder
agent's drafted POs still awaiting approval (AUTO-PO-*, status Draft).

Run:  python backend/test_inventory.py     (exit 0 = pass)
"""
from datetime import datetime

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
        order_quantity=50, unit="pcs", expected_delivery_date=datetime.utcnow().date(), status=status)


def test_inventory_summary_flags_risk_and_agent_drafts():
    db = _fresh_session()
    db.add_all([
        _item("BOLT-01", stock=0, reorder=100),      # out of stock, at risk (0% coverage)
        _item("NUT-02", stock=40, reorder=100),      # at risk (40% coverage)
        _item("WIRE-03", stock=90, reorder=100),     # at risk (90% coverage)
        _item("OIL-04", stock=500, reorder=100),     # healthy -> not at risk
        _item("MISC-05", stock=0, reorder=0),        # EMPTY with no reorder policy -> still out of stock
        _item("GEAR-06", stock=None, reorder=100),   # NULL stock reads as 0 -> out of stock, at risk
        _item("SPARE-07", stock=5, reorder=0),       # has stock, no policy -> not a signal
    ])
    # the Reorder agent's drafts (AUTO-PO), plus noise that must not count
    db.add_all([
        _auto_po(1, "Draft"),                        # pending agent draft
        _auto_po(2, "Draft"),                        # pending agent draft
        _auto_po(3, "Approved"),                     # already approved -> not pending
        models.PurchaseOrder(po_no="PO-9001", item_id=2, item_name="manual", order_quantity=10,
                             unit="pcs", expected_delivery_date=datetime.utcnow().date(), status="Draft"),  # manual -> excluded
    ])
    db.commit()

    s = inventory.build_inventory_summary(db, "DEFAULT")
    assert s["total_items"] == 7
    # at risk: 3 empty (BOLT, MISC, GEAR) + 2 under-policy (NUT, WIRE). OIL healthy;
    # SPARE has stock and no policy -> not a signal.
    assert s["at_risk"] == 5
    # out of stock counts EVERY empty shelf, not just the ones with a reorder
    # policy — MISC-05 (no policy) and GEAR-06 (NULL stock) used to be dropped.
    assert s["out_of_stock"] == 3
    # out_of_stock reconciles with at_risk: it's the empty subset, and (since
    # at_risk <= TOP_N here) every one shows in the chase list.
    assert s["out_of_stock"] == sum(1 for r in s["items"] if r["out_of_stock"])
    assert s["out_of_stock"] <= s["at_risk"]
    # worst coverage first, ties broken by item_code: the three empties (ratio 0)
    # in code order, then NUT (40%), then WIRE (90%).
    assert [i["item_code"] for i in s["items"]] == [
        "BOLT-01", "GEAR-06", "MISC-05", "NUT-02", "WIRE-03"]
    assert s["items"][0]["out_of_stock"] is True and s["items"][0]["coverage"] == 0
    # the no-policy empty item is present, honestly labelled out of stock at 0% cover
    misc = next(r for r in s["items"] if r["item_code"] == "MISC-05")
    assert misc["out_of_stock"] is True and misc["coverage"] == 0 and misc["reorder_level"] == 0
    # the under-policy row still reads its real coverage
    nut = next(r for r in s["items"] if r["item_code"] == "NUT-02")
    assert nut["coverage"] == 40 and nut["out_of_stock"] is False
    # only AUTO-PO drafts awaiting approval are the agent's pending queue
    assert s["auto_pos_pending"] == 2
    assert all(p["po_no"].startswith("AUTO-PO-") for p in s["auto_pos"])

    # empty inventory -> zeros, no crash
    empty = inventory.build_inventory_summary(_fresh_session(), "DEFAULT")
    assert empty["total_items"] == 0 and empty["at_risk"] == 0 and empty["items"] == []
    assert empty["out_of_stock"] == 0 and empty["auto_pos_pending"] == 0


if __name__ == "__main__":
    test_inventory_summary_flags_risk_and_agent_drafts()
    print("INVENTORY OK: at-risk items (worst coverage first) + out-of-stock counts every "
          "empty shelf (policy or not, NULL-safe) reconciled with at_risk; Reorder-agent drafts; empty-safe")
