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
import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from auth import get_current_user, require_roles
from database import SessionLocal
from money_redaction import redact_money
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


def _actor(current_user):
    """The signed-in user's name for the audit trail. The JWT carries it as
    `sub` in production; some harnesses pass `username`. Fall back to a marker
    rather than inventing a name — and never read it from client input."""
    user = current_user or {}
    return user.get("sub") or user.get("username") or "system"


# The fields a revision can move, in the order they read on the part master.
# part_code is the KEY that identifies the part, not a changed field — a row
# whose part_code "changed" is a different part, not an edit — so it is not here.
_AUDITED_FIELDS = ("part_name", "material", "part_weight_g", "cavities",
                   "active_cavities", "ideal_cycle_time_s", "price_per_piece",
                   "effective_from")


def _audit_value(spec, field):
    """One field of a spec, JSON-serialisable. effective_from is a date;
    everything else is already a str / int / float."""
    value = getattr(spec, field)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _diff_changes(prior, spec):
    """``{field: {"old": <was>, "new": <now>}}`` for a create or a revise.

    ``prior`` is None for the first spec of a part code, and then every field
    reads old=null — the trail says the part was created, not edited. For a
    revision only the fields that actually moved are recorded, against the spec
    that was current when the new one was entered.
    """
    changes = {}
    for field in _AUDITED_FIELDS:
        new = _audit_value(spec, field)
        old = _audit_value(prior, field) if prior is not None else None
        if prior is None or old != new:
            changes[field] = {"old": old, "new": new}
    return changes


def _audit_entry(entry):
    """One audit row as the API returns it: the acting user, the moment, and the
    field-level old->new of that one change."""
    try:
        changes = json.loads(entry.changes) if entry.changes else {}
    except (TypeError, ValueError):
        # A trail row whose JSON is somehow unreadable must still list: an empty
        # change set is better than a 500 that hides the whole history.
        changes = {}
    return {
        "part_code": entry.part_code,
        "part_name": entry.part_name,
        "actor": entry.actor,
        "at": entry.created_at.isoformat() if entry.created_at else None,
        "changes": changes,
    }


@router.get("/part-specs")
def list_part_specs(db: Session = Depends(_get_db),
                    current_user: dict = Depends(get_current_user)):
    """The CURRENT spec for every part this workspace has — one row each.

    ONE ROW PER PART, NOT PER REVISION. A spec is effective-dated and a revision
    is a NEW row (see create_part_spec), so a part revised a few times accrued a
    stack of rows that cluttered the master and buried the figure actually in
    force. This returns, per part code, the spec with the greatest effective_from
    that is ALREADY in force (`<= today`) — the one a reader is costing against
    right now. Every revision stays in the database and in the audit trail; this
    is a read, not a delete. A future-dated spec is held back until its date
    arrives, so a price agreed for next month does not restate this month.

    Readable by anyone signed in: an operator looking at an hourly target should
    be able to see the cycle time it came from. Only writing is Admin.
    """
    rows = (db.query(models.PartSpec)
              .filter(models.PartSpec.tenant_code == request_tenant(current_user))
              .order_by(models.PartSpec.part_code.asc(),
                        models.PartSpec.effective_from.desc(),
                        models.PartSpec.id.desc())
              .all())
    # The plant runs on IST; the server clock is UTC (Railway). For the 5.5h
    # after IST midnight the server day is still yesterday, which would hold back
    # a spec that is in force today while the board — which renders in IST (#749)
    # — already costs at the new price. Resolve "today" in IST so the master and
    # the board agree on the day a spec takes effect.
    today = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
    current = {}
    for r in rows:
        if r.part_code in current:
            continue  # rows are effective_from-desc, so the first kept is the latest
        # A spec dated in the future is not yet in force: the figure a reader is
        # costing against is still the previous one, so skip it until its day.
        if r.effective_from and r.effective_from <= today:
            current[r.part_code] = r
    # An operator may read a spec for its cycle time, but price_per_piece — the
    # figure that turns shots into rupees — is Admin-only, so it is omitted from
    # every row for a non-Admin (money_redaction). Writing a spec is already Admin.
    return redact_money(
        {"specs": [_row(r) for r in current.values()],
         # Materials for the dropdown come from EVERY spec, not only the
         # current ones: a grade used on a now-superseded spec is still a grade
         # this plant buys, and dropping it would make a reviser retype it.
         "materials": sorted({r.material for r in rows})},
        current_user)


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
    # The spec that is current for this part BEFORE this write — what the new row
    # is a change FROM. Read now, while it is still the latest; None for a brand
    # new part, which makes every field read old=null in the trail.
    prior = (db.query(models.PartSpec)
               .filter(models.PartSpec.tenant_code == tenant,
                       models.PartSpec.part_code == spec.part_code)
               .order_by(models.PartSpec.effective_from.desc(),
                         models.PartSpec.id.desc())
               .first())
    db.add(spec)
    try:
        # Flush (not commit) so a duplicate effective_from still raises here, yet
        # the spec and its audit row land in ONE transaction at the commit below:
        # the trail can never lose the one change whose second commit failed.
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"a spec for {spec.part_code} already starts on "
                    f"{spec.effective_from}. Use a different date to supersede it."))
    # Record the change in the same transaction as the spec. The entry would be a
    # lie only for a write that was refused — and a refused spec never reaches
    # here (the 409 above). tenant_code and actor are set explicitly: the trail of
    # a price change is commercial data and must carry WHO even when no request
    # tenant is bound (a direct call in a test). `prior` was read before the
    # flush, so it is the spec this one supersedes, not this one.
    db.add(models.PartSpecAudit(
        tenant_code=tenant,
        part_code=spec.part_code,
        part_name=spec.part_name,
        actor=_actor(current_user),
        changes=json.dumps(_diff_changes(prior, spec)),
    ))
    db.commit()
    db.refresh(spec)
    return _row(spec)


@router.get("/part-spec-audit")
def list_part_spec_audit(part_code: str | None = None,
                         db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(["Admin"]))):
    """The part master's change history, newest first — who changed what, when.

    ADMIN ONLY, like the writes it records. A spec change restates the plant's
    revenue and output, and the trail of those changes is the same commercial
    data: an operator who may not see a price may not see its history either.
    Scoped to the caller's tenant; an optional `part_code` narrows it to one
    part. Each entry carries the field-level old->new of that single change,
    with old=null throughout for the row that first created a part.
    """
    tenant = request_tenant(current_user)
    query = (db.query(models.PartSpecAudit)
               .filter(models.PartSpecAudit.tenant_code == tenant))
    code = str(part_code or "").strip()
    if code:
        query = query.filter(models.PartSpecAudit.part_code == code)
    rows = query.order_by(models.PartSpecAudit.created_at.desc(),
                          models.PartSpecAudit.id.desc()).all()
    return {"entries": [_audit_entry(r) for r in rows]}


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

