"""Inventory routes — stock items and the transaction ledger.

Items (list / create / update / delete) and the transaction ledger
(list / record). One behaviour preserved exactly: recording a transaction that
drops an item to/through its reorder point publishes an InventoryLow domain
event (ADR-0001/0003) on the request DB session — subscribers react and commit
atomically. Also exposes the low-stock escalation generator (builds
models.Escalation rows directly; self-contained). Peeled out of main.py per
ADR-0009.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal
from events import event_bus, InventoryLow
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter()


@router.get("/inventory/items", response_model=List[schemas.InventoryItemResponse])
def get_inventory_items(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.InventoryItem)
        .order_by(models.InventoryItem.id.desc())
        .limit(500)
        .all()
    )


@router.post("/inventory/items", response_model=schemas.InventoryItemResponse)
def create_inventory_item(
    item: schemas.InventoryItemCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.item_code == item.item_code)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Item code already exists")

    new_item = models.InventoryItem(**item.model_dump())
    db.add(new_item)
    db.commit()
    db.refresh(new_item)

    return new_item


@router.patch("/inventory/items/{item_id}", response_model=schemas.InventoryItemResponse)
def update_inventory_item(
    item_id: int,
    payload: schemas.InventoryItemUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)

    return item


@router.delete("/inventory/items/{item_id}")
def delete_inventory_item(
    item_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    db.delete(item)
    db.commit()

    return {"message": "Inventory item deleted successfully"}


@router.get("/inventory/transactions", response_model=List[schemas.InventoryTransactionResponse])
def get_inventory_transactions(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.InventoryTransaction)
        .order_by(models.InventoryTransaction.id.desc())
        .limit(300)
        .all()
    )


@router.post("/inventory/transactions", response_model=schemas.InventoryTransactionResponse)
def create_inventory_transaction(
    transaction: schemas.InventoryTransactionCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == transaction.item_id)
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    # Whether the item was healthy *before* this transaction, so InventoryLow is
    # raised only on the crossing into low stock (ADR-0003), not on every issue.
    was_above_reorder = item.current_stock > item.reorder_level

    quantity = abs(transaction.quantity)

    if transaction.transaction_type == "Issue":
        if item.current_stock < quantity:
            raise HTTPException(status_code=400, detail="Insufficient stock")
        item.current_stock -= quantity

    elif transaction.transaction_type == "Return":
        item.current_stock += quantity

    elif transaction.transaction_type == "Receive":
        item.current_stock += quantity

    elif transaction.transaction_type == "Adjust":
        item.current_stock = quantity

    else:
        raise HTTPException(status_code=400, detail="Invalid transaction type")

    new_transaction = models.InventoryTransaction(
        item_id=transaction.item_id,
        transaction_type=transaction.transaction_type,
        quantity=quantity,
        reference=transaction.reference,
        notes=transaction.notes,
    )

    db.add(new_transaction)

    # Widen the event stream: signal when stock crosses its reorder level so the
    # AI platform can react with a reorder recommendation (ADR-0003).
    if was_above_reorder and item.current_stock <= item.reorder_level:
        event_bus.publish(InventoryLow(
            tenant_code=request_tenant(current_user),
            item_id=item.id,
            item_code=item.item_code,
            item_name=item.item_name,
            current_stock=item.current_stock,
            reorder_level=item.reorder_level,
        ), db)

    db.commit()
    db.refresh(new_transaction)

    return new_transaction


@router.post("/inventory/generate-low-stock-escalations")
def generate_low_stock_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    low_stock_items = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.current_stock <= models.InventoryItem.reorder_level)
        .all()
    )

    created = 0

    for item in low_stock_items:
        title = f"Low stock: {item.item_code} - {item.item_name}"

        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                models.Escalation.status != "Resolved",
            )
            .first()
        )

        if existing:
            continue

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="High" if item.current_stock == 0 else "Medium",
            owner="Stores",
            department="Inventory",
            status="Open",
            source="Inventory",
            notes=f"Current stock {item.current_stock} {item.unit}; reorder level {item.reorder_level} {item.unit}",
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}
