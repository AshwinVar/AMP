"""Quality routes — inspections and defect escalations.

Quality inspections (list / create / update / delete). One behaviour preserved
exactly: recording a failed inspection publishes a QualityInspectionFailed
domain event (ADR-0001/0003) on the request DB session so subscribers react and
commit atomically. Also exposes the defect escalation generator (builds
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
from auth import get_current_user, require_roles
from database import SessionLocal
from events import event_bus, QualityInspectionFailed
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/quality", tags=["Quality"])


@router.get("/inspections", response_model=List[schemas.QualityInspectionResponse])
def get_quality_inspections(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.QualityInspection)
        .order_by(models.QualityInspection.id.desc())
        .limit(300)
        .all()
    )


@router.post("/inspections", response_model=schemas.QualityInspectionResponse)
def create_quality_inspection(
    inspection: schemas.QualityInspectionCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    existing = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.inspection_no == inspection.inspection_no)
        .first()
    )

    if existing:
        raise HTTPException(status_code=400, detail="Inspection number already exists")

    if inspection.machine_id:
        machine = (
            db.query(models.Machine)
            .filter(models.Machine.id == inspection.machine_id)
            .first()
        )
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    if inspection.work_order_id:
        work_order = (
            db.query(models.WorkOrder)
            .filter(models.WorkOrder.id == inspection.work_order_id)
            .first()
        )
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if inspection.production_plan_id:
        production_plan = (
            db.query(models.ProductionPlan)
            .filter(models.ProductionPlan.id == inspection.production_plan_id)
            .first()
        )
        if not production_plan:
            raise HTTPException(status_code=404, detail="Production plan not found")

    # Reject physically-impossible rows at the boundary, the same guard the
    # production-record ingest already applies (#266). The upper-bound invariant
    # below only stops passed+failed OVERSHOOTING inspected; it does nothing about
    # a NEGATIVE count, and a negative one silently corrupts every quality window
    # that sums these columns — a negative failed_quantity drives the fail rate
    # (failed/inspected) below zero and the quality rate (good/total) above 100%,
    # violating the honesty rule (a metric can't exceed the bound the data
    # supports). The schema types are plain ints, so nothing else stops them.
    if min(inspection.inspected_quantity, inspection.passed_quantity,
           inspection.failed_quantity, inspection.rework_quantity,
           inspection.scrap_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

    if inspection.passed_quantity + inspection.failed_quantity > inspection.inspected_quantity:
        raise HTTPException(
            status_code=400,
            detail="passed_quantity + failed_quantity cannot exceed inspected_quantity",
        )

    new_inspection = models.QualityInspection(**inspection.model_dump())
    db.add(new_inspection)

    # Widen the event stream: a quality inspection recorded failures (ADR-0003).
    if (new_inspection.failed_quantity or 0) > 0:
        event_bus.publish(QualityInspectionFailed(
            tenant_code=request_tenant(current_user),
            inspection_no=new_inspection.inspection_no,
            failed_quantity=new_inspection.failed_quantity,
            inspected_quantity=new_inspection.inspected_quantity,
            machine_id=new_inspection.machine_id,
            work_order_id=new_inspection.work_order_id,
            defect_category=new_inspection.defect_category,
        ), db)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Inspection number is already in use")
    db.refresh(new_inspection)

    return new_inspection


@router.patch("/inspections/{inspection_id}", response_model=schemas.QualityInspectionResponse)
def update_quality_inspection(
    inspection_id: int,
    payload: schemas.QualityInspectionUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    inspection = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.id == inspection_id)
        .first()
    )

    if not inspection:
        raise HTTPException(status_code=404, detail="Quality inspection not found")

    data = payload.model_dump(exclude_unset=True)

    for key, value in data.items():
        setattr(inspection, key, value)

    # passed/failed/rework/scrap are Column(Integer, default=0) WITHOUT nullable=False,
    # and QualityInspectionUpdate types each Optional[int]=None — so a client can PATCH
    # an explicit `{"passed_quantity": null}` (exclude_unset keeps an explicit null) and
    # a legacy/raw-SQL row can already carry NULL. Left as None, the invariant check
    # below did `None + failed` (TypeError -> unhandled 500) and, even past that, the
    # response 500'd anyway because QualityInspectionResponse types these fields as
    # non-optional int. A NULL count is the column's own default of 0 — coalesce before
    # the check and the return, the same `or 0` the sibling generate_defect_escalations
    # (#287) and the analytics rollups already apply to these very columns. This also
    # heals a pre-existing NULL row on any update. inspected_quantity is nullable=False
    # and not settable via this schema, so it needs no guard.
    inspection.passed_quantity = inspection.passed_quantity or 0
    inspection.failed_quantity = inspection.failed_quantity or 0
    inspection.rework_quantity = inspection.rework_quantity or 0
    inspection.scrap_quantity = inspection.scrap_quantity or 0

    # Reject negative counts here too (parity with create / the production-record
    # ingest #266). A NULL heals to 0 above and is fine; a NEGATIVE patch value
    # (`{"failed_quantity": -5}`) is physically impossible and would corrupt the
    # fail/quality-rate windows exactly as it would on create — so a clean 400,
    # not a stored negative. inspected_quantity isn't settable via this schema and
    # is nullable=False, so it's always a real int; including it is harmless.
    if min(inspection.inspected_quantity, inspection.passed_quantity,
           inspection.failed_quantity, inspection.rework_quantity,
           inspection.scrap_quantity) < 0:
        raise HTTPException(status_code=400, detail="quantities must be non-negative")

    if inspection.passed_quantity + inspection.failed_quantity > inspection.inspected_quantity:
        raise HTTPException(
            status_code=400,
            detail="passed_quantity + failed_quantity cannot exceed inspected_quantity",
        )

    db.commit()
    db.refresh(inspection)

    return inspection


@router.delete("/inspections/{inspection_id}")
def delete_quality_inspection(
    inspection_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    inspection = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.id == inspection_id)
        .first()
    )

    if not inspection:
        raise HTTPException(status_code=404, detail="Quality inspection not found")

    db.delete(inspection)
    db.commit()

    return {"message": "Quality inspection deleted successfully"}


@router.post("/generate-defect-escalations")
def generate_defect_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    # Bound the scan in SQL — only inspections that can actually raise an
    # escalation come back, like the sibling generators (document-review,
    # maintenance-overdue, late-order) which filter in SQL rather than pulling a
    # growing table into Python. An escalation fires when inspected_quantity > 0
    # AND (fail rate >= 10% OR there was scrap). "fail rate >= 10%" is
    # failed/inspected*100 >= 10, i.e. failed*10 >= inspected (integer-exact, no
    # rounding), so the SQL predicate matches the Python threshold below exactly.
    # NULL failed/scrap columns make the predicate false and are simply excluded —
    # matching the "no escalation" intent, and sidestepping a None comparison.
    inspections = (
        db.query(models.QualityInspection)
        .filter(
            models.QualityInspection.inspected_quantity > 0,
            or_(
                models.QualityInspection.failed_quantity * 10
                >= models.QualityInspection.inspected_quantity,
                models.QualityInspection.scrap_quantity > 0,
            ),
        )
        .all()
    )
    created = 0

    for inspection in inspections:
        # failed/scrap/rework are Column(Integer, default=0) WITHOUT nullable=False,
        # so a row written by raw SQL / a migration / an update that clears the field
        # can legitimately be NULL. The SQL filter above selects a row when
        # scrap_quantity > 0 REGARDLESS of failed_quantity, so an inspection with
        # recorded scrap but a NULL failed_quantity reaches here — and the old
        # `None / inspected_quantity` raised TypeError, 500-ing the whole generator.
        # A NULL count is the column's own default of 0, so coalesce before dividing
        # and rendering (matching the _int coalesce used across the engines).
        failed = inspection.failed_quantity or 0
        scrap = inspection.scrap_quantity or 0
        rework = inspection.rework_quantity or 0
        # inspected_quantity > 0 is guaranteed by the query, so the divide is safe.
        fail_rate = (failed / inspection.inspected_quantity) * 100

        title = f"Quality issue: {inspection.inspection_no}"

        # Dedup against an already-OPEN escalation with the same title. Escalation.status
        # is Column(String, default="Open") WITHOUT nullable=False, so a raw-SQL /
        # migration / cleared-field row can carry a genuine NULL. In SQL a bare
        # `status != 'Resolved'` is NULL — not TRUE — for such a row, so it did NOT find
        # a NULL-status open escalation and raised a DUPLICATE, inflating the very
        # open-escalation count every reader reports (analytics /escalations,
        # system-notifications, tenant-activity all count a NULL status as still open).
        # OR the NULL back in so a NULL-status open escalation blocks the duplicate; a
        # Resolved one still lets a fresh recurrence through. (Mirrors #425, which widened
        # the other five generators but missed this one.)
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
            machine_id=inspection.machine_id,
            title=title,
            severity="Critical" if fail_rate >= 20 else "High",
            owner="Quality Lead",
            department="Quality",
            status="Open",
            source="Quality",
            notes=(
                f"Fail rate {round(fail_rate, 1)}%; "
                f"defect category {inspection.defect_category or 'N/A'}; "
                f"scrap {scrap}; rework {rework}"
            ),
        )

        db.add(escalation)
        created += 1

    db.commit()

    return {"created": created}
