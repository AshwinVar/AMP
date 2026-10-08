"""Part master routes — the figures no controller holds, typed in once.

WHY THESE ARE ADMIN. A part spec is what turns a shot count into kilograms and
money for every hour the plant runs. Changing `price_per_piece` restates the
plant's revenue; changing `active_cavities` restates its output. Those are not
supervisor-level edits, and they are exactly the figures a plant will argue
about later, so writes are Admin and every change leaves a new effective-dated
row rather than overwriting the old one.

WHY A NEW ROW RATHER THAN AN EDIT. The customer's own part table has a "From
date" column, which is the same instinct: a price is a fact about a period. If
editing a spec changed the row in place, last month's revenue would move because
this month's price did, and a report already sent to a customer would stop
reconciling. POST with a new `effective_from` supersedes; it does not overwrite.

WHAT IS REFUSED RATHER THAN COERCED. A weight of zero makes a month's material
consumption vanish. A cycle time of zero makes the ideal rate infinite. Active
cavities above total cavities is a typo that silently overstates output. Each is
a 400 naming the field, because a part master entered wrongly is not discovered
until someone disputes an invoice.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant

router = APIRouter(tags=["Part master"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _text(value, field, limit=120):
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    if len(text) > limit:
        raise HTTPException(status_code=400,
                            detail=f"{field} is longer than {limit} characters")
    return text


def _positive(value, field, allow_zero=False):
    """A finite number above zero. NaN and infinity print as figures and mean
    nothing, which is the same rule ev.Fact keeps one layer up."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number, got {value!r}")
    if number != number or number in (float("inf"), float("-inf")):
        raise HTTPException(status_code=400, detail=f"{field} must be a finite number")
    if number < 0 or (number == 0 and not allow_zero):
        raise HTTPException(
            status_code=400,
            detail=(f"{field} must be greater than zero" if not allow_zero
                    else f"{field} cannot be negative"))
    return number


def _as_date(value, field):
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"{field} must be YYYY-MM-DD, got {value!r}")


def _row(spec):
    return {
        "id": spec.id,
        "part_code": spec.part_code,
        "part_name": spec.part_name,
        "material": spec.material,
        "part_weight_g": spec.part_weight_g,
        "cavities": spec.cavities,
        "active_cavities": spec.active_cavities,
        "ideal_cycle_time_s": spec.ideal_cycle_time_s,
        "price_per_piece": spec.price_per_piece,
        "effective_from": spec.effective_from.isoformat() if spec.effective_from else None,
        "ideal_parts_per_hour": round(spec.ideal_parts_per_hour),
    }


@router.get("/part-specs")
def list_part_specs(db: Session = Depends(_get_db),
                    current_user: dict = Depends(get_current_user)):
    """Every spec this workspace has, newest effective date first.

    Readable by anyone signed in: an operator looking at an hourly target should
    be able to see the cycle time it came from. Only writing is Admin.
    """
    rows = (db.query(models.PartSpec)
              .filter(models.PartSpec.tenant_code == request_tenant(current_user))
              .order_by(models.PartSpec.part_code.asc(),
                        models.PartSpec.effective_from.desc())
              .all())
    return {"specs": [_row(r) for r in rows],
            "materials": sorted({r.material for r in rows})}


@router.post("/part-specs", status_code=201)
def create_part_spec(body: dict, db: Session = Depends(_get_db),
                     current_user: dict = Depends(require_roles(["Admin"]))):
    """Add a spec, or supersede one from a given date."""
    tenant = request_tenant(current_user)
    cavities = int(_positive(body.get("cavities", 1), "cavities"))
    active = int(_positive(body.get("active_cavities", cavities), "active_cavities"))
    if active > cavities:
        # The typo that silently overstates output for the life of the tool.
        raise HTTPException(
            status_code=400,
            detail=(f"active_cavities ({active}) cannot exceed cavities ({cavities}) — "
                    f"a mould cannot run more cavities than it has"))

    spec = models.PartSpec(
        tenant_code=tenant,
        part_code=_text(body.get("part_code"), "part_code", 40),
        part_name=_text(body.get("part_name"), "part_name"),
        material=_text(body.get("material"), "material"),
        part_weight_g=_positive(body.get("part_weight_g"), "part_weight_g"),
        cavities=cavities,
        active_cavities=active,
        ideal_cycle_time_s=_positive(body.get("ideal_cycle_time_s"), "ideal_cycle_time_s"),
        # Zero is allowed: a plant may track output before it agrees a price, and
        # the board reports those parts as unpriced rather than refusing them.
        price_per_piece=_positive(body.get("price_per_piece", 0), "price_per_piece",
                                  allow_zero=True),
        effective_from=_as_date(body.get("effective_from") or date.today().isoformat(),
                                "effective_from"),
    )
    db.add(spec)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"a spec for {spec.part_code} already starts on "
                    f"{spec.effective_from}. Use a different date to supersede it."))
    db.refresh(spec)
    return _row(spec)


@router.get("/tool-assignments")
def list_tool_assignments(db: Session = Depends(_get_db),
                          current_user: dict = Depends(get_current_user)):
    """Which mould is on which machine, and what it makes.

    This is the mould-change record. The customer changes tooling roughly once a
    fortnight, so this is a handful of entries a month — not the per-hour entry
    screen their spreadsheet drew, which nobody on an unmanned floor would fill.
    """
    tenant = request_tenant(current_user)
    tools = (db.query(models.ToolAsset)
               .filter(models.ToolAsset.tenant_code == tenant)
               .order_by(models.ToolAsset.tool_no.asc()).all())
    machines = {m.id: m.name for m in db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant).all()}
    return {"tools": [{
        "id": t.id, "tool_no": t.tool_no, "name": t.name,
        "machine_id": t.machine_id,
        "machine": machines.get(t.machine_id),
        "part_code": t.part_code,
        "cavities": t.cavities,
        "status": t.status,
        "cycles_total": t.cycles_total,
        "cycles_since_service": t.cycles_since_service,
        "service_interval_cycles": t.service_interval_cycles,
    } for t in tools],
        "machines": [{"id": i, "name": n} for i, n in sorted(machines.items(),
                                                             key=lambda kv: kv[1])]}


@router.post("/tool-assignments/{tool_id}")
def assign_tool(tool_id: int, body: dict, db: Session = Depends(_get_db),
                current_user: dict = Depends(require_roles(["Admin"]))):
    """Record a mould change: which machine this tool is now on, and its part."""
    tenant = request_tenant(current_user)
    tool = (db.query(models.ToolAsset)
              .filter(models.ToolAsset.tenant_code == tenant,
                      models.ToolAsset.id == tool_id).first())
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {tool_id} in this workspace")

    if "machine_id" in body:
        machine_id = body.get("machine_id")
        if machine_id in (None, "", 0):
            tool.machine_id = None          # back to the tool room
        else:
            machine = (db.query(models.Machine)
                         .filter(models.Machine.tenant_code == tenant,
                                 models.Machine.id == int(machine_id)).first())
            if machine is None:
                # Scoped by tenant, so a tool can never be fitted to another
                # workspace's machine by guessing an id.
                raise HTTPException(status_code=400,
                                    detail=f"no machine {machine_id} in this workspace")
            tool.machine_id = machine.id
    if "part_code" in body:
        code = str(body.get("part_code") or "").strip()
        tool.part_code = code or None
    db.commit()
    db.refresh(tool)
    return {"id": tool.id, "tool_no": tool.tool_no, "machine_id": tool.machine_id,
            "part_code": tool.part_code}

