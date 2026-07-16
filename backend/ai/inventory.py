"""Inventory summary — the supply-risk read-model (ADR-0007).

Answers "what am I about to run out of, and is anything being done about it?":
the items at or below their reorder level (worst coverage first), how many have
already run out, and the purchase orders the Reorder agent has drafted and left
awaiting approval. A read-model over inventory_items + purchase_orders —
auto-scoped to the tenant by the query layer (ADR-0002); it adds no storage.
"""
import models

name = "inventory"

TOP_N = 10


def _coverage(stock: int, level: int) -> int:
    """Stock as a percentage of the reorder level (100% = right at the line)."""
    return round(stock / level * 100) if level else 0


def build_inventory_summary(db, tenant: str) -> dict:
    """Items at/below reorder level (worst coverage first), the out-of-stock
    count, and the Reorder agent's drafted POs still awaiting approval.
    inventory_items and purchase_orders are auto-scoped (ADR-0002)."""
    from ai.agents import AUTO_PO_PREFIX  # lazy: avoids an import cycle at package load

    items = db.query(models.InventoryItem).all()
    # "At risk" = has a reorder policy and has hit it. Items with no reorder
    # level set aren't a replenishment signal, so they're left out.
    at_risk = [
        i for i in items
        if (i.reorder_level or 0) > 0 and (i.current_stock or 0) <= i.reorder_level
    ]
    at_risk.sort(key=lambda i: ((i.current_stock or 0) / i.reorder_level, i.current_stock or 0))

    rows = [{
        "item_code": i.item_code,
        "item_name": i.item_name,
        "current_stock": i.current_stock or 0,
        "reorder_level": i.reorder_level or 0,
        "unit": i.unit,
        "supplier": i.supplier,
        "coverage": _coverage(i.current_stock or 0, i.reorder_level or 0),
        "out_of_stock": (i.current_stock or 0) == 0,
    } for i in at_risk[:TOP_N]]

    pending = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.po_no.like(f"{AUTO_PO_PREFIX}-%"),
                models.PurchaseOrder.status == "Draft")
        .order_by(models.PurchaseOrder.id.desc())
        .all()
    )
    auto_pos = [{
        "po_no": p.po_no,
        "item_name": p.item_name,
        "order_quantity": p.order_quantity,
        "unit": p.unit,
        "expected_delivery_date": p.expected_delivery_date.isoformat() if p.expected_delivery_date else None,
    } for p in pending]

    return {
        "total_items": len(items),
        "at_risk": len(at_risk),
        "out_of_stock": sum(1 for i in at_risk if (i.current_stock or 0) == 0),
        "items": rows,
        "auto_pos_pending": len(auto_pos),
        "auto_pos": auto_pos,
    }
