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
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
import stock_events
from auth import get_current_user, require_roles
from payload_fields import MAX_QTY
from database import SessionLocal
import ai.escalations


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get("/items", response_model=List[schemas.InventoryItemResponse])
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


def check_stock_levels(current_stock, reorder_level):
    """THE bound on an item's stock and reorder level for a JSON write: whole
    numbers in [0, MAX_QTY], or a 400. Create and PATCH both call it; the CSV
    importers apply the same bound per cell through payload_fields.int_cell.

    A NEGATIVE value is physically impossible and corrupts the low-stock
    read-model: generate_low_stock_escalations would label a -5 item "Medium"
    (current_stock == 0 is false) and print "Current stock -5". Past MAX_QTY is a
    typo, and on PostgreSQL an overflow of the INTEGER column, not a stock.
    (test_inventory_stock_bounds.py)"""
    if min(current_stock, reorder_level) < 0:
        raise HTTPException(status_code=400, detail="current_stock and reorder_level must be non-negative")
    if max(current_stock, reorder_level) > MAX_QTY:
        raise HTTPException(status_code=400, detail="current_stock and reorder_level must be at most "
                                                    f"{MAX_QTY:,}")


@router.post("/items", response_model=schemas.InventoryItemResponse)
def create_inventory_item(
    item: schemas.InventoryItemCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    check_stock_levels(item.current_stock, item.reorder_level)
    existing = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.item_code == item.item_code)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Item code already exists")

    new_item = models.InventoryItem(**item.model_dump())
    db.add(new_item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Item code is already in use")
    db.refresh(new_item)

    return new_item


@router.patch("/items/{item_id}", response_model=schemas.InventoryItemResponse)
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
    before = item.current_stock

    for key, value in data.items():
        setattr(item, key, value)

    # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
    # nullable=False, and InventoryItemUpdate types each Optional[int]=None — so a
    # client can PATCH an explicit `{"current_stock": null}` (exclude_unset keeps an
    # explicit null) and a legacy / raw-SQL / migration row can already carry NULL.
    # Left as None, the response 500s because InventoryItemResponse types these
    # fields as non-optional int (ResponseValidationError int <- None). A NULL
    # stock/level is the column's own default of 0 — coalesce before the return, the
    # same heal the sibling create_inventory_transaction (and the orders #323 /
    # quality #324 PATCH responses) already apply; this also heals a pre-existing
    # NULL row touched by an unrelated field.
    item.current_stock = item.current_stock or 0
    item.reorder_level = item.reorder_level or 0

    # A NULL heals to 0 above; a NEGATIVE patch value is refused (check_stock_levels).
    check_stock_levels(item.current_stock, item.reorder_level)
    stock_events.stock_dropped(db, item, before)

    db.commit()
    db.refresh(item)

    return item


@router.delete("/items/{item_id}")
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


@router.get("/transactions", response_model=List[schemas.InventoryTransactionResponse])
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


@router.post("/transactions", response_model=schemas.InventoryTransactionResponse)
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

    # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
    # nullable=False, so a row written by raw SQL / a migration / a cleared update
    # can hold a true NULL. This ledger endpoint then did `None > reorder_level`,
    # `None < quantity` and `None -= quantity` and raised TypeError -> an unhandled
    # 500 on any transaction against such an item — the same NULL that
    # recommendations_routes and the low-stock generator already guard for these
    # exact columns. A transaction must apply to a CONCRETE base, so coalesce the
    # stock to the column's own default of 0 before the arithmetic; the ledger
    # write then stores a real integer (the NULL is healed, not propagated).
    current_stock = item.current_stock or 0

    quantity = abs(transaction.quantity)

    if transaction.transaction_type == "Issue":
        if current_stock < quantity:
            raise HTTPException(status_code=400, detail="Insufficient stock")
        item.current_stock = current_stock - quantity

    elif transaction.transaction_type == "Return":
        item.current_stock = current_stock + quantity

    elif transaction.transaction_type == "Receive":
        item.current_stock = current_stock + quantity

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

    # Tell the Reorder agent if this took the item across its reorder level
    # (ADR-0005): the one rule every stock writer calls. `current_stock` is the
    # value before this transaction.
    stock_events.stock_dropped(db, item, current_stock)

    db.commit()
    db.refresh(new_transaction)

    return new_transaction


@router.post("/generate-low-stock-escalations")
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

        # Dedup against an already-open escalation for this item. status is
        # Column(String, default="Open") WITHOUT nullable=False, so a raw-SQL /
        # migration / cleared-field row can hold a genuine NULL, and SQL's bare
        # `status != 'Resolved'` is NULL — not TRUE — for it, so the dedup MISSED a
        # NULL-status open escalation and raised a DUPLICATE. A NULL status is not a
        # terminal state (still open) — the same convention every open-escalation
        # READER already applies (analytics /escalations #295, system-notifications
        # #403, tenant-activity, the maintenance/document generators' own overdue
        # selection) — so OR the NULL in and treat it as open: it blocks the
        # duplicate instead of inflating the very open-escalation count the readers
        # report. A Resolved escalation still lets a fresh recurrence through.
        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                # Open = not terminal, by the one rule (ai.escalations.open_clause).
                # `!= "Resolved"` counted a CANCELLED escalation as open, so withdrawing
                # one silenced this alert permanently (test_open_escalation_one_rule, s.7).
                # NULL stays open, as #295/#403 required: open_clause COALESCEs it.
                ai.escalations.open_clause(),
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
