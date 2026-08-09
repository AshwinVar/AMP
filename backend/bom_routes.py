"""Bill-of-materials routes — a tenant maintains its own recipes (ADR-0013).

`GET /bom` moves here from core_routes, where it read the module-level PART_BOM
dict. Its shape changes because a real bill of materials has MANY components:
one row per COMPONENT rather than one row per part. `BomViewer.tsx` is updated
in the same change so the table groups them instead of duplicating React keys.

Access is deliberately unchanged from the old endpoint: reads and writes are
Admin. A recipe is what turns a completed work order into stock movement, so
editing one moves inventory for every future completion — that is not a
supervisor-level action, and widening the read at the same time as introducing
writes would be two access decisions hidden in one change.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from auth import require_roles
from database import SessionLocal
from payload_fields import str_field
from tenancy import request_tenant

router = APIRouter(tags=["Bill of Materials"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _quantity(value, where):
    """A component rate must be a positive, finite number.

    Float rather than int, because "1.5 kg of bar per shaft" is the most
    ordinary thing a bill of materials says and the dict this replaces could not
    express it. Zero and negative are rejected rather than stored: a zero rate
    describes no consumption (delete the line instead) and a negative one would
    make a completion INCREASE stock, which is the sign error ADR-0010 exists to
    keep out of the data.
    """
    try:
        q = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"{where}: quantity_per_unit must be a number")
    if q != q or q in (float("inf"), float("-inf")):
        raise HTTPException(status_code=400,
                            detail=f"{where}: quantity_per_unit must be finite")
    if q <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"{where}: quantity_per_unit must be greater than zero")
    return q


def _effective_from(value):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="effective_from must be YYYY-MM-DD")


def _serialise(bom, items_by_code):
    """One row per component, carrying the part-level fields with it.

    A part with no components still returns one row, with the component fields
    blank: a BOM that only produces (no consumption) is a real case, and
    dropping it from the response would make it invisible in the UI that is
    supposed to let someone fix it.
    """
    out_item = items_by_code.get(bom.output_item_code or "")
    base = {
        "id": bom.id,
        "part_number": bom.part_number,
        "revision": bom.revision,
        "active": bool(bom.active),
        "effective_from": bom.effective_from.isoformat() if bom.effective_from else None,
        "finished_goods_code": bom.output_item_code or "",
        "finished_goods_name": out_item.item_name if out_item else "",
        "notes": bom.notes or "",
        "updated_by": bom.updated_by or bom.created_by or "",
        "updated_at": bom.updated_at.isoformat() if bom.updated_at else None,
    }
    if not bom.components:
        return [dict(base, component_id=None, component_code="",
                     component_name="", quantity_per_unit=0.0, unit="",
                     component_stock=None, component_reorder_level=None)]
    rows = []
    for c in bom.components:
        item = items_by_code.get(c.component_code or "")
        rows.append(dict(base,
                         component_id=c.id,
                         component_code=c.component_code,
                         component_name=item.item_name if item else "",
                         quantity_per_unit=c.quantity_per_unit or 0.0,
                         unit=c.unit or (item.unit if item else ""),
                         # Carried so the viewer can keep its Stock Health
                         # column. None (not 0) when the recipe names an item
                         # this tenant does not stock — "unknown" and "empty"
                         # are different states and the UI renders them
                         # differently.
                         component_stock=item.current_stock if item else None,
                         component_reorder_level=item.reorder_level if item else None))
    return rows


def _items_by_code(db, tenant):
    return {i.item_code: i for i in db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == tenant).all()}


@router.get("/bom")
def list_bom(db: Session = Depends(_get_db),
             current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    boms = (db.query(models.BillOfMaterials)
            .filter(models.BillOfMaterials.tenant_code == tenant)
            .order_by(models.BillOfMaterials.part_number,
                      models.BillOfMaterials.revision)
            .all())
    items = _items_by_code(db, tenant)
    rows = []
    for b in boms:
        rows.extend(_serialise(b, items))
    return rows


@router.post("/bom")
def create_bom(payload: dict, db: Session = Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    part_number = str_field(payload, "part_number", "Bill of materials")
    revision = (payload.get("revision") or "A").strip() or "A"

    components = payload.get("components") or []
    if not isinstance(components, list):
        raise HTTPException(status_code=400, detail="components must be a list")

    # Validate EVERY line before writing any of them, so a bad line on row 4
    # does not leave rows 1-3 committed. The savepoint-per-row pattern used by
    # the CSV imports is right for a bulk upload of independent records; a BOM
    # is one document and a half-entered recipe is worse than a rejected one.
    parsed = []
    seen = set()
    for i, line in enumerate(components, start=1):
        where = f"Component {i}"
        if not isinstance(line, dict):
            raise HTTPException(status_code=400, detail=f"{where}: must be an object")
        code = str_field(line, "component_code", where)
        if code in seen:
            raise HTTPException(
                status_code=400,
                detail=f"{where}: {code} is listed twice; combine the quantities")
        seen.add(code)
        parsed.append({"component_code": code,
                       "quantity_per_unit": _quantity(line.get("quantity_per_unit"), where),
                       "unit": (line.get("unit") or "").strip()})

    bom = models.BillOfMaterials(
        tenant_code=tenant,
        part_number=part_number,
        revision=revision,
        output_item_code=(payload.get("output_item_code") or "").strip() or None,
        effective_from=_effective_from(payload.get("effective_from")),
        active=bool(payload.get("active", True)),
        notes=(payload.get("notes") or "").strip() or None,
        created_by=current_user.get("sub"),
        updated_by=current_user.get("sub"),
    )
    for p in parsed:
        bom.components.append(models.BomComponent(tenant_code=tenant, **p))
    db.add(bom)
    try:
        db.commit()
    except IntegrityError:
        # UNIQUE (tenant_code, part_number, revision). Caught rather than
        # pre-checked with a SELECT because a pre-check races: two admins
        # submitting revision B at once both see it free.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A bill of materials for '{part_number}' revision "
                   f"'{revision}' already exists.")
    db.refresh(bom)
    return _serialise(bom, _items_by_code(db, tenant))


@router.patch("/bom/{bom_id}")
def update_bom(bom_id: int, payload: dict, db: Session = Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    # Filtered by tenant EXPLICITLY as well as by id. The ADR-0002 hook would
    # scope this read inside a request, but a by-id handler that relies on the
    # ambient binding alone is one refactor away from being a cross-tenant edit.
    bom = (db.query(models.BillOfMaterials)
           .filter(models.BillOfMaterials.id == bom_id,
                   models.BillOfMaterials.tenant_code == tenant)
           .first())
    if bom is None:
        raise HTTPException(status_code=404, detail="Bill of materials not found")

    if "active" in payload:
        bom.active = bool(payload.get("active"))
    if "effective_from" in payload:
        bom.effective_from = _effective_from(payload.get("effective_from"))
    if "output_item_code" in payload:
        bom.output_item_code = (payload.get("output_item_code") or "").strip() or None
    if "notes" in payload:
        bom.notes = (payload.get("notes") or "").strip() or None

    if "components" in payload:
        lines = payload.get("components") or []
        if not isinstance(lines, list):
            raise HTTPException(status_code=400, detail="components must be a list")
        parsed, seen = [], set()
        for i, line in enumerate(lines, start=1):
            where = f"Component {i}"
            if not isinstance(line, dict):
                raise HTTPException(status_code=400, detail=f"{where}: must be an object")
            code = str_field(line, "component_code", where)
            if code in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"{where}: {code} is listed twice; combine the quantities")
            seen.add(code)
            parsed.append({"component_code": code,
                           "quantity_per_unit": _quantity(line.get("quantity_per_unit"), where),
                           "unit": (line.get("unit") or "").strip()})
        # Replace wholesale. A BOM is one document: sending five lines means the
        # recipe is those five, not those five plus whatever was there before.
        bom.components.clear()
        for p in parsed:
            bom.components.append(models.BomComponent(tenant_code=tenant, **p))

    bom.updated_by = current_user.get("sub")
    bom.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(bom)
    return _serialise(bom, _items_by_code(db, tenant))


@router.delete("/bom/{bom_id}")
def delete_bom(bom_id: int, db: Session = Depends(_get_db),
               current_user: dict = Depends(require_roles(["Admin"]))):
    tenant = request_tenant(current_user)
    bom = (db.query(models.BillOfMaterials)
           .filter(models.BillOfMaterials.id == bom_id,
                   models.BillOfMaterials.tenant_code == tenant)
           .first())
    if bom is None:
        raise HTTPException(status_code=404, detail="Bill of materials not found")
    db.delete(bom)      # components cascade (delete-orphan)
    db.commit()
    return {"message": "Bill of materials deleted"}
