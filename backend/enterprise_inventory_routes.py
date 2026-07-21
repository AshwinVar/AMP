"""
Enterprise Inventory routes — appended to main.py at startup via import.
Remnants, Material Issue Slips, GRN, Cycle Count, Variance Report, CSV Import.
"""
import csv as csv_lib
import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

import models
from auth import get_current_user, require_roles
from database import SessionLocal


router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── Remnants ──────────────────────────────────────────────────


@router.get("/remnants")
def get_remnants(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.Remnant).order_by(models.Remnant.id.desc()).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    return [
        {
            "id": r.id, "tag_no": r.tag_no, "item_id": r.item_id,
            "item_code": items[r.item_id].item_code if r.item_id in items else "",
            "item_name": items[r.item_id].item_name if r.item_id in items else "",
            "source_reference": r.source_reference,
            "original_qty": r.original_qty, "remaining_qty": r.remaining_qty,
            "unit": r.unit, "location": r.location,
            "status": r.status, "notes": r.notes, "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/remnants")
def create_remnant(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    count = db.query(models.Remnant).count()
    r = models.Remnant(
        tag_no=payload.get("tag_no") or f"REM-{1000 + count + 1}",
        item_id=int(payload["item_id"]),
        source_reference=payload.get("source_reference", ""),
        original_qty=int(payload["original_qty"]),
        remaining_qty=int(payload["remaining_qty"]),
        unit=payload["unit"],
        location=payload.get("location", ""),
        status="Available",
        notes=payload.get("notes", ""),
    )
    db.add(r); db.commit(); db.refresh(r)
    return {"id": r.id, "tag_no": r.tag_no}


@router.patch("/remnants/{rid}/status")
def update_remnant_status(rid: int, payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    r = db.query(models.Remnant).filter(models.Remnant.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Remnant not found")
    r.status = payload.get("status", r.status)
    r.remaining_qty = int(payload.get("remaining_qty", r.remaining_qty))
    db.commit()
    return {"ok": True}

# ── Material Issue Slips ──────────────────────────────────────


@router.get("/issue-slips")
def get_issue_slips(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.MaterialIssueSlip).order_by(models.MaterialIssueSlip.id.desc()).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    return [
        {
            "id": s.id, "slip_no": s.slip_no, "item_id": s.item_id,
            "item_code": items[s.item_id].item_code if s.item_id in items else "",
            "item_name": items[s.item_id].item_name if s.item_id in items else "",
            "remnant_id": s.remnant_id, "work_order_ref": s.work_order_ref,
            "requested_qty": s.requested_qty, "issued_qty": s.issued_qty,
            "requested_by": s.requested_by, "approved_by": s.approved_by,
            "status": s.status, "notes": s.notes,
            "created_at": s.created_at, "issued_at": s.issued_at,
        }
        for s in rows
    ]


@router.post("/issue-slips")
def create_issue_slip(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    count = db.query(models.MaterialIssueSlip).count()
    s = models.MaterialIssueSlip(
        slip_no=f"MIS-{5000 + count + 1}",
        item_id=int(payload["item_id"]),
        remnant_id=int(payload["remnant_id"]) if payload.get("remnant_id") else None,
        work_order_ref=payload.get("work_order_ref", ""),
        requested_qty=int(payload["requested_qty"]),
        requested_by=payload.get("requested_by", current_user.get("sub", "Operator")),
        status="Pending",
        notes=payload.get("notes", ""),
    )
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "slip_no": s.slip_no}


@router.patch("/issue-slips/{sid}/approve")
def approve_issue_slip(sid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    s = db.query(models.MaterialIssueSlip).filter(models.MaterialIssueSlip.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="Slip not found")
    s.status = "Approved"
    s.approved_by = current_user.get("sub", "Admin")
    db.commit()
    return {"ok": True}


@router.patch("/issue-slips/{sid}/issue")
def issue_slip(sid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    s = db.query(models.MaterialIssueSlip).filter(models.MaterialIssueSlip.id == sid).first()
    if not s or s.status != "Approved":
        raise HTTPException(status_code=400, detail="Slip must be Approved before issuing")
    item = db.query(models.InventoryItem).filter(models.InventoryItem.id == s.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item.current_stock < s.requested_qty:
        raise HTTPException(status_code=400, detail=f"Insufficient stock: {item.current_stock} {item.unit} available")
    item.current_stock -= s.requested_qty
    s.issued_qty = s.requested_qty
    s.status = "Issued"
    s.issued_at = datetime.utcnow()
    db.add(models.InventoryTransaction(
        item_id=item.id, transaction_type="Issue",
        quantity=s.requested_qty, reference=s.slip_no,
        notes=f"Issued via {s.slip_no} for {s.work_order_ref or 'unspecified job'}",
    ))
    if s.remnant_id:
        rem = db.query(models.Remnant).filter(models.Remnant.id == s.remnant_id).first()
        if rem:
            rem.remaining_qty = max(0, rem.remaining_qty - s.requested_qty)
            if rem.remaining_qty == 0:
                rem.status = "Consumed"
    db.commit()
    return {"ok": True}


@router.patch("/issue-slips/{sid}/reject")
def reject_issue_slip(sid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    s = db.query(models.MaterialIssueSlip).filter(models.MaterialIssueSlip.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="Slip not found")
    s.status = "Rejected"
    s.approved_by = current_user.get("sub", "Admin")
    db.commit()
    return {"ok": True}

# ── GRN ──────────────────────────────────────────────────────


@router.get("/grns")
def get_grns(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    grns = db.query(models.GoodsReceiptNote).order_by(models.GoodsReceiptNote.id.desc()).all()
    grn_items = db.query(models.GRNItem).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    result = []
    for g in grns:
        gi = [x for x in grn_items if x.grn_id == g.id]
        result.append({
            "id": g.id, "grn_no": g.grn_no,
            "purchase_order_ref": g.purchase_order_ref,
            "supplier_name": g.supplier_name, "received_by": g.received_by,
            "status": g.status, "notes": g.notes, "created_at": g.created_at,
            "items": [
                {
                    "id": x.id, "item_id": x.item_id,
                    "item_code": items[x.item_id].item_code if x.item_id in items else "",
                    "item_name": items[x.item_id].item_name if x.item_id in items else "",
                    "lot_no": x.lot_no, "ordered_qty": x.ordered_qty,
                    "received_qty": x.received_qty, "accepted_qty": x.accepted_qty,
                    "rejected_qty": x.rejected_qty, "inspection_status": x.inspection_status,
                }
                for x in gi
            ],
        })
    return result


@router.post("/grns")
def create_grn(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    count = db.query(models.GoodsReceiptNote).count()
    g = models.GoodsReceiptNote(
        grn_no=f"GRN-{3000 + count + 1}",
        purchase_order_ref=payload.get("purchase_order_ref", ""),
        supplier_name=payload["supplier_name"],
        received_by=payload.get("received_by", current_user.get("sub", "Admin")),
        status="Draft",
        notes=payload.get("notes", ""),
    )
    db.add(g); db.commit(); db.refresh(g)
    for line in payload.get("items", []):
        db.add(models.GRNItem(
            grn_id=g.id, item_id=int(line["item_id"]),
            lot_no=line.get("lot_no", ""),
            ordered_qty=int(line.get("ordered_qty", 0)),
            received_qty=int(line["received_qty"]),
            accepted_qty=int(line["accepted_qty"]),
            rejected_qty=int(line.get("rejected_qty", 0)),
            inspection_status=line.get("inspection_status", "Accepted"),
        ))
    db.commit()
    return {"id": g.id, "grn_no": g.grn_no}


@router.patch("/grns/{gid}/accept")
def accept_grn(gid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    g = db.query(models.GoodsReceiptNote).filter(models.GoodsReceiptNote.id == gid).first()
    if not g:
        raise HTTPException(status_code=404, detail="GRN not found")
    gi = db.query(models.GRNItem).filter(models.GRNItem.grn_id == gid).all()
    for line in gi:
        if line.accepted_qty > 0:
            item = db.query(models.InventoryItem).filter(models.InventoryItem.id == line.item_id).first()
            if item:
                item.current_stock += line.accepted_qty
                db.add(models.InventoryTransaction(
                    item_id=item.id, transaction_type="Receive",
                    quantity=line.accepted_qty, reference=g.grn_no,
                    notes=f"GRN receipt | Lot: {line.lot_no or '-'} | Supplier: {g.supplier_name}",
                ))
    accepted = sum(x.accepted_qty for x in gi)
    received = sum(x.received_qty for x in gi)
    g.status = "Accepted" if accepted == received else "Partial"
    db.commit()
    return {"ok": True, "status": g.status}

# ── Cycle Count ───────────────────────────────────────────────


@router.get("/cycle-counts")
def get_cycle_counts(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    counts = db.query(models.CycleCount).order_by(models.CycleCount.id.desc()).all()
    count_items = db.query(models.CycleCountItem).all()
    items = {i.id: i for i in db.query(models.InventoryItem).all()}
    result = []
    for c in counts:
        ci = [x for x in count_items if x.count_id == c.id]
        result.append({
            "id": c.id, "count_no": c.count_no, "counted_by": c.counted_by,
            "status": c.status, "notes": c.notes, "created_at": c.created_at,
            "items": [
                {
                    "id": x.id, "item_id": x.item_id,
                    "item_code": items[x.item_id].item_code if x.item_id in items else "",
                    "item_name": items[x.item_id].item_name if x.item_id in items else "",
                    "book_qty": x.book_qty, "physical_qty": x.physical_qty,
                    "variance": x.variance,
                }
                for x in ci
            ],
        })
    return result


@router.post("/cycle-counts")
def create_cycle_count(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    count = db.query(models.CycleCount).count()
    c = models.CycleCount(
        count_no=f"CC-{2000 + count + 1}",
        counted_by=payload.get("counted_by", current_user.get("sub", "Admin")),
        status="Draft",
        notes=payload.get("notes", ""),
    )
    db.add(c); db.commit(); db.refresh(c)
    for line in payload.get("items", []):
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == int(line["item_id"])).first()
        if not item:
            continue
        physical = int(line["physical_qty"])
        db.add(models.CycleCountItem(
            count_id=c.id, item_id=item.id,
            book_qty=item.current_stock,
            physical_qty=physical,
            variance=physical - item.current_stock,
        ))
    db.commit()
    return {"id": c.id, "count_no": c.count_no}


@router.patch("/cycle-counts/{cid}/approve")
def approve_cycle_count(cid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    c = db.query(models.CycleCount).filter(models.CycleCount.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cycle count not found")
    ci = db.query(models.CycleCountItem).filter(models.CycleCountItem.count_id == cid).all()
    for line in ci:
        if line.variance != 0:
            item = db.query(models.InventoryItem).filter(models.InventoryItem.id == line.item_id).first()
            if item:
                item.current_stock = line.physical_qty
                db.add(models.InventoryTransaction(
                    item_id=item.id, transaction_type="Adjust",
                    quantity=abs(line.variance),
                    reference=c.count_no,
                    notes=f"Cycle count adjustment | Variance: {line.variance:+d}",
                ))
    c.status = "Approved"
    db.commit()
    return {"ok": True}

# ── Variance Report ───────────────────────────────────────────


@router.get("/inventory/variance-report")
def variance_report(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    items = db.query(models.InventoryItem).all()
    txns = db.query(models.InventoryTransaction).all()
    latest_count_items = {}
    for ci in db.query(models.CycleCountItem).all():
        latest_count_items[ci.item_id] = ci
    rows = []
    for item in items:
        item_txns = [t for t in txns if t.item_id == item.id]
        total_in = sum(t.quantity for t in item_txns if t.transaction_type in ("Receive", "Return"))
        total_out = sum(t.quantity for t in item_txns if t.transaction_type in ("Issue", "Adjust"))
        last_count = latest_count_items.get(item.id)
        rows.append({
            "item_id": item.id,
            "item_code": item.item_code,
            "item_name": item.item_name,
            "category": item.category,
            "unit": item.unit,
            "book_stock": item.current_stock,
            "total_received": total_in,
            "total_issued": total_out,
            "last_physical_count": last_count.physical_qty if last_count else None,
            "last_variance": last_count.variance if last_count else None,
            "status": (
                "Stockout" if item.current_stock == 0
                else "Low" if item.current_stock <= item.reorder_level
                else "OK"
            ),
        })
    return sorted(rows, key=lambda r: (r["last_variance"] or 0))

# ── Tally CSV Import ──────────────────────────────────────────


@router.post("/inventory/import-csv")
async def import_inventory_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv_lib.DictReader(io.StringIO(text))
    created = updated = skipped = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        try:
            code = (row.get("item_code") or row.get("Item Code") or "").strip()
            name = (row.get("item_name") or row.get("Item Name") or "").strip()
            if not code or not name:
                skipped += 1
                continue
            category = (row.get("category") or row.get("Category") or "Imported").strip()
            unit = (row.get("unit") or row.get("Unit") or "pcs").strip()
            stock = int(float((row.get("current_stock") or row.get("Opening Stock") or row.get("Stock") or "0").strip() or 0))
            reorder = int(float((row.get("reorder_level") or row.get("Reorder Level") or "0").strip() or 0))
            supplier = (row.get("supplier") or row.get("Supplier") or "").strip()
            location = (row.get("location") or row.get("Location") or "").strip()
            existing = db.query(models.InventoryItem).filter(models.InventoryItem.item_code == code).first()
            if existing:
                existing.item_name = name
                existing.category = category
                existing.unit = unit
                existing.current_stock = stock
                existing.reorder_level = reorder
                if supplier:
                    existing.supplier = supplier
                if location:
                    existing.location = location
                updated += 1
            else:
                db.add(models.InventoryItem(
                    item_code=code, item_name=name, category=category,
                    unit=unit, current_stock=stock, reorder_level=reorder,
                    supplier=supplier, location=location,
                ))
                created += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:10]}
