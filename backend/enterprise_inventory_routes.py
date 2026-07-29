"""
Enterprise Inventory routes — appended to main.py at startup via import.
Remnants, Material Issue Slips, GRN, Cycle Count, Variance Report, CSV Import.
"""
import csv as csv_lib
import io
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import case, func
from sqlalchemy.orm import Session

import models
from csv_safe import read_upload_text
from payload_fields import int_field, str_field
from auth import get_current_user, require_roles
from database import SessionLocal


router = APIRouter(tags=["Enterprise Inventory"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Bounded pages + cheap item labels ────────────────────────
#
# The history/list endpoints in this file all read from tables that only ever
# grow (remnants, issue slips, receipts, cycle counts) and label each row from
# the item master. Loading the whole table plus the whole InventoryItem master
# on every call is the rule-4 antipattern; these helpers give the bounded,
# label-only version. The GRN and cycle-count endpoints additionally nest their
# line items (see _children / _group_by).

_PAGE_DEFAULT = 50
_PAGE_MAX = 200

# SQLite allows at most 999 bound parameters per statement, and a page of large
# receipts can reference more item ids than that.
_IN_CHUNK = 500


def _page(limit: int, offset: int):
    """Clamp caller-supplied paging into a range that cannot exhaust the server."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _PAGE_DEFAULT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, _PAGE_MAX)), max(0, offset)


def _in_chunks(db, model, column, values):
    """`WHERE column IN values` for arbitrarily many values."""
    values = list(values)
    rows = []
    for i in range(0, len(values), _IN_CHUNK):
        rows.extend(db.query(model).filter(column.in_(values[i:i + _IN_CHUNK])).all())
    return rows


def _children(db, model, fk_column, parent_ids):
    """Only the child rows belonging to the parents on this page."""
    return _in_chunks(db, model, fk_column, parent_ids) if parent_ids else []


def _group_by(rows, attr):
    """{fk: [rows]} in one pass, replacing a rescan of `rows` per parent."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[getattr(row, attr)].append(row)
    return grouped


def _item_labels(db, item_ids):
    """{item_id: (item_code, item_name)} for just the items this page references.

    The callers only ever read those two columns, so this replaces loading the
    entire InventoryItem table as ORM objects."""
    item_ids = {i for i in item_ids if i is not None}
    labels = {}
    ids = list(item_ids)
    for i in range(0, len(ids), _IN_CHUNK):
        for row in (db.query(models.InventoryItem.id, models.InventoryItem.item_code,
                             models.InventoryItem.item_name)
                    .filter(models.InventoryItem.id.in_(ids[i:i + _IN_CHUNK])).all()):
            labels[row.id] = (row.item_code, row.item_name)
    return labels


# ── Remnants ──────────────────────────────────────────────────


@router.get("/remnants")
def get_remnants(limit: int = _PAGE_DEFAULT, offset: int = 0,
                 db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Remnant (offcut) stock, newest first, one page at a time.

    This used to `.all()` the whole (ever-growing) remnants table and, on every
    call, hydrate the ENTIRE InventoryItem master into ORM objects just to read
    two columns per row — the same rule-4 antipattern the GRN / cycle-count
    history endpoints in this file already retired. A remnant table only ever
    grows (one row per offcut logged), so paging keeps the response bounded, and
    _item_labels fetches only the (item_code, item_name) of the items THIS page
    references. Tenant scoping stays automatic (ADR-0002)."""
    limit, offset = _page(limit, offset)
    rows = (db.query(models.Remnant)
            .order_by(models.Remnant.id.desc())
            .limit(limit).offset(offset).all())
    items = _item_labels(db, {r.item_id for r in rows})
    return [
        {
            "id": r.id, "tag_no": r.tag_no, "item_id": r.item_id,
            "item_code": items.get(r.item_id, ("", ""))[0],
            "item_name": items.get(r.item_id, ("", ""))[1],
            "source_reference": r.source_reference,
            "original_qty": r.original_qty, "remaining_qty": r.remaining_qty,
            "unit": r.unit, "location": r.location,
            "status": r.status, "notes": r.notes, "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/remnants")
def create_remnant(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    # Same raw-`payload: dict` boundary as the GRN/cycle-count handlers (#379):
    # nothing validates the body, so a missing key, "", null or "abc" reached
    # int() and raised — a bare 500 (and a missing "unit" KeyError'd, or a null
    # unit tripped the NOT NULL constraint into an IntegrityError 500). Validate
    # every field through the shared _int_field first so malformed input is a 400
    # naming the field, exactly like the sibling receipt handlers.
    item_id = int_field(payload, "item_id", "Remnant")
    original_qty = int_field(payload, "original_qty", "Remnant")
    remaining_qty = int_field(payload, "remaining_qty", "Remnant")
    unit = str_field(payload, "unit", "Remnant")
    count = db.query(models.Remnant).count()
    r = models.Remnant(
        tag_no=payload.get("tag_no") or f"REM-{1000 + count + 1}",
        item_id=item_id,
        source_reference=payload.get("source_reference", ""),
        original_qty=original_qty,
        remaining_qty=remaining_qty,
        unit=unit,
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
    # required=False: an absent/blank remaining_qty keeps the current value (as the
    # old `.get(..., r.remaining_qty)` default did); a present but non-numeric one
    # is a 400, not the bare 500 that int("abc") used to raise.
    r.remaining_qty = int_field(payload, "remaining_qty", "Remnant",
                                 required=False, default=r.remaining_qty or 0)
    db.commit()
    return {"ok": True}

# ── Material Issue Slips ──────────────────────────────────────


@router.get("/issue-slips")
def get_issue_slips(limit: int = _PAGE_DEFAULT, offset: int = 0,
                    db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Material issue slips, newest first, one page at a time.

    Same rule-4 fix as get_remnants above: the issue-slip table grows one row per
    material request, and this used to `.all()` it whole and hydrate the entire
    InventoryItem master just to read two columns. Page the slips and label only
    the items this page references. Tenant scoping stays automatic (ADR-0002)."""
    limit, offset = _page(limit, offset)
    rows = (db.query(models.MaterialIssueSlip)
            .order_by(models.MaterialIssueSlip.id.desc())
            .limit(limit).offset(offset).all())
    items = _item_labels(db, {s.item_id for s in rows})
    return [
        {
            "id": s.id, "slip_no": s.slip_no, "item_id": s.item_id,
            "item_code": items.get(s.item_id, ("", ""))[0],
            "item_name": items.get(s.item_id, ("", ""))[1],
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
    # Raw `payload: dict` boundary (see create_remnant / #379): validate the
    # numeric fields through the shared _int_field so a missing/blank/non-numeric
    # item_id or requested_qty is a 400 naming the field, not a bare int() 500.
    item_id = int_field(payload, "item_id", "Issue slip")
    requested_qty = int_field(payload, "requested_qty", "Issue slip")
    # remnant_id is optional (a slip may draw from general stock): parse only when
    # supplied, still validated so "abc" is a 400 rather than a 500.
    remnant_id = int_field(payload, "remnant_id", "Issue slip") if payload.get("remnant_id") else None
    count = db.query(models.MaterialIssueSlip).count()
    s = models.MaterialIssueSlip(
        slip_no=f"MIS-{5000 + count + 1}",
        item_id=item_id,
        remnant_id=remnant_id,
        work_order_ref=payload.get("work_order_ref", ""),
        requested_qty=requested_qty,
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
    # Only a Pending slip may be approved. Without this, an already-Issued slip
    # could be flipped back to "Approved" and issued again (issue_slip only checks
    # status == "Approved"), deducting the same stock twice.
    if s.status != "Pending":
        raise HTTPException(status_code=400, detail=f"Slip cannot be approved from status '{s.status}'")
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
    # current_stock is Column(Integer, default=0) WITHOUT nullable=False, so a
    # raw-SQL / migration / cleared-field write can store a true NULL. `None < int`
    # and `None -= int` raise TypeError -> an unhandled 500 on the issue. A NULL
    # stock is an empty shelf (0 available) — the same coalesce every sibling path
    # applies to this column (inventory_routes, subscribers, orders_routes).
    current_stock = item.current_stock or 0
    if current_stock < s.requested_qty:
        raise HTTPException(status_code=400, detail=f"Insufficient stock: {current_stock} {item.unit} available")
    item.current_stock = current_stock - s.requested_qty
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
def get_grns(limit: int = _PAGE_DEFAULT, offset: int = 0,
             db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Receipt history, newest first, one page at a time.

    This used to `.all()` the whole goods_receipt_notes and grn_items tables and
    then, for every GRN, rescan the entire line list to find its own lines — an
    O(GRNs x lines) join done in Python. Measured on seeded data: 4,000 GRNs with
    16,000 lines took 47s, of which 37s was that one rescan; the three queries
    that fetched the data took 0.19s. Grouping the lines once into a dict gives
    identical output, and paging keeps the response bounded as the table grows
    (a receipt table only ever grows)."""
    limit, offset = _page(limit, offset)
    grns = (db.query(models.GoodsReceiptNote)
            .order_by(models.GoodsReceiptNote.id.desc())
            .limit(limit).offset(offset).all())
    grn_items = _children(db, models.GRNItem, models.GRNItem.grn_id, [g.id for g in grns])
    by_grn = _group_by(grn_items, "grn_id")
    items = _item_labels(db, {x.item_id for x in grn_items})
    result = []
    for g in grns:
        gi = by_grn.get(g.id, [])
        result.append({
            "id": g.id, "grn_no": g.grn_no,
            "purchase_order_ref": g.purchase_order_ref,
            "supplier_name": g.supplier_name, "received_by": g.received_by,
            "status": g.status, "notes": g.notes, "created_at": g.created_at,
            "items": [
                {
                    "id": x.id, "item_id": x.item_id,
                    "item_code": items.get(x.item_id, ("", ""))[0],
                    "item_name": items.get(x.item_id, ("", ""))[1],
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
    """Record a goods receipt. Header and lines are written in ONE transaction.

    This used to commit the header, then parse the lines — so one malformed line
    (a missing accepted_qty, a blank field, a JSON null) raised out of the loop as
    a 500 while the header stayed committed. That left a zero-line GRN in the
    receipt history that accept_grn would still happily mark "Accepted": a
    goods receipt on record against which no goods ever moved, and which each
    retry duplicated. Lines are now validated BEFORE anything is written, and the
    header is flushed (not committed) so a failure rolls the whole thing back."""
    supplier_name = str_field(payload, "supplier_name")

    # Validate every line first — nothing is written until all of them parse.
    parsed = []
    for i, line in enumerate(payload.get("items", []), start=1):
        where = f"Line {i}"
        if not isinstance(line, dict):
            raise HTTPException(status_code=400, detail=f"{where}: expected an object")
        parsed.append(dict(
            item_id=int_field(line, "item_id", where),
            lot_no=line.get("lot_no", ""),
            ordered_qty=int_field(line, "ordered_qty", where, required=False),
            received_qty=int_field(line, "received_qty", where),
            accepted_qty=int_field(line, "accepted_qty", where),
            rejected_qty=int_field(line, "rejected_qty", where, required=False),
            inspection_status=line.get("inspection_status", "Accepted"),
        ))

    count = db.query(models.GoodsReceiptNote).count()
    g = models.GoodsReceiptNote(
        grn_no=f"GRN-{3000 + count + 1}",
        purchase_order_ref=payload.get("purchase_order_ref", ""),
        supplier_name=supplier_name,
        received_by=payload.get("received_by", current_user.get("sub", "Admin")),
        status="Draft",
        notes=payload.get("notes", ""),
    )
    # flush, not commit — g.id is assigned but the row is still inside the
    # transaction, so anything below that fails takes the header down with it.
    db.add(g); db.flush()
    for row in parsed:
        db.add(models.GRNItem(grn_id=g.id, **row))
    db.commit()
    return {"id": g.id, "grn_no": g.grn_no}


@router.patch("/grns/{gid}/accept")
def accept_grn(gid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    g = db.query(models.GoodsReceiptNote).filter(models.GoodsReceiptNote.id == gid).first()
    if not g:
        raise HTTPException(status_code=404, detail="GRN not found")
    # Accepting adds each line's accepted_qty to stock, so it must run at most once.
    # Without this guard a repeat call re-adds the whole GRN to inventory (and logs
    # a second Receive transaction) — silent stock inflation.
    if g.status != "Draft":
        raise HTTPException(status_code=400, detail=f"GRN already processed (status '{g.status}')")
    gi = db.query(models.GRNItem).filter(models.GRNItem.grn_id == gid).all()
    for line in gi:
        if line.accepted_qty > 0:
            item = db.query(models.InventoryItem).filter(models.InventoryItem.id == line.item_id).first()
            if item:
                # NULL current_stock (nullable Integer) would TypeError on `+=`;
                # coalesce an empty shelf to 0 before adding the receipt, the same
                # guard orders_routes uses on the PO-receipt stock bump.
                item.current_stock = (item.current_stock or 0) + line.accepted_qty
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
def get_cycle_counts(limit: int = _PAGE_DEFAULT, offset: int = 0,
                     db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Cycle-count history, newest first, one page at a time.

    Same shape as get_grns above, and the same fix. The select-all checkbox in
    the UI makes a count containing every SKU, so a year of weekly full counts is
    already 26k lines — measured 28s at 2,000 counts / 20,000 lines, and a 17 MB
    JSON body at five years of history."""
    limit, offset = _page(limit, offset)
    counts = (db.query(models.CycleCount)
              .order_by(models.CycleCount.id.desc())
              .limit(limit).offset(offset).all())
    count_items = _children(db, models.CycleCountItem, models.CycleCountItem.count_id,
                            [c.id for c in counts])
    by_count = _group_by(count_items, "count_id")
    items = _item_labels(db, {x.item_id for x in count_items})
    result = []
    for c in counts:
        ci = by_count.get(c.id, [])
        result.append({
            "id": c.id, "count_no": c.count_no, "counted_by": c.counted_by,
            "status": c.status, "notes": c.notes, "created_at": c.created_at,
            "items": [
                {
                    "id": x.id, "item_id": x.item_id,
                    "item_code": items.get(x.item_id, ("", ""))[0],
                    "item_name": items.get(x.item_id, ("", ""))[1],
                    "book_qty": x.book_qty, "physical_qty": x.physical_qty,
                    "variance": x.variance,
                }
                for x in ci
            ],
        })
    return result


@router.post("/cycle-counts")
def create_cycle_count(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    """Record a physical stock count. Header and lines are written in ONE
    transaction, for the same reason as create_grn above: this used to commit the
    header before parsing lines, so a blank physical_qty raised a 500 and left a
    zero-line Draft count that approve_cycle_count would still mark "Approved" —
    a completed stock reconciliation on record that adjusted nothing."""
    # Validate every line first — nothing is written until all of them parse.
    parsed = []
    for i, line in enumerate(payload.get("items", []), start=1):
        where = f"Line {i}"
        if not isinstance(line, dict):
            raise HTTPException(status_code=400, detail=f"{where}: expected an object")
        parsed.append((int_field(line, "item_id", where),
                       int_field(line, "physical_qty", where)))

    count = db.query(models.CycleCount).count()
    c = models.CycleCount(
        count_no=f"CC-{2000 + count + 1}",
        counted_by=payload.get("counted_by", current_user.get("sub", "Admin")),
        status="Draft",
        notes=payload.get("notes", ""),
    )
    db.add(c); db.flush()          # see create_grn: flush keeps this in the transaction
    for item_id, physical in parsed:
        item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
        if not item:
            continue
        # book_qty records the on-book stock at count time. A NULL current_stock
        # (nullable Integer) would both make `physical - None` TypeError AND write
        # book_qty=NULL, which violates CycleCountItem.book_qty (nullable=False) ->
        # IntegrityError. Treat a missing stock as an empty shelf (0), consistent
        # with the issue/receipt paths above.
        book_qty = item.current_stock or 0
        db.add(models.CycleCountItem(
            count_id=c.id, item_id=item.id,
            book_qty=book_qty,
            physical_qty=physical,
            variance=physical - book_qty,
        ))
    db.commit()
    return {"id": c.id, "count_no": c.count_no}


@router.patch("/cycle-counts/{cid}/approve")
def approve_cycle_count(cid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin"]))):
    c = db.query(models.CycleCount).filter(models.CycleCount.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cycle count not found")
    # Approving moves stock, so it must run at most once — the same rule accept_grn
    # states 100 lines above. Without this guard a repeat call re-applies the count:
    # variance is written once at creation and never cleared, so `variance != 0`
    # below is still true and the whole body fires again. Measured on a 100-unit
    # item counted at 90, then legitimately receipted by 500:
    #
    #     approve #1 -> 90    receipt -> 590    approve #2 -> 90
    #
    # 500 units destroyed, and a second "Adjust 10" ledger row written that makes
    # the trail corroborate the corrupted figure instead of exposing it. The
    # Approve button carries no disabled state (EnterpriseInventory.tsx), so a
    # double-click, a retry after a timeout, or a second admin on a 3s-polled page
    # all reach this.
    if c.status != "Draft":
        raise HTTPException(status_code=400, detail=f"Cycle count already processed (status '{c.status}')")
    ci = db.query(models.CycleCountItem).filter(models.CycleCountItem.count_id == cid).all()
    for line in ci:
        if line.variance != 0:
            item = db.query(models.InventoryItem).filter(models.InventoryItem.id == line.item_id).first()
            if item:
                # Apply the variance as a DELTA, not `= physical_qty`. A count is
                # taken on the floor and approved later, and an absolute set
                # silently reverts everything booked in between — the same 500-unit
                # loss as above, from a single legitimate approve.
                #
                # The delta is also what this block's own ledger row already
                # claims: it records quantity=abs(variance) and "Variance: -10",
                # never "set to 90". The stock mutation now matches the audit row
                # instead of contradicting it. NULL current_stock (nullable
                # Integer) coalesces to 0, the idiom accept_grn uses above.
                item.current_stock = (item.current_stock or 0) + line.variance
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

# Transaction types that add to / draw down on-hand stock. Unchanged from the
# prior Python filter — kept as module constants so the SQL aggregate and the
# semantics stay in one place.
_IN_TYPES = ("Receive", "Return")
_OUT_TYPES = ("Issue", "Adjust")


@router.get("/inventory/variance-report")
def variance_report(db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    items = db.query(models.InventoryItem).all()

    # Received/issued totals per item, summed in SQL. inventory_transactions is on
    # the growing-table list (rule 4): the old code pulled the WHOLE ledger into
    # Python and did an O(items x transactions) `t.item_id == item.id` filter per
    # item. One GROUP BY replaces that (item_id is already indexed — main.py), and
    # the result set is one row per item (a naturally bounded master table).
    # InventoryTransaction is tenant-scoped (tenancy.SCOPED_MODELS), so the
    # do_orm_execute hook still filters the aggregate to the request's tenant
    # exactly as it did the old .all() scan. quantity is nullable=False, but
    # coalesce so an item with only one side reads 0 rather than NULL.
    txn_totals = {
        item_id: (int(total_in), int(total_out))
        for item_id, total_in, total_out in (
            db.query(
                models.InventoryTransaction.item_id,
                func.coalesce(func.sum(case(
                    (models.InventoryTransaction.transaction_type.in_(_IN_TYPES),
                     models.InventoryTransaction.quantity), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (models.InventoryTransaction.transaction_type.in_(_OUT_TYPES),
                     models.InventoryTransaction.quantity), else_=0)), 0),
            )
            .group_by(models.InventoryTransaction.item_id)
            .all()
        )
    }

    # The latest cycle-count line per item, picked DETERMINISTICALLY by max(id) in
    # SQL — not by hydrating the whole (growing) cycle_count_items table and letting
    # a Python last-write-wins depend on the scan's row order. On SQLite a table scan
    # happens to come back id-ascending, so the old dict-overwrite kept the highest-id
    # (latest) line; on PostgreSQL (production) a seqscan has NO guaranteed order, so
    # it could keep a STALE earlier count and report the wrong physical_qty / variance
    # for that item — a value the data doesn't support (rule 2). Selecting the max-id
    # line per item_id fixes that AND bounds the scan (rule 4): the result set is one
    # row per counted item (a naturally bounded set), served by the existing
    # cycle_count_items(item_id) index (main.py). Tenant-safe: CycleCountItem is in
    # tenancy.SCOPED_MODELS so the outer select is tenant-filtered by the ORM hook, and
    # item_id is a FK to the globally-unique inventory_items.id (never shared across
    # tenants), so a per-item max(id) is inherently the caller's own latest line — a
    # foreign row can never match the scoped outer filter (proven by
    # test_variance_report_is_tenant_isolated).
    latest_ci_ids = (
        db.query(func.max(models.CycleCountItem.id))
        .group_by(models.CycleCountItem.item_id)
        .scalar_subquery()
    )
    latest_count_items = {
        ci.item_id: ci
        for ci in (
            db.query(models.CycleCountItem)
            .filter(models.CycleCountItem.id.in_(latest_ci_ids))
            .all()
        )
    }

    rows = []
    for item in items:
        total_in, total_out = txn_totals.get(item.id, (0, 0))
        last_count = latest_count_items.get(item.id)
        # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
        # nullable=False, so either can be a true NULL. The old status ternary did
        # `None == 0` (falls through) then `None <= reorder_level` (or `int <= None`)
        # -> TypeError, 500-ing the WHOLE report on a single legacy NULL row (the
        # loop covers every item). A NULL stock is an empty shelf (0) — the coalesce
        # every sibling path uses; book_stock reports the same 0 so the displayed
        # stock and the status derived from it reconcile.
        stock = item.current_stock or 0
        level = item.reorder_level or 0
        rows.append({
            "item_id": item.id,
            "item_code": item.item_code,
            "item_name": item.item_name,
            "category": item.category,
            "unit": item.unit,
            "book_stock": stock,
            "total_received": total_in,
            "total_issued": total_out,
            "last_physical_count": last_count.physical_qty if last_count else None,
            "last_variance": last_count.variance if last_count else None,
            "status": (
                "Stockout" if stock == 0
                else "Low" if stock <= level
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
    text, encoding = await read_upload_text(file)
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
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors[:10], "encoding": encoding}
