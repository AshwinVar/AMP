"""Domain event subscribers (ADR-0001).

Reactions that used to live inline in HTTP handlers now live here as
independent, testable handlers wired to domain events. Each handler receives
the event and the caller's DB session and operates within its transaction.
"""
import models
from bom import PART_BOM
from events import ProductionCompleted, InventoryLow, event_bus


def move_bom_on_production_completed(event: ProductionCompleted, db) -> None:
    """Consume raw material and receive finished goods per the bill of materials.

    Moved verbatim from the work-order handler — behaviour is identical, it is
    just triggered by an event now instead of being hardcoded in the endpoint.
    """
    bom = PART_BOM.get(event.part_number)
    if not bom:
        return
    qty = event.quantity

    # Deduct raw material
    if bom["raw"]:
        raw_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.item_code == bom["raw"]
        ).first()
        if raw_item:
            # current_stock / reorder_level are nullable Integer columns
            # (Column(Integer, default=0) WITHOUT nullable=False — the default only
            # fills a value the *inserter* omitted, so a raw-SQL / migration /
            # cleared-field write can legitimately store NULL). This handler runs
            # synchronously inside the work-order-completion transaction and a
            # subscriber error propagates by design (events.py), so `None > int`,
            # `min(int, None)` and `None - int` would raise TypeError and 500 the
            # completion write. Coalesce a missing stock to 0 (an empty shelf can
            # issue nothing) — the same guard already applied to these very columns
            # elsewhere (ai_copilot, inventory_routes, recommendations_routes).
            current_stock = raw_item.current_stock or 0
            reorder_level = raw_item.reorder_level or 0
            was_above = current_stock > reorder_level
            consume = min(qty * bom["consume_per_unit"], current_stock)
            raw_item.current_stock = current_stock - consume
            db.add(models.InventoryTransaction(
                item_id=raw_item.id,
                transaction_type="Issue",
                quantity=consume,
                reference=event.work_order_no,
                notes=f"Auto-issued for WO {event.work_order_no} — {event.part_number}",
            ))
            # Production consumption can trip a reorder — emit InventoryLow so the
            # Reorder agent reacts (ADR-0005).
            if was_above and raw_item.current_stock <= reorder_level:
                event_bus.publish(InventoryLow(
                    tenant_code=event.tenant_code,
                    item_id=raw_item.id,
                    item_code=raw_item.item_code,
                    item_name=raw_item.item_name,
                    current_stock=raw_item.current_stock,
                    reorder_level=reorder_level,
                ), db)

    # Add finished goods
    if bom["fg"]:
        fg_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.item_code == bom["fg"]
        ).first()
        if fg_item:
            # Same nullable-column guard as the raw side: a NULL finished-goods
            # stock must not TypeError-500 the completion write.
            fg_item.current_stock = (fg_item.current_stock or 0) + qty
            db.add(models.InventoryTransaction(
                item_id=fg_item.id,
                transaction_type="Receive",
                quantity=qty,
                reference=event.work_order_no,
                notes=f"Auto-received from WO {event.work_order_no} completion",
            ))


def register(bus=event_bus) -> None:
    """Wire all subscribers to the bus. Called once at startup."""
    bus.subscribe(ProductionCompleted, move_bom_on_production_completed)
