"""Factory-ops routes — escalations, factory layout, documents, maintenance, notifications.

The day-to-day factory operations CRUD: escalation tracking (the raise-from-smart-alerts endpoint stays in
main — it shares main's generate_alerts helper), the digital-twin floor layout nodes (incl. auto-generate),
controlled compliance documents, maintenance tasks, and notifications — each
with a "generate escalations/notifications" helper endpoint. Peeled out of
main.py per ADR-0009. Plain CRUD; no event-bus coupling.
"""
from datetime import datetime, timedelta
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


router = APIRouter(tags=["Factory Ops"])


@router.get("/escalations", response_model=List[schemas.EscalationResponse])
def get_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.Escalation)
        .order_by(models.Escalation.id.desc())
        .limit(300)
        .all()
    )


@router.post("/escalations", response_model=schemas.EscalationResponse)
def create_escalation(
    escalation: schemas.EscalationCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    if escalation.machine_id:
        machine = (
            db.query(models.Machine)
            .filter(models.Machine.id == escalation.machine_id)
            .first()
        )
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    new_escalation = models.Escalation(**escalation.model_dump())
    db.add(new_escalation)
    db.commit()
    db.refresh(new_escalation)

    return new_escalation


@router.patch("/escalations/{escalation_id}", response_model=schemas.EscalationResponse)
def update_escalation(
    escalation_id: int,
    payload: schemas.EscalationUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    escalation = (
        db.query(models.Escalation)
        .filter(models.Escalation.id == escalation_id)
        .first()
    )

    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    if payload.status is not None:
        escalation.status = payload.status
        if payload.status == "Resolved" and escalation.resolved_at is None:
            escalation.resolved_at = datetime.utcnow()

    if payload.owner is not None:
        escalation.owner = payload.owner

    if payload.department is not None:
        escalation.department = payload.department

    if payload.resolution_notes is not None:
        escalation.resolution_notes = payload.resolution_notes

    db.commit()
    db.refresh(escalation)

    return escalation


@router.delete("/escalations/{escalation_id}")
def delete_escalation(
    escalation_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    escalation = (
        db.query(models.Escalation)
        .filter(models.Escalation.id == escalation_id)
        .first()
    )

    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    db.delete(escalation)
    db.commit()

    return {"message": "Escalation deleted successfully"}


@router.get("/factory-layout/nodes", response_model=List[schemas.FactoryLayoutNodeResponse])
def get_factory_layout_nodes(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.FactoryLayoutNode).order_by(models.FactoryLayoutNode.id.asc()).all()


@router.post("/factory-layout/nodes", response_model=schemas.FactoryLayoutNodeResponse)
def create_factory_layout_node(
    node: schemas.FactoryLayoutNodeCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    if node.machine_id:
        machine = db.query(models.Machine).filter(models.Machine.id == node.machine_id).first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    new_node = models.FactoryLayoutNode(**node.model_dump())
    db.add(new_node)
    db.commit()
    db.refresh(new_node)
    return new_node


@router.patch("/factory-layout/nodes/{node_id}", response_model=schemas.FactoryLayoutNodeResponse)
def update_factory_layout_node(
    node_id: int,
    payload: schemas.FactoryLayoutNodeUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    node = db.query(models.FactoryLayoutNode).filter(models.FactoryLayoutNode.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Factory layout node not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("machine_id"):
        machine = db.query(models.Machine).filter(models.Machine.id == data["machine_id"]).first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    for key, value in data.items():
        setattr(node, key, value)

    db.commit()
    db.refresh(node)
    return node


@router.delete("/factory-layout/nodes/{node_id}")
def delete_factory_layout_node(
    node_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    node = db.query(models.FactoryLayoutNode).filter(models.FactoryLayoutNode.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Factory layout node not found")

    db.delete(node)
    db.commit()
    return {"message": "Factory layout node deleted successfully"}


@router.post("/factory-layout/auto-generate")
def auto_generate_factory_layout(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machines = db.query(models.Machine).order_by(models.Machine.id.asc()).all()
    existing_count = db.query(models.FactoryLayoutNode).count()

    if existing_count > 0:
        return {"created": 0, "message": "Layout already exists"}

    created = 0
    x = 40
    y = 50
    col = 0

    for machine in machines:
        node = models.FactoryLayoutNode(
            machine_id=machine.id,
            node_name=machine.name,
            node_type="Machine",
            x_position=x,
            y_position=y,
            width=180,
            height=110,
            zone="Production",
        )
        db.add(node)
        created += 1
        col += 1
        x += 220

        if col >= 4:
            col = 0
            x = 40
            y += 160

    db.commit()
    return {"created": created}


@router.get("/documents", response_model=List[schemas.ComplianceDocumentResponse])
def get_documents(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.ComplianceDocument).order_by(models.ComplianceDocument.id.desc()).limit(500).all()


@router.post("/documents", response_model=schemas.ComplianceDocumentResponse)
def create_document(
    document: schemas.ComplianceDocumentCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.document_no == document.document_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Document number already exists")

    new_document = models.ComplianceDocument(**document.model_dump())
    db.add(new_document)
    db.commit()
    db.refresh(new_document)
    return new_document


@router.patch("/documents/{document_id}", response_model=schemas.ComplianceDocumentResponse)
def update_document(
    document_id: int,
    payload: schemas.ComplianceDocumentUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    document = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(document, key, value)

    db.commit()
    db.refresh(document)
    return document


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    document = db.query(models.ComplianceDocument).filter(models.ComplianceDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    db.delete(document)
    db.commit()
    return {"message": "Document deleted successfully"}


@router.post("/documents/generate-review-escalations")
def generate_document_review_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()
    documents = (
        db.query(models.ComplianceDocument)
        .filter(
            models.ComplianceDocument.review_due_date < today,
            models.ComplianceDocument.approval_status != "Obsolete",
        )
        .all()
    )

    created = 0

    for document in documents:
        title = f"Document review overdue: {document.document_no}"

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
            severity="Medium",
            owner=document.owner,
            department=document.department,
            status="Open",
            source="Compliance",
            notes=f"{document.title} review was due on {document.review_due_date}",
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}


@router.get("/maintenance/tasks", response_model=List[schemas.MaintenanceTaskResponse])
def get_maintenance_tasks(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(models.MaintenanceTask).order_by(models.MaintenanceTask.id.desc()).limit(500).all()


@router.post("/maintenance/tasks", response_model=schemas.MaintenanceTaskResponse)
def create_maintenance_task(
    task: schemas.MaintenanceTaskCreate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    existing = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.task_no == task.task_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Task number already exists")

    machine = db.query(models.Machine).filter(models.Machine.id == task.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    new_task = models.MaintenanceTask(**task.model_dump())
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    return new_task


@router.patch("/maintenance/tasks/{task_id}", response_model=schemas.MaintenanceTaskResponse)
def update_maintenance_task(
    task_id: int,
    payload: schemas.MaintenanceTaskUpdate,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    task = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Maintenance task not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, key, value)

    if task.status == "Completed" and task.completed_date is None:
        task.completed_date = datetime.utcnow().date()

    db.commit()
    db.refresh(task)
    return task


@router.delete("/maintenance/tasks/{task_id}")
def delete_maintenance_task(
    task_id: int,
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    task = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Maintenance task not found")

    db.delete(task)
    db.commit()
    return {"message": "Maintenance task deleted successfully"}


@router.post("/maintenance/generate-overdue-escalations")
def generate_maintenance_overdue_escalations(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    today = datetime.utcnow().date()
    tasks = (
        db.query(models.MaintenanceTask)
        .filter(
            models.MaintenanceTask.planned_date < today,
            models.MaintenanceTask.status != "Completed",
        )
        .all()
    )

    created = 0

    for task in tasks:
        title = f"Maintenance overdue: {task.task_no}"
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
            machine_id=task.machine_id,
            title=title,
            severity="High" if task.priority in ["Critical", "High"] else "Medium",
            owner=task.assigned_to,
            department="Maintenance",
            status="Open",
            source="Maintenance",
            notes=f"{task.task_type} task was planned for {task.planned_date}",
        )

        db.add(escalation)
        created += 1

    db.commit()
    return {"created": created}


@router.get("/notifications", response_model=List[schemas.NotificationResponse])
def get_notifications(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.Notification).order_by(models.Notification.id.desc()).limit(500).all()


@router.post("/notifications", response_model=schemas.NotificationResponse)
def create_notification(payload: schemas.NotificationCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    row = models.Notification(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/notifications/{notification_id}", response_model=schemas.NotificationResponse)
def update_notification(notification_id: int, payload: schemas.NotificationUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.Notification).filter(models.Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    db.refresh(row)
    return row


@router.post("/notifications/generate-system-notifications")
def generate_system_notifications(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    created = 0

    def add_notification(kind, severity, title, message):
        nonlocal created
        existing = db.query(models.Notification).filter(models.Notification.title == title, models.Notification.status != "Read").first()
        if existing:
            return
        db.add(models.Notification(
            notification_type=kind,
            severity=severity,
            title=title,
            message=message,
            status="Unread",
        ))
        created += 1

    breakdowns = db.query(models.Machine).filter(models.Machine.status == "Breakdown").all()
    for machine in breakdowns:
        add_notification("Machine", "Critical", f"Machine breakdown: {machine.name}", f"{machine.name} is currently in Breakdown state.")

    open_escalations = db.query(models.Escalation).filter(models.Escalation.status != "Resolved").all()
    if len(open_escalations) > 0:
        add_notification("Escalation", "Warning", "Open escalations pending", f"{len(open_escalations)} escalation(s) still require action.")

    low_stock = db.query(models.InventoryItem).filter(models.InventoryItem.current_stock <= models.InventoryItem.reorder_level).all()
    if len(low_stock) > 0:
        add_notification("Inventory", "Warning", "Low stock items detected", f"{len(low_stock)} item(s) are below reorder level.")

    db.commit()
    return {"created": created}
