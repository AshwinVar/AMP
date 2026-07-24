"""Inventory summary — the supply-risk read-model (ADR-0007).

Answers "what am I about to run out of, and is anything being done about it?":
the items at or below their reorder level (worst coverage first), how many have
already run out, and the purchase orders the Reorder agent has drafted and left
awaiting approval. A read-model over inventory_items + purchase_orders —
auto-scoped to the tenant by the query layer (ADR-0002); it adds no storage.

"At risk" is the reorder chase list: an item with a reorder policy that has hit
it, PLUS any item whose shelf is empty — an empty shelf is the strongest
replenishment signal there is, and ``reorder_level`` defaults to 0, so an item
that was never given a reorder level would otherwise vanish from the count the
moment it ran out. ``out_of_stock`` is therefore a subset of ``at_risk`` (they
reconcile), and every out-of-stock item surfaces at the top of the chase list.
"""
import models

name = "inventory"

TOP_N = 10


def _coverage(stock: int, level: int) -> int:
    """Stock as a percentage of the reorder level (100% = right at the line).
    An item with no reorder level (0) has no line to measure against, so it
    reads 0% — which for an empty item is honest, and for a stocked one just
    means 'uncovered by policy'."""
    return round(stock / level * 100) if level else 0


def _is_out(item) -> bool:
    """The shelf is empty — a hard fact independent of reorder policy. ``<= 0``
    so oversold/negative stock still reads as out, and a NULL stock as 0."""
    return (item.current_stock or 0) <= 0


def _cover_ratio(item) -> float:
    """Sort key: stock as a fraction of the reorder level, lowest (worst) first.
    Guarded against a 0/NULL reorder level (which used to divide-by-zero the
    moment an empty no-policy item entered the list) — no policy sorts as 0.0,
    i.e. as urgent as a fully depleted item."""
    stock = item.current_stock or 0
    level = item.reorder_level or 0
    return stock / level if level > 0 else 0.0


def build_inventory_summary(db, tenant: str) -> dict:
    """Items at/below reorder level (worst coverage first), the out-of-stock
    count, and the Reorder agent's drafted POs still awaiting approval.
    inventory_items and purchase_orders are auto-scoped (ADR-0002)."""
    from ai.agents import AUTO_PO_PREFIX  # lazy: avoids an import cycle at package load

    items = db.query(models.InventoryItem).all()
    # "At risk" = an empty shelf (out of stock, whatever the policy), OR a set
    # reorder policy that's been hit. Empty items are pulled in explicitly
    # because reorder_level defaults to 0: without this an item that was never
    # given a reorder level silently drops out of the count the moment it runs
    # dry — the exact case the out-of-stock alert most needs to see.
    at_risk = [
        i for i in items
        if _is_out(i) or ((i.reorder_level or 0) > 0 and (i.current_stock or 0) <= i.reorder_level)
    ]
    # Worst coverage first; item_code breaks ties so the order is deterministic
    # (several empty items all share ratio 0.0).
    at_risk.sort(key=lambda i: (_cover_ratio(i), i.current_stock or 0, i.item_code or ""))

    rows = [{
        "item_code": i.item_code,
        "item_name": i.item_name,
        "current_stock": i.current_stock or 0,
        "reorder_level": i.reorder_level or 0,
        "unit": i.unit,
        "supplier": i.supplier,
        "coverage": _coverage(i.current_stock or 0, i.reorder_level or 0),
        "out_of_stock": _is_out(i),
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
        # A subset of at_risk (empty items are all in at_risk), so the two
        # reconcile and the chase list above always shows the out-of-stock ones.
        "out_of_stock": sum(1 for i in at_risk if _is_out(i)),
        "items": rows,
        "auto_pos_pending": len(auto_pos),
        "auto_pos": auto_pos,
    }
