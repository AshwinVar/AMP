"""Orders & procurement routes — sales orders, suppliers, purchase orders.

The order-to-procurement domain: customer orders (CRUD + CSV export + analytics
+ late-order escalation generation), suppliers (CRUD), and purchase orders
(CRUD + analytics + overdue-escalation generation; a received-qty bump auto-adds
an InventoryTransaction). Peeled out of main.py per ADR-0009. Plain CRUD with no
event-bus coupling. `_orders_csv` is module-level (unit-tested by name in
test_orders_export.py).
"""
import csv
import io
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from csv_safe import import_row_error, read_upload_text
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
    """The tenant's order book as CSV text (auto-scoped, ADR-0002).

    Customer and product names are user-typed, so this goes through csv_safe
    rather than csv.writer directly — see that module on formula injection."""
    from csv_safe import csv_text
    return csv_text(
        ["Order No", "Customer", "Product", "Order Qty", "Dispatched Qty",
         "Status", "Priority", "Due Date"],
        [[o.order_no, o.customer_name, o.product_name, o.order_quantity or 0,
          o.dispatched_quantity or 0, o.status, o.priority or "",
          o.due_date.isoformat() if o.due_date else ""]
         for o in db.query(models.CustomerOrder).order_by(models.CustomerOrder.due_date).all()])


router = APIRouter(tags=["Orders & Procurement"])


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

    # Reject physically-impossible rows at the boundary, the same guard the
    # production-record (#266) and quality-inspection (#324) ingests already
    # apply. order_quantity / dispatched_quantity are plain ints in the schema
    # (no ge=0), so nothing else stops a negative — and a negative dispatched
    # silently drives the dispatch-rate headline (dispatched/ordered) below 0,
    # a metric the data can't support. Checked BEFORE the over-dispatch invariant
    # so a negative order_quantity gets an honest message, not "cannot exceed".
    if min(order.order_quantity, order.dispatched_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

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
    # order_no is GLOBALLY unique but the pre-check above is tenant-scoped, so a
    # number another tenant already uses passes the check then trips the DB
    # constraint on commit. Return a clean 409 (and roll back the poisoned
    # session) instead of an unhandled IntegrityError -> 500.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Order number is already in use")
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

    # dispatched_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can hold a true NULL,
    # and CustomerOrderUpdate.dispatched_quantity is Optional (omitted -> not
    # applied). A PATCH that leaves the field alone on such a row then hit
    # `None > order_quantity` / `None >= order_quantity` and raised TypeError,
    # 500-ing the update — and even guarding the comparison, response_model
    # CustomerOrderResponse.dispatched_quantity is `int`, so returning a NULL would
    # just move the 500 to response serialisation. Materialise the column's own
    # default of 0 (exactly what /analytics/customer-orders already coalesces this
    # NULL to) so both the status logic and the response see a real int.
    if order.dispatched_quantity is None:
        order.dispatched_quantity = 0

    # Reject a negative dispatched patch (parity with create / the production-record
    # #266 and quality #324 ingests). A NULL heals to 0 above and is fine; a negative
    # (`{"dispatched_quantity": -5}`) is physically impossible and would corrupt the
    # dispatch-rate window exactly as on create — so a clean 400, not a stored
    # negative. order_quantity isn't settable via this schema, so it needs no guard.
    if order.dispatched_quantity < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

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
    # Aggregate the growing customer_orders table in SQL rather than hydrating
    # every row into Python just to bucket it with list comprehensions (rule-4
    # antipattern; the same GROUP BY fix already applied to /analytics/work-orders,
    # /analytics/production-plans and /analytics/escalations). CustomerOrder is in
    # SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes every
    # aggregate SELECT below exactly as it did the old .all() scan.
    today = datetime.utcnow().date()

    status_counts = dict(
        db.query(models.CustomerOrder.status, func.count())
        .group_by(models.CustomerOrder.status)
        .all()
    )

    # order_quantity is nullable=False, but SUM over an empty book is still NULL;
    # dispatched_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can be a true NULL,
    # which the old Python sum() turned into int + None -> TypeError. COALESCE(..,0)
    # makes both read as the column's own default of 0 (the CSV export already reads
    # dispatched_quantity as `or 0`).
    total_orders, total_order_qty, total_dispatched_qty = db.query(
        func.count(models.CustomerOrder.id),
        func.coalesce(func.sum(models.CustomerOrder.order_quantity), 0),
        func.coalesce(func.sum(models.CustomerOrder.dispatched_quantity), 0),
    ).one()
    total_order_qty = int(total_order_qty)
    total_dispatched_qty = int(total_dispatched_qty)
    dispatch_rate = round((total_dispatched_qty / total_order_qty) * 100) if total_order_qty else 0

    # Late = past due and not already dispatched/cancelled. status is String
    # default="Pending" (not NOT NULL); the old Python `row.status not in [...]`
    # counted a NULL-status overdue order as late (None is not in the list), but
    # SQL's `status NOT IN (...)` is NULL — not TRUE — for a NULL status, so it
    # would silently drop that row. OR the NULL back in to keep the same basis.
    late = db.query(func.count(models.CustomerOrder.id)).filter(
        models.CustomerOrder.due_date < today,
        or_(
            models.CustomerOrder.status.is_(None),
            models.CustomerOrder.status.notin_(["Dispatched", "Cancelled"]),
        ),
    ).scalar() or 0

    priority_counts = dict(
        db.query(models.CustomerOrder.priority, func.count())
        .group_by(models.CustomerOrder.priority)
        .all()
    )
    customer_counts = {
        name: int(qty)
        for name, qty in db.query(
            models.CustomerOrder.customer_name,
            func.coalesce(func.sum(models.CustomerOrder.order_quantity), 0),
        )
        .group_by(models.CustomerOrder.customer_name)
        .all()
    }

    return {
        "total_orders": total_orders,
        "pending": status_counts.get("Pending", 0),
        "partial": status_counts.get("Partial", 0),
        "dispatched": status_counts.get("Dispatched", 0),
        "cancelled": status_counts.get("Cancelled", 0),
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

    # "Late" here MUST mean exactly what the /analytics/customer-orders headline
    # counts as late, or the two disagree: the headline says N late while this
    # generator raises fewer than N escalations. status is String default="Pending"
    # (not NOT NULL), so a raw-SQL / migration / cleared write can store NULL, and
    # SQL's `status NOT IN (...)` is NULL — not TRUE — for a NULL status, which
    # silently drops an overdue NULL-status order from this scan even though the
    # analytics endpoint (#295) OR's the NULL back in and counts it as late. Mirror
    # that predicate exactly so the escalation basis reconciles with the headline.
    late_orders = (
        db.query(models.CustomerOrder)
        .filter(
            models.CustomerOrder.due_date < today,
            or_(
                models.CustomerOrder.status.is_(None),
                models.CustomerOrder.status.notin_(["Dispatched", "Cancelled"]),
            ),
        )
        .all()
    )

    created = 0

    for order in late_orders:
        title = f"Late customer order: {order.order_no}"

        # Dedup against an already-OPEN escalation with the same title. Escalation.status
        # is Column(String, default="Open") WITHOUT nullable=False, so a raw-SQL /
        # migration / cleared-field row can carry a genuine NULL. In SQL a bare
        # `status != 'Resolved'` is NULL — not TRUE — for such a row, so it did NOT find
        # a NULL-status open escalation and raised a DUPLICATE, inflating the very
        # open-escalation count every reader reports. OR the NULL back in so a NULL-status
        # open escalation blocks the duplicate; a Resolved one still lets a fresh
        # recurrence through. (Mirrors #425, which missed this generator.)
        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                or_(
                    models.Escalation.status.is_(None),
                    models.Escalation.status != "Resolved",
                ),
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
                # Coalesce a NULL dispatched_quantity to 0 for the DISPLAYED note.
                # dispatched_quantity is Column(Integer, default=0) WITHOUT
                # nullable=False, so a legacy / raw-SQL / cleared-field row can hold a
                # true NULL — and this generator's late basis selects on due_date/status
                # (never dispatched_quantity, see the reconciliation above), so a
                # NULL-dispatched Pending overdue order reaches here and the note read
                # "dispatched None/500". A raw None must never leak into a value a human
                # reads (rule-2); 0 is the column's own default and the exact coalesce
                # /analytics/customer-orders and the CSV export already apply to it.
                f"Customer {order.customer_name}; product {order.product_name}; "
                f"due {order.due_date}; dispatched {order.dispatched_quantity or 0}/{order.order_quantity}"
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
    try:
        db.commit()
    except IntegrityError:  # supplier_code globally unique; tenant-scoped pre-check
        db.rollback()
        raise HTTPException(status_code=409, detail="Supplier code is already in use")
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

    # Reject physically-impossible rows at the boundary (parity with the
    # production-record #266 and quality-inspection #324 ingests). order_quantity /
    # received_quantity are plain ints in the schema (no ge=0), so nothing else
    # stops a negative — and a negative received silently drives the receipt-rate
    # headline (received/ordered) below 0, a metric the data can't support. Checked
    # BEFORE the over-received invariant so a negative order_quantity gets an honest
    # message, not "cannot exceed".
    if min(po.order_quantity, po.received_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

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
    try:
        db.commit()
    except IntegrityError:  # po_no globally unique; tenant-scoped pre-check
        db.rollback()
        raise HTTPException(status_code=409, detail="PO number is already in use")
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

    # received_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can hold a true NULL,
    # and PurchaseOrderUpdate.received_quantity is Optional (omitted -> not applied).
    # Pre-fix a PATCH against such a row hit `None > order_quantity` and
    # `received - None` (max()) and raised TypeError, 500-ing the update — even the
    # normal "receive stock" PATCH, whose new value is fine but whose OLD value is
    # NULL. /analytics/purchasing already coalesces this exact NULL to its column
    # default of 0; do the same here (and response_model PurchaseOrderResponse
    # .received_quantity is `int`, so a NULL would ResponseValidationError-500 on
    # the way out regardless). Capture old_received BEFORE the payload applies so
    # the stock-receipt delta stays correct (new - 0 = new, not new - new = 0).
    old_received = po.received_quantity or 0

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(po, key, value)

    if po.received_quantity is None:
        po.received_quantity = 0

    # Reject a negative received patch (parity with create / the production-record
    # #266 and quality #324 ingests). A NULL heals to 0 above and is fine; a negative
    # (`{"received_quantity": -5}`) is physically impossible and would corrupt the
    # receipt-rate window — so a clean 400 before the receipt delta and status logic
    # run. order_quantity isn't settable via this schema, so it needs no guard.
    if po.received_quantity < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

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
            # current_stock is likewise Column(Integer, default=0) WITHOUT
            # nullable=False; `None += int` would TypeError-500 the receipt on a
            # legacy NULL-stock item (same guard as subscribers.py / inventory_routes).
            item.current_stock = (item.current_stock or 0) + received_delta

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
    today = datetime.utcnow().date()

    # Resolve supplier names from a single lightweight 2-column fetch of the
    # (small, bounded) supplier roster — its length is also the supplier count.
    # An unknown/foreign supplier_id is simply absent from the map and falls back
    # to the "Supplier {id}" label (a deleted-supplier row keeps its pending).
    supplier_rows = db.query(models.Supplier.id, models.Supplier.supplier_name).all()
    supplier_names = {sid: name for sid, name in supplier_rows}

    # Roll the GROWING purchase_orders table up in SQL rather than hydrating every
    # PO row into Python just to bucket/sum it with list comprehensions (rule-4
    # antipattern; the same GROUP BY fix already applied to /analytics/customer-
    # orders #295 and the other analytics endpoints). PurchaseOrder is in
    # SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes every
    # aggregate SELECT below exactly as it did the old .all() scan.
    #
    # received_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can hold a true NULL;
    # SUM already skips NULLs and COALESCE(..,0) turns an empty book's NULL sum into
    # 0, matching the old `sum(received_quantity or 0)` (and the CSV export's
    # `received_quantity or 0`). order_quantity is nullable=False and needs no guard.
    overdue_case = case((models.PurchaseOrder.expected_delivery_date < today, 1), else_=0)
    status_rows = db.query(
        models.PurchaseOrder.status,
        func.count(models.PurchaseOrder.id),
        func.coalesce(func.sum(models.PurchaseOrder.order_quantity), 0),
        func.coalesce(func.sum(models.PurchaseOrder.received_quantity), 0),
        func.coalesce(func.sum(overdue_case), 0),
    ).group_by(models.PurchaseOrder.status).all()

    status_counts = {}
    purchase_orders = 0
    ordered_qty = 0
    received_qty = 0
    overdue = 0
    for status, count, ordered, received, overdue_in_group in status_rows:
        status_counts[status] = int(count)
        purchase_orders += int(count)
        ordered_qty += int(ordered)
        received_qty += int(received)
        # Overdue = past its expected date AND not already Received/Cancelled.
        # status is String default="Open" (not NOT NULL), so a raw-SQL / migration
        # / cleared write can store NULL; the old Python `status not in [...]`
        # counted a NULL-status overdue PO as overdue (None is not in the list),
        # and the overdue-escalation generator OR's the same NULL back in so the
        # headline and the generated escalations reconcile (mirrors #298 for
        # customer orders). SQL groups NULL status into its own row, which this
        # `not in` includes exactly as before.
        if status not in ("Received", "Cancelled"):
            overdue += int(overdue_in_group)

    receipt_rate = round((received_qty / ordered_qty) * 100) if ordered_qty else 0

    # Per-supplier outstanding (order minus received, floored at 0 per PO, then
    # summed) in one GROUP BY supplier_id — at most one row per supplier, not a
    # Python pass over every PO. Mirrors the per-record floor of the old
    # `max(order_quantity - (received_quantity or 0), 0)`.
    per_po_pending = models.PurchaseOrder.order_quantity - func.coalesce(
        models.PurchaseOrder.received_quantity, 0
    )
    pending_case = case((per_po_pending > 0, per_po_pending), else_=0)
    pending_rows = db.query(
        models.PurchaseOrder.supplier_id,
        func.coalesce(func.sum(pending_case), 0),
    ).group_by(models.PurchaseOrder.supplier_id).all()

    supplier_pending = {}
    for supplier_id, pending in pending_rows:
        name = supplier_names.get(supplier_id, f"Supplier {supplier_id}")
        supplier_pending[name] = supplier_pending.get(name, 0) + int(pending)

    return {
        "suppliers": len(supplier_names),
        "purchase_orders": purchase_orders,
        "open": status_counts.get("Open", 0),
        "partial": status_counts.get("Partial", 0),
        "received": status_counts.get("Received", 0),
        "cancelled": status_counts.get("Cancelled", 0),
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

    # "Overdue" here MUST mean exactly what the /analytics/purchasing headline
    # counts as overdue, or the two disagree: the headline says N overdue while
    # this generator raises fewer than N escalations. status is String
    # default="Open" (not NOT NULL), so a raw-SQL / migration / cleared write can
    # store NULL, and SQL's `status NOT IN (...)` is NULL — not TRUE — for a NULL
    # status, which silently drops an overdue NULL-status PO from this scan even
    # though the analytics endpoint counts it as overdue. OR the NULL back in so
    # the escalation basis reconciles with the headline (mirrors #298 for
    # customer orders).
    overdue_pos = (
        db.query(models.PurchaseOrder)
        .filter(
            models.PurchaseOrder.expected_delivery_date < today,
            or_(
                models.PurchaseOrder.status.is_(None),
                models.PurchaseOrder.status.notin_(["Received", "Cancelled"]),
            ),
        )
        .all()
    )

    created = 0

    for po in overdue_pos:
        title = f"Overdue purchase order: {po.po_no}"

        # Dedup against an already-OPEN escalation with the same title. Escalation.status
        # is Column(String, default="Open") WITHOUT nullable=False, so a raw-SQL /
        # migration / cleared-field row can carry a genuine NULL. In SQL a bare
        # `status != 'Resolved'` is NULL — not TRUE — for such a row, so it did NOT find
        # a NULL-status open escalation and raised a DUPLICATE, inflating the very
        # open-escalation count every reader reports. OR the NULL back in so a NULL-status
        # open escalation blocks the duplicate; a Resolved one still lets a fresh
        # recurrence through. (Mirrors #425, which missed this generator.)
        existing = (
            db.query(models.Escalation)
            .filter(
                models.Escalation.title == title,
                or_(
                    models.Escalation.status.is_(None),
                    models.Escalation.status != "Resolved",
                ),
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
                # Coalesce a NULL received_quantity to 0 for the DISPLAYED note (same
                # reason as the late-order generator above). received_quantity is
                # Column(Integer, default=0) WITHOUT nullable=False, and this generator's
                # overdue basis selects on expected_delivery_date/status (never
                # received_quantity), so a NULL-received overdue PO reaches here and the
                # note read "received None/500" — a raw None in a value a human reads
                # (rule-2). 0 is the column's own default and the coalesce
                # /analytics/purchasing and the CSV export already apply to it.
                f"Supplier {supplier_name}; item {po.item_name}; "
                f"expected {po.expected_delivery_date}; received {po.received_quantity or 0}/{po.order_quantity}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}


@router.post("/suppliers/import-csv")
async def import_suppliers_csv(
    file: UploadFile = File(...),
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    """Onboarding import: the supplier directory from a CSV (same style as the
    inventory Tally import and the machines import). Upserts by supplier_code,
    accepts flexible headers (supplier_code/Code, supplier_name/Name/Supplier,
    contact_person/Contact, email/Email, phone/Phone, category/Category,
    status/Status), requires code + name per row (else skipped), and reports
    per-row errors instead of failing the whole file. Auto-scoped (ADR-0002)."""
    text, encoding = await read_upload_text(file)
    reader = csv.DictReader(io.StringIO(text))
    created = updated = skipped = 0
    errors = []
    # One SAVEPOINT per row — supplier_code is UNIQUE globally while this lookup
    # is tenant-scoped (ADR-0002), so a code another workspace owns is invisible
    # here and takes the insert branch. Without the savepoint that collision
    # surfaces on a LATER row's autoflush, gets blamed on that innocent row, and
    # then poisons the single commit below into a 500 that loses the whole file.
    # See the same fix and its full trace in enterprise_inventory_routes.
    for i, row in enumerate(reader, start=2):
        outcome = None
        try:
            with db.begin_nested():
                code = (row.get("supplier_code") or row.get("Code") or row.get("Supplier Code") or "").strip()
                name = (row.get("supplier_name") or row.get("Name") or row.get("Supplier") or "").strip()
                if not code or not name:
                    outcome = "skipped"
                else:
                    contact = (row.get("contact_person") or row.get("Contact") or "").strip()
                    email = (row.get("email") or row.get("Email") or "").strip()
                    phone = (row.get("phone") or row.get("Phone") or "").strip()
                    category = (row.get("category") or row.get("Category") or "").strip()
                    status = (row.get("status") or row.get("Status") or "Active").strip() or "Active"
                    existing = db.query(models.Supplier).filter(models.Supplier.supplier_code == code).first()
                    if existing:
                        existing.supplier_name = name
                        if contact:
                            existing.contact_person = contact
                        if email:
                            existing.email = email
                        if phone:
                            existing.phone = phone
                        if category:
                            existing.category = category
                        existing.status = status
                        outcome = "updated"
                    else:
                        db.add(models.Supplier(
                            supplier_code=code, supplier_name=name,
                            contact_person=contact or None, email=email or None,
                            phone=phone or None, category=category or None, status=status,
                        ))
                        outcome = "created"
        except Exception as e:
            errors.append(f"Row {i}: {import_row_error(e)}")
            continue
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "skipped":
            skipped += 1
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors[:10], "encoding": encoding}
