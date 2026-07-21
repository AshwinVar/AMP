"""Orders & procurement routes — sales orders, suppliers, purchase orders.

The order-to-procurement domain: customer orders (CRUD + CSV export + analytics
+ late-order escalation generation), suppliers (CRUD), and purchase orders
(CRUD + analytics + overdue-escalation generation; a received-qty bump auto-adds
an InventoryTransaction). Peeled out of main.py per ADR-0009. Plain CRUD with no
event-bus coupling. `_orders_csv` is module-level (unit-tested by name in
test_orders_export.py).
"""
import csv
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_roles
from database import SessionLocal


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _orders_csv(db) -> str:
    """The tenant's order book as CSV text (auto-scoped, ADR-0002)."""
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order No", "Customer", "Product", "Order Qty", "Dispatched Qty",
                "Status", "Priority", "Due Date"])
    for o in db.query(models.CustomerOrder).order_by(models.CustomerOrder.due_date).all():
        w.writerow([o.order_no, o.customer_name, o.product_name, o.order_quantity or 0,
                    o.dispatched_quantity or 0, o.status, o.priority or "",
                    o.due_date.isoformat() if o.due_date else ""])
    return buf.getvalue()


router = APIRouter()


@router.get("/customer-orders/export")
def export_customer_orders(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Export the tenant's order book as CSV — SME manufacturers live in Excel.
    from fastapi.responses import Response
    return Response(content=_orders_csv(db), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=order-book.csv"})


@router.get("/customer-orders", response_model=List[schemas.CustomerOrderResponse])
def get_customer_orders(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.CustomerOrder)
        .order_by(models.CustomerOrder.id.desc())
        .limit(500)
        .all()
    )


@router.post("/customer-orders", response_model=schemas.CustomerOrderResponse)
def create_customer_order(
    order: schemas.CustomerOrderCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.order_no == order.order_no)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Order number already exists")

    if order.linked_work_order_id:
        work_order = (
            db.query(models.WorkOrder)
            .filter(models.WorkOrder.id == order.linked_work_order_id)
            .first()
        )
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if order.linked_production_plan_id:
        plan = (
            db.query(models.ProductionPlan)
            .filter(models.ProductionPlan.id == order.linked_production_plan_id)
            .first()
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Production plan not found")

    if order.dispatched_quantity > order.order_quantity:
        raise HTTPException(status_code=400, detail="Dispatched quantity cannot exceed order quantity")

    status = order.status
    if order.dispatched_quantity >= order.order_quantity:
        status = "Dispatched"
    elif order.dispatched_quantity > 0:
        status = "Partial"

    new_order = models.CustomerOrder(**order.model_dump())
    new_order.status = status

    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    return new_order


@router.patch("/customer-orders/{order_id}", response_model=schemas.CustomerOrderResponse)
def update_customer_order(
    order_id: int,
    payload: schemas.CustomerOrderUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    order = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.id == order_id)
        .first()
    )

    if not order:
        raise HTTPException(status_code=404, detail="Customer order not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(order, key, value)

    if order.dispatched_quantity > order.order_quantity:
        raise HTTPException(status_code=400, detail="Dispatched quantity cannot exceed order quantity")

    if order.dispatched_quantity >= order.order_quantity:
        order.status = "Dispatched"
    elif order.dispatched_quantity > 0 and order.status != "Cancelled":
        order.status = "Partial"

    db.commit()
    db.refresh(order)

    return order


@router.delete("/customer-orders/{order_id}")
def delete_customer_order(
    order_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    order = (
        db.query(models.CustomerOrder)
        .filter(models.CustomerOrder.id == order_id)
        .first()
    )

    if not order:
        raise HTTPException(status_code=404, detail="Customer order not found")

    db.delete(order)
    db.commit()

    return {"message": "Customer order deleted successfully"}


@router.get("/analytics/customer-orders")
def get_customer_order_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    orders = db.query(models.CustomerOrder).all()

    today = datetime.utcnow().date()

    pending = len([row for row in orders if row.status == "Pending"])
    partial = len([row for row in orders if row.status == "Partial"])
    dispatched = len([row for row in orders if row.status == "Dispatched"])
    cancelled = len([row for row in orders if row.status == "Cancelled"])
    late = len([row for row in orders if row.due_date < today and row.status not in ["Dispatched", "Cancelled"]])

    total_order_qty = sum(row.order_quantity for row in orders)
    total_dispatched_qty = sum(row.dispatched_quantity for row in orders)
    dispatch_rate = round((total_dispatched_qty / total_order_qty) * 100) if total_order_qty else 0

    priority_counts = {}
    customer_counts = {}

    for row in orders:
        priority_counts[row.priority] = priority_counts.get(row.priority, 0) + 1
        customer_counts[row.customer_name] = customer_counts.get(row.customer_name, 0) + row.order_quantity

    return {
        "total_orders": len(orders),
        "pending": pending,
        "partial": partial,
        "dispatched": dispatched,
        "cancelled": cancelled,
        "late": late,
        "total_order_qty": total_order_qty,
        "total_dispatched_qty": total_dispatched_qty,
        "dispatch_rate": dispatch_rate,
        "priority_counts": priority_counts,
        "customer_counts": customer_counts,
    }


@router.post("/customer-orders/generate-late-order-escalations")
def generate_late_order_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()

    late_orders = (
        db.query(models.CustomerOrder)
        .filter(
            models.CustomerOrder.due_date < today,
            models.CustomerOrder.status.notin_(["Dispatched", "Cancelled"]),
        )
        .all()
    )

    created = 0

    for order in late_orders:
        title = f"Late customer order: {order.order_no}"

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
            severity="Critical" if order.priority == "Critical" else "High",
            owner="Planning",
            department="Dispatch",
            status="Open",
            source="Orders",
            notes=(
                f"Customer {order.customer_name}; product {order.product_name}; "
                f"due {order.due_date}; dispatched {order.dispatched_quantity}/{order.order_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}


@router.get("/suppliers", response_model=List[schemas.SupplierResponse])
def get_suppliers(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.Supplier).order_by(models.Supplier.id.desc()).limit(500).all()


@router.post("/suppliers", response_model=schemas.SupplierResponse)
def create_supplier(
    supplier: schemas.SupplierCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.Supplier).filter(models.Supplier.supplier_code == supplier.supplier_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Supplier code already exists")

    new_supplier = models.Supplier(**supplier.model_dump())
    db.add(new_supplier)
    db.commit()
    db.refresh(new_supplier)
    return new_supplier


@router.patch("/suppliers/{supplier_id}", response_model=schemas.SupplierResponse)
def update_supplier(
    supplier_id: int,
    payload: schemas.SupplierUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, key, value)

    db.commit()
    db.refresh(supplier)
    return supplier


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(
    supplier_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    db.delete(supplier)
    db.commit()
    return {"message": "Supplier deleted successfully"}


@router.get("/purchase-orders", response_model=List[schemas.PurchaseOrderResponse])
def get_purchase_orders(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id.desc()).limit(500).all()


@router.post("/purchase-orders", response_model=schemas.PurchaseOrderResponse)
def create_purchase_order(
    po: schemas.PurchaseOrderCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.po_no == po.po_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="PO number already exists")

    supplier = db.query(models.Supplier).filter(models.Supplier.id == po.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    if po.item_id:
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == po.item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Inventory item not found")

    if po.received_quantity > po.order_quantity:
        raise HTTPException(status_code=400, detail="Received quantity cannot exceed order quantity")

    status = po.status
    if po.received_quantity >= po.order_quantity:
        status = "Received"
    elif po.received_quantity > 0:
        status = "Partial"

    new_po = models.PurchaseOrder(**po.model_dump())
    new_po.status = status

    db.add(new_po)
    db.commit()
    db.refresh(new_po)
    return new_po


@router.patch("/purchase-orders/{po_id}", response_model=schemas.PurchaseOrderResponse)
def update_purchase_order(
    po_id: int,
    payload: schemas.PurchaseOrderUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    old_received = po.received_quantity

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(po, key, value)

    if po.received_quantity > po.order_quantity:
        raise HTTPException(status_code=400, detail="Received quantity cannot exceed order quantity")

    if po.received_quantity >= po.order_quantity:
        po.status = "Received"
    elif po.received_quantity > 0 and po.status != "Cancelled":
        po.status = "Partial"

    received_delta = max(po.received_quantity - old_received, 0)

    if received_delta > 0 and po.item_id:
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == po.item_id).first()
        if item:
            item.current_stock += received_delta

            transaction = models.InventoryTransaction(
                item_id=item.id,
                transaction_type="Receive",
                quantity=received_delta,
                reference=po.po_no,
                notes="Auto stock receipt from purchase order",
            )
            db.add(transaction)

    db.commit()
    db.refresh(po)
    return po


@router.delete("/purchase-orders/{po_id}")
def delete_purchase_order(
    po_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    db.delete(po)
    db.commit()
    return {"message": "Purchase order deleted successfully"}


@router.get("/analytics/purchasing")
def get_purchasing_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    suppliers = db.query(models.Supplier).all()
    pos = db.query(models.PurchaseOrder).all()
    today = datetime.utcnow().date()

    open_count = len([row for row in pos if row.status == "Open"])
    partial = len([row for row in pos if row.status == "Partial"])
    received = len([row for row in pos if row.status == "Received"])
    cancelled = len([row for row in pos if row.status == "Cancelled"])
    overdue = len([row for row in pos if row.expected_delivery_date < today and row.status not in ["Received", "Cancelled"]])

    ordered_qty = sum(row.order_quantity for row in pos)
    received_qty = sum(row.received_quantity for row in pos)
    receipt_rate = round((received_qty / ordered_qty) * 100) if ordered_qty else 0

    supplier_pending = {}
    for row in pos:
        supplier = db.query(models.Supplier).filter(models.Supplier.id == row.supplier_id).first()
        name = supplier.supplier_name if supplier else f"Supplier {row.supplier_id}"
        pending = max(row.order_quantity - row.received_quantity, 0)
        supplier_pending[name] = supplier_pending.get(name, 0) + pending

    return {
        "suppliers": len(suppliers),
        "purchase_orders": len(pos),
        "open": open_count,
        "partial": partial,
        "received": received,
        "cancelled": cancelled,
        "overdue": overdue,
        "ordered_qty": ordered_qty,
        "received_qty": received_qty,
        "receipt_rate": receipt_rate,
        "supplier_pending": supplier_pending,
    }


@router.post("/purchase-orders/generate-overdue-escalations")
def generate_overdue_po_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()

    overdue_pos = (
        db.query(models.PurchaseOrder)
        .filter(
            models.PurchaseOrder.expected_delivery_date < today,
            models.PurchaseOrder.status.notin_(["Received", "Cancelled"]),
        )
        .all()
    )

    created = 0

    for po in overdue_pos:
        title = f"Overdue purchase order: {po.po_no}"

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

        supplier = db.query(models.Supplier).filter(models.Supplier.id == po.supplier_id).first()
        supplier_name = supplier.supplier_name if supplier else f"Supplier {po.supplier_id}"

        escalation = models.Escalation(
            machine_id=None,
            title=title,
            severity="High",
            owner="Purchasing",
            department="Supply Chain",
            status="Open",
            source="Purchasing",
            notes=(
                f"Supplier {supplier_name}; item {po.item_name}; "
                f"expected {po.expected_delivery_date}; received {po.received_quantity}/{po.order_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}
