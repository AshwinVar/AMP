"""Domain event subscribers (ADR-0001).

Reactions that used to live inline in HTTP handlers now live here as
independent, testable handlers wired to domain events. Each handler receives
the event and the caller's DB session and operates within its transaction.
"""
import bom
import models
from events import ProductionCompleted, InventoryLow, event_bus


def move_bom_on_production_completed(event: ProductionCompleted, db) -> None:
    """Consume components and receive the finished good per the tenant's own BOM.

    The recipe now comes from the database, scoped to the tenant that raised the
    work order (ADR-0013). It used to come from a module-level dict shared by
    every customer, which meant one company's rates were applied to another
    company's stock, and a product that company had defined itself moved nothing
    at all.
    """
    # The tenant comes from the EVENT, not from the ambient context. The event
    # already carries it (events.ProductionCompleted.tenant_code) because a
    # subscriber may run outside the request that produced it, and a recipe
    # resolved through an ambient binding rather than a stated one is exactly
    # the shape of the ingest defect in ADR-0011.
    recipe = bom.resolve(db, event.tenant_code, event.part_number)
    if recipe is None:
        return

    # Guard the completed quantity to a non-negative number before it drives any
    # physical inventory movement — this handler is the single choke-point every
    # ProductionCompleted event flows through (ADR-0001), so the guard belongs
    # here rather than only at each producer. The completion path
    # (work_orders_routes) falls back to the order's target_quantity when
    # actual_quantity is NULL, and target_quantity — nullable=False in the model,
    # but that constraint is not retro-applied to a raw-SQL / migration / cleared
    # row (the same legacy-row reality the stock coalesces below defend) — can
    # itself be NULL or, on a row that predates the create/PATCH non-negative
    # guards, negative. Left unguarded:
    #   * a NULL quantity made `qty * quantity_per_unit` raise TypeError and,
    #     because a subscriber error propagates by design (events.py), 500-ed the
    #     whole completion write — the same NULL-count-500 class fixed for the
    #     OEE record (#447) and the predictive scorer (#428);
    #   * a NEGATIVE quantity produced a negative `consume`, so `current_stock -
    #     consume` INCREASED raw stock and `stock + qty` DECREASED finished goods,
    #     writing negative-quantity Issue/Receive ledger rows — inventory movement
    #     the production data can't support (ADR-0010: never invent a number).
    # A missing or physically-impossible completed quantity means no measured
    # output, so it moves 0 — identical to the honest-zero path a genuine recorded
    # actual of 0 already takes (work_orders_routes).
    qty = max(event.quantity or 0, 0)

    # ---- consume every component, at this tenant's own rate ------------------
    for component_code, per_unit, _unit in bom.components_of(recipe):
        if not component_code or (per_unit or 0) <= 0:
            # A line with no code, or a zero/negative rate, describes no
            # movement. Skipped rather than written as a zero-quantity ledger
            # row, which would be a transaction that records nothing happening.
            continue
        # Scoped by tenant EXPLICITLY. The ADR-0002 hook would scope this too
        # inside a request, but this handler must be correct as a unit — see the
        # module docstring in bom.py.
        raw_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.tenant_code == event.tenant_code,
            models.InventoryItem.item_code == component_code,
        ).first()
        if raw_item is None:
            # The recipe names an item this tenant does not stock. Nothing to
            # deduct; the BOM validation on write is what stops this being
            # created, and a stale reference must not 500 a completion.
            continue

        # current_stock / reorder_level are nullable Integer columns
        # (Column(Integer, default=0) WITHOUT nullable=False — the default only
        # fills a value the *inserter* omitted, so a raw-SQL / migration /
        # cleared-field write can legitimately store NULL). This handler runs
        # synchronously inside the work-order-completion transaction and a
        # subscriber error propagates by design (events.py), so `None > int`,
        # `min(int, None)` and `None - int` would raise TypeError and 500 the
        # completion write. Coalesce a missing stock to 0 (an empty shelf can
        # issue nothing).
        current_stock = raw_item.current_stock or 0
        reorder_level = raw_item.reorder_level or 0
        was_above = current_stock > reorder_level
        consume = min(qty * per_unit, current_stock)
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

    # ---- receive the finished good ------------------------------------------
    if recipe.output_item_code:
        fg_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.tenant_code == event.tenant_code,
            models.InventoryItem.item_code == recipe.output_item_code,
        ).first()
        if fg_item:
            # Same nullable-column guard as the component side: a NULL
            # finished-goods stock must not TypeError-500 the completion write.
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
