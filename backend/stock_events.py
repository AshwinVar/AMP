"""The one rule for when a change of stock tells the Reorder agent (ADR-0005).

InventoryLow is what the Reorder agent proposes a purchase order from. Every write
of an item's current_stock calls stock_dropped after setting it, so the agent
hears about an item going low however it went low: the ledger, a work order's BOM
consumption, an issue slip, a cycle count, a corrected receipt, a PATCH or a CSV
import. Before this module only the first two told it, each with its own copy of
the rule (test_inventory_low_every_drop.py).
"""
from events import InventoryLow, event_bus


def stock_dropped(db, item, before):
    """Publish InventoryLow if this write took `item` from above its reorder level
    to at or below it. Call after item.current_stock holds the new value; `before`
    is the stock this write started from. Returns whether it published.

    A CROSSING, not a level: an item already below its level does not re-announce
    on every issue, or the Reorder agent would propose a purchase order per issue
    slip. An item with no reorder level (a NULL; the column default is 0)
    announces nothing: `stock <= None` says nothing about being low, and the
    low-stock read model's SQL `current_stock <= reorder_level` skips it too.
    """
    level = item.reorder_level
    if level is None:
        return False
    after = item.current_stock or 0
    if (before or 0) > level >= after:
        event_bus.publish(InventoryLow(
            tenant_code=item.tenant_code,
            item_id=item.id,
            item_code=item.item_code,
            item_name=item.item_name,
            current_stock=after,
            reorder_level=level,
        ), db)
        return True
    return False
