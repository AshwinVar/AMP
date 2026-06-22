"""
GMATS tenant-scoped enterprise inventory.

Implements the client's exact spec:
- 4-bucket stock model: Physical / Reserved / Available / Reorder
- Item aliases (many names -> one stock item)
- Purchase entry / stock inward
- Proforma Invoice  -> RESERVES stock (prevents double-selling)
- Tax Invoice       -> DEDUCTS physical stock, clears reservation
- Material Issue Note (free spares with a machine) -> deducts physical, not billed
- Reorder alerts (min stock -> Purchase Required)

Every record carries a tenant_code so the same rig serves any future client.
The frontend company switcher supplies the tenant; defaults to "GMATS".
"""
from datetime import datetime

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user, require_roles
from database import SessionLocal


def register(app):
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _item_dict(db, item):
        aliases = [
            a.alias_name
            for a in db.query(models.GmatsAlias).filter(models.GmatsAlias.item_id == item.id).all()
        ]
        available = item.physical_stock - item.reserved_stock
        return {
            "id": item.id,
            "tenant_code": item.tenant_code,
            "item_code": item.item_code,
            "item_name": item.item_name,
            "category": item.category,
            "unit": item.unit,
            "physical_stock": item.physical_stock,
            "reserved_stock": item.reserved_stock,
            "available_stock": available,
            "reorder_level": item.reorder_level,
            "purchase_rate": item.purchase_rate,
            "location": item.location,
            "supplier": item.supplier,
            "aliases": aliases,
            "reorder_needed": available <= item.reorder_level,
        }

    # ── Items / Stock ─────────────────────────────────────────────

    @app.get("/gmats/items")
    def gmats_items(tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        rows = db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == tenant).order_by(models.GmatsItem.item_name).all()
        return [_item_dict(db, r) for r in rows]

    @app.get("/gmats/summary")
    def gmats_summary(tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        rows = db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == tenant).all()
        physical = sum(r.physical_stock for r in rows)
        reserved = sum(r.reserved_stock for r in rows)
        reorder_needed = sum(1 for r in rows if (r.physical_stock - r.reserved_stock) <= r.reorder_level)
        open_proformas = db.query(models.GmatsProforma).filter(
            models.GmatsProforma.tenant_code == tenant, models.GmatsProforma.status == "Open"
        ).count()
        return {
            "items": len(rows),
            "total_physical": physical,
            "total_reserved": reserved,
            "total_available": physical - reserved,
            "reorder_needed": reorder_needed,
            "open_proformas": open_proformas,
        }

    @app.post("/gmats/items")
    def gmats_create_item(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        item = models.GmatsItem(
            tenant_code=payload.get("tenant", "GMATS"),
            item_code=payload["item_code"],
            item_name=payload["item_name"],
            category=payload.get("category", "General"),
            unit=payload.get("unit", "Nos"),
            physical_stock=int(payload.get("physical_stock", 0)),
            reserved_stock=0,
            reorder_level=int(payload.get("reorder_level", 0)),
            location=payload.get("location", ""),
            purchase_rate=int(payload.get("purchase_rate", 0)),
            supplier=payload.get("supplier", ""),
        )
        db.add(item); db.commit(); db.refresh(item)
        for alias in payload.get("aliases", []):
            if alias.strip():
                db.add(models.GmatsAlias(tenant_code=item.tenant_code, item_id=item.id, alias_name=alias.strip()))
        db.commit()
        return _item_dict(db, item)

    @app.patch("/gmats/items/{item_id}")
    def gmats_update_item(item_id: int, payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        item = db.query(models.GmatsItem).filter(models.GmatsItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        if "reorder_level" in payload:
            item.reorder_level = int(payload["reorder_level"])
        if "purchase_rate" in payload:
            item.purchase_rate = int(payload["purchase_rate"])
        if "location" in payload:
            item.location = payload["location"]
        db.commit()
        return _item_dict(db, item)

    @app.post("/gmats/items/{item_id}/stock-in")
    def gmats_stock_in(item_id: int, payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        """Purchase entry / stock inward — increases physical stock."""
        item = db.query(models.GmatsItem).filter(models.GmatsItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        qty = int(payload["qty"])
        if qty <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be positive")
        item.physical_stock += qty
        if payload.get("purchase_rate"):
            item.purchase_rate = int(payload["purchase_rate"])
        db.commit()
        return _item_dict(db, item)

    # ── Aliases ───────────────────────────────────────────────────

    @app.post("/gmats/items/{item_id}/aliases")
    def gmats_add_alias(item_id: int, payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        item = db.query(models.GmatsItem).filter(models.GmatsItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        name = payload["alias_name"].strip()
        if name:
            db.add(models.GmatsAlias(tenant_code=item.tenant_code, item_id=item.id, alias_name=name))
            db.commit()
        return _item_dict(db, item)

    @app.get("/gmats/resolve")
    def gmats_resolve(name: str, tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        """Resolve any alias / code / name to the single master item — demonstrates the alias system."""
        q = name.strip().lower()
        items = db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == tenant).all()
        for it in items:
            if it.item_code.lower() == q or it.item_name.lower() == q:
                return {"matched": True, "via": "master", "item": _item_dict(db, it)}
        alias = db.query(models.GmatsAlias).filter(models.GmatsAlias.tenant_code == tenant).all()
        for a in alias:
            if a.alias_name.lower() == q:
                it = db.query(models.GmatsItem).filter(models.GmatsItem.id == a.item_id).first()
                if it:
                    return {"matched": True, "via": f"alias '{a.alias_name}'", "item": _item_dict(db, it)}
        return {"matched": False, "via": None, "item": None}

    # ── Proforma Invoice (reserve stock) ──────────────────────────

    @app.get("/gmats/proformas")
    def gmats_proformas(tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        rows = db.query(models.GmatsProforma).filter(models.GmatsProforma.tenant_code == tenant).order_by(models.GmatsProforma.id.desc()).all()
        items = {i.id: i for i in db.query(models.GmatsItem).all()}
        out = []
        for p in rows:
            lines = db.query(models.GmatsProformaLine).filter(models.GmatsProformaLine.proforma_id == p.id).all()
            out.append({
                "id": p.id, "proforma_no": p.proforma_no, "customer_name": p.customer_name,
                "status": p.status, "created_at": p.created_at,
                "lines": [
                    {"item_id": l.item_id,
                     "item_name": items[l.item_id].item_name if l.item_id in items else "",
                     "qty": l.qty}
                    for l in lines
                ],
            })
        return out

    @app.post("/gmats/proformas")
    def gmats_create_proforma(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        tenant = payload.get("tenant", "GMATS")
        lines = payload.get("lines", [])
        if not lines:
            raise HTTPException(status_code=400, detail="At least one line item required")
        # validate availability first
        for line in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == int(line["item_id"])).first()
            if not item:
                raise HTTPException(status_code=404, detail="Item not found")
            available = item.physical_stock - item.reserved_stock
            qty = int(line["qty"])
            if qty > available:
                raise HTTPException(status_code=400, detail=f"Cannot reserve {qty} {item.unit} of {item.item_name}: only {available} available")
        count = db.query(models.GmatsProforma).filter(models.GmatsProforma.tenant_code == tenant).count()
        p = models.GmatsProforma(
            tenant_code=tenant,
            proforma_no=f"PI-{1000 + count + 1}",
            customer_name=payload["customer_name"],
            status="Open",
        )
        db.add(p); db.commit(); db.refresh(p)
        for line in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == int(line["item_id"])).first()
            qty = int(line["qty"])
            item.reserved_stock += qty               # RESERVE — physical unchanged
            db.add(models.GmatsProformaLine(proforma_id=p.id, item_id=item.id, qty=qty))
        db.commit()
        return {"id": p.id, "proforma_no": p.proforma_no}

    @app.patch("/gmats/proformas/{pid}/cancel")
    def gmats_cancel_proforma(pid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        p = db.query(models.GmatsProforma).filter(models.GmatsProforma.id == pid).first()
        if not p or p.status != "Open":
            raise HTTPException(status_code=400, detail="Only open proformas can be cancelled")
        lines = db.query(models.GmatsProformaLine).filter(models.GmatsProformaLine.proforma_id == pid).all()
        for l in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == l.item_id).first()
            if item:
                item.reserved_stock = max(0, item.reserved_stock - l.qty)   # release reservation
        p.status = "Cancelled"
        db.commit()
        return {"ok": True}

    # ── Tax Invoice (final deduction) ─────────────────────────────

    @app.get("/gmats/invoices")
    def gmats_invoices(tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        rows = db.query(models.GmatsInvoice).filter(models.GmatsInvoice.tenant_code == tenant).order_by(models.GmatsInvoice.id.desc()).all()
        return [
            {"id": v.id, "invoice_no": v.invoice_no, "proforma_id": v.proforma_id,
             "customer_name": v.customer_name, "status": v.status, "created_at": v.created_at}
            for v in rows
        ]

    @app.post("/gmats/proformas/{pid}/invoice")
    def gmats_generate_invoice(pid: int, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        """Generate Tax Invoice from a proforma: deduct physical, clear the reservation."""
        p = db.query(models.GmatsProforma).filter(models.GmatsProforma.id == pid).first()
        if not p or p.status != "Open":
            raise HTTPException(status_code=400, detail="Only open proformas can be invoiced")
        lines = db.query(models.GmatsProformaLine).filter(models.GmatsProformaLine.proforma_id == pid).all()
        for l in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == l.item_id).first()
            if item:
                item.physical_stock = max(0, item.physical_stock - l.qty)   # DEDUCT physical
                item.reserved_stock = max(0, item.reserved_stock - l.qty)   # clear reservation
        count = db.query(models.GmatsInvoice).filter(models.GmatsInvoice.tenant_code == p.tenant_code).count()
        inv = models.GmatsInvoice(
            tenant_code=p.tenant_code,
            invoice_no=f"INV-{7000 + count + 1}",
            proforma_id=p.id,
            customer_name=p.customer_name,
            status="Generated",
        )
        db.add(inv)
        p.status = "Invoiced"
        db.commit()
        return {"id": inv.id, "invoice_no": inv.invoice_no}

    # ── Material Issue Note (free spares with a machine) ──────────

    @app.get("/gmats/min")
    def gmats_min_list(tenant: str = "GMATS", db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        rows = db.query(models.GmatsMIN).filter(models.GmatsMIN.tenant_code == tenant).order_by(models.GmatsMIN.id.desc()).all()
        items = {i.id: i for i in db.query(models.GmatsItem).all()}
        out = []
        for m in rows:
            lines = db.query(models.GmatsMINLine).filter(models.GmatsMINLine.min_id == m.id).all()
            out.append({
                "id": m.id, "min_no": m.min_no, "customer_name": m.customer_name,
                "machine_ref": m.machine_ref, "status": m.status, "created_at": m.created_at,
                "lines": [
                    {"item_id": l.item_id,
                     "item_name": items[l.item_id].item_name if l.item_id in items else "",
                     "qty": l.qty}
                    for l in lines
                ],
            })
        return out

    @app.post("/gmats/min")
    def gmats_create_min(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
        tenant = payload.get("tenant", "GMATS")
        lines = payload.get("lines", [])
        if not lines:
            raise HTTPException(status_code=400, detail="At least one spare line required")
        for line in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == int(line["item_id"])).first()
            if not item:
                raise HTTPException(status_code=404, detail="Item not found")
            qty = int(line["qty"])
            if qty > item.physical_stock:
                raise HTTPException(status_code=400, detail=f"Cannot issue {qty} {item.unit} of {item.item_name}: only {item.physical_stock} physical")
        count = db.query(models.GmatsMIN).filter(models.GmatsMIN.tenant_code == tenant).count()
        m = models.GmatsMIN(
            tenant_code=tenant,
            min_no=f"MIN-{4000 + count + 1}",
            customer_name=payload["customer_name"],
            machine_ref=payload.get("machine_ref", ""),
            status="Issued",
        )
        db.add(m); db.commit(); db.refresh(m)
        for line in lines:
            item = db.query(models.GmatsItem).filter(models.GmatsItem.id == int(line["item_id"])).first()
            qty = int(line["qty"])
            item.physical_stock = max(0, item.physical_stock - qty)   # deduct even though not billed
            db.add(models.GmatsMINLine(min_id=m.id, item_id=item.id, qty=qty))
        db.commit()
        return {"id": m.id, "min_no": m.min_no}


# ─────────────────────────────────────────────────────────────────
# Seed GMATS tenant with compressor inventory + aliases (idempotent)
# ─────────────────────────────────────────────────────────────────

def seed_gmats(db):
    if db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == "GMATS").count() > 0:
        return

    items = [
        # code, name, category, unit, physical, reorder, rate, location, supplier, [aliases]
        ("AF-001",  "Air Filter",                "Spares",     "Nos", 5,   10, 850,    "Spare Rack S1", "Mann Filters India",  ["Intake Filter", "Air Cleaner Element"]),
        ("OF-001",  "Oil Filter",                "Spares",     "Nos", 40,  15, 650,    "Spare Rack S1", "Mann Filters India",  ["Lube Filter", "Oil Element"]),
        ("OIL-46",  "Compressor Oil 46",         "Consumable", "L",   200, 50, 320,    "Lube Store",    "Castrol India",       ["Screw Oil 46", "Comp Oil 46", "Lubricant ISO 46"]),
        ("COL-1",   "1\" Collar",                "Fittings",   "Nos", 150, 30, 45,     "Bin B3",        "GI Fittings Co",      ["1\" Coupler", "GI Coupler 1\"", "Pipe Collar 1\""]),
        ("SV-001",  "Solenoid Valve",            "Spares",     "Nos", 12,  6,  1200,   "Spare Rack S2", "Rotex Automation",    ["Drain Solenoid", "Auto Drain Valve"]),
        ("SAE-20",  "Screw Air End 20HP",        "Assembly",   "Nos", 4,   2,  68000,  "Assembly Bay",  "GHH Rand",            ["Airend 20HP", "Compression Element 20HP"]),
        ("PS-001",  "Pressure Switch",           "Spares",     "Nos", 18,  8,  950,    "Spare Rack S2", "Danfoss India",       ["MPS Switch", "Cut-off Switch"]),
        ("VB-A50",  "V-Belt A50",                "Spares",     "Nos", 7,   10, 180,    "Spare Rack S3", "Gates India",         ["Drive Belt A50", "Fan Belt A50"]),
        ("SC-20HP", "20 HP Screw Compressor",    "Machine",    "Nos", 6,   2,  185000, "Finished Goods","GMATS (in-house)",    ["SAC-20", "20HP Screw Unit"]),
        ("RAD-01",  "Refrigerated Air Dryer",    "Machine",    "Nos", 3,   2,  42000,  "Finished Goods","GMATS (in-house)",    ["Ref Dryer", "Air Dryer Unit"]),
        ("ART-500", "Air Receiver Tank 500L",    "Machine",    "Nos", 8,   3,  28000,  "Finished Goods","GMATS (in-house)",    ["Air Tank 500L", "Receiver 500L"]),
        ("DV-001",  "Auto Drain Valve",          "Spares",     "Nos", 22,  10, 740,    "Spare Rack S2", "Rotex Automation",    ["Timer Drain", "Electronic Drain"]),
    ]

    for code, name, cat, unit, physical, reorder, rate, loc, sup, aliases in items:
        item = models.GmatsItem(
            tenant_code="GMATS", item_code=code, item_name=name, category=cat, unit=unit,
            physical_stock=physical, reserved_stock=0, reorder_level=reorder,
            purchase_rate=rate, location=loc, supplier=sup,
        )
        db.add(item); db.commit(); db.refresh(item)
        for a in aliases:
            db.add(models.GmatsAlias(tenant_code="GMATS", item_id=item.id, alias_name=a))
        db.commit()

    print("[SEED] GMATS compressor inventory")
