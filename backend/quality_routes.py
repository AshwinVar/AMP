"""Quality routes — inspections and defect escalations.

Quality inspections (list / create / update / delete). One behaviour preserved
exactly: recording a failed inspection publishes a QualityInspectionFailed
domain event (ADR-0001/0003) on the request DB session so subscribers react and
commit atomically. Also exposes the defect escalation generator (builds
models.Escalation rows directly; self-contained). Peeled out of main.py per
ADR-0009.
"""
from typing import List

from fastapi import Depends, HTTPException
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


def register(app):
    @app.get("/quality/inspections", response_model=List[schemas.QualityInspectionResponse])
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

    @app.post("/quality/inspections", response_model=schemas.QualityInspectionResponse)
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

        db.commit()
        db.refresh(new_inspection)

        return new_inspection

    @app.patch("/quality/inspections/{inspection_id}", response_model=schemas.QualityInspectionResponse)
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

        if inspection.passed_quantity + inspection.failed_quantity > inspection.inspected_quantity:
            raise HTTPException(
                status_code=400,
                detail="passed_quantity + failed_quantity cannot exceed inspected_quantity",
            )

        db.commit()
        db.refresh(inspection)

        return inspection

    @app.delete("/quality/inspections/{inspection_id}")
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

    @app.post("/quality/generate-defect-escalations")
    def generate_defect_escalations(
        db: Session = Depends(_get_db),
        current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
    ):
        inspections = db.query(models.QualityInspection).all()
        created = 0

        for inspection in inspections:
            if inspection.inspected_quantity <= 0:
                continue

            fail_rate = (inspection.failed_quantity / inspection.inspected_quantity) * 100

            if fail_rate < 10 and inspection.scrap_quantity <= 0:
                continue

            title = f"Quality issue: {inspection.inspection_no}"

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
                    f"scrap {inspection.scrap_quantity}; rework {inspection.rework_quantity}"
                ),
            )

            db.add(escalation)
            created += 1

        db.commit()

        return {"created": created}

