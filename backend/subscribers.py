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
            was_above = raw_item.current_stock > raw_item.reorder_level
            consume = min(qty * bom["consume_per_unit"], raw_item.current_stock)
            raw_item.current_stock -= consume
            db.add(models.InventoryTransaction(
                item_id=raw_item.id,
                transaction_type="Issue",
                quantity=consume,
                reference=event.work_order_no,
                notes=f"Auto-issued for WO {event.work_order_no} — {event.part_number}",
            ))
            # Production consumption can trip a reorder — emit InventoryLow so the
            # Reorder agent reacts (ADR-0005).
            if was_above and raw_item.current_stock <= raw_item.reorder_level:
                event_bus.publish(InventoryLow(
                    tenant_code=event.tenant_code,
                    item_id=raw_item.id,
                    item_code=raw_item.item_code,
                    item_name=raw_item.item_name,
                    current_stock=raw_item.current_stock,
                    reorder_level=raw_item.reorder_level,
                ), db)

    # Add finished goods
    if bom["fg"]:
        fg_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.item_code == bom["fg"]
        ).first()
        if fg_item:
            fg_item.current_stock += qty
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
