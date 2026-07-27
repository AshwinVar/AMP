"""Reporting routes — CSV/text exports and report-request records.

Operational exports (downtime / shifts / OEE as CSV, the intelligence summary as
text) plus the report-request log (list / create). All compute comes from the
shared engines — analytics_engine (build_* + calculate_oee_from_record) and
report_generator (build_daily_summary_text) — so nothing here couples back to
main. Peeled out of main.py per ADR-0009.

Note: /reports/daily-summary.txt deliberately stays in main; it calls the
/analytics/summary endpoint function directly, which will move only when the
analytics-summary compute is factored out of main into the shared engine.
"""
import csv
import io
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
from analytics_engine import (
    build_management_summary,
    build_shift_kpis,
    build_smart_alerts,
    calculate_oee_from_record,
)
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant, tenant_unit_value
from report_generator import build_daily_summary_text


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/reports", tags=["Reports"])


def _machine_names(db) -> dict:
    """{machine_id: name} for the (tenant-scoped) machine roster, in one query.

    The CSV exports below resolve a machine name per row. Looking that name up
    with a fresh ``db.query(Machine)`` inside the row loop is an N+1 that grows
    with the (unbounded) downtime / production tables — thousands of point
    lookups for one export. The machine roster itself is small and bounded, so
    fetch it once as a map and read names from memory. Matches the N+1 fixes
    already applied to the analytics endpoints (e.g. #273, #276)."""
    return dict(db.query(models.Machine.id, models.Machine.name).all())


@router.get("/downtime.csv")
def export_downtime_csv(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    logs = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.asc()).all()
    names = _machine_names(db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "reason", "duration", "notes", "created_at"])
    for log in logs:
        writer.writerow([log.id, log.machine_id, names.get(log.machine_id) or "", log.reason, log.duration, log.notes or "", log.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=downtime_report.csv"})


@router.get("/shifts.csv")
def export_shifts_csv(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    shifts = db.query(models.ShiftData).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "shift_name", "target_output", "actual_output", "efficiency_percent", "created_at"])
    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        writer.writerow([shift.id, shift.shift_name, shift.target_output, shift.actual_output, efficiency, shift.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=shift_report.csv"})


# ── Operational exports ─────────────────────────────────────────────
# The tables a manufacturer's back office actually lives in — exported in the
# same shape as the classic three above: role-gated (Admin/Supervisor),
# auto-scoped to the tenant (ADR-0002), machine/supplier names resolved from one
# bounded map (never per-row N+1), stable id-ascending order, and an empty table
# yielding a header-only CSV rather than an error.


def _csv(headers, rows, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(content=output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/work-orders.csv")
def export_work_orders_csv(db: Session = Depends(_get_db),
                           current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.WorkOrder).order_by(models.WorkOrder.id.asc()).all()
    names = _machine_names(db)
    return _csv(
        ["id", "work_order_no", "part_number", "batch_number", "machine_id", "machine_name",
         "target_quantity", "actual_quantity", "status", "material_state",
         "planned_start", "planned_end", "created_at"],
        [[w.id, w.work_order_no, w.part_number, w.batch_number, w.machine_id,
          names.get(w.machine_id) or "", w.target_quantity, w.actual_quantity or 0,
          w.status, w.material_state, w.planned_start or "", w.planned_end or "", w.created_at]
         for w in rows],
        "work_orders.csv")


@router.get("/maintenance.csv")
def export_maintenance_csv(db: Session = Depends(_get_db),
                           current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.MaintenanceTask).order_by(models.MaintenanceTask.id.asc()).all()
    names = _machine_names(db)
    return _csv(
        ["id", "task_no", "machine_id", "machine_name", "task_type", "priority", "assigned_to",
         "planned_date", "completed_date", "downtime_minutes", "spare_parts_used", "status"],
        [[t.id, t.task_no, t.machine_id, names.get(t.machine_id) or "", t.task_type,
          t.priority, t.assigned_to, t.planned_date or "", t.completed_date or "",
          t.downtime_minutes or 0, t.spare_parts_used or "", t.status]
         for t in rows],
        "maintenance_tasks.csv")


@router.get("/purchase-orders.csv")
def export_purchase_orders_csv(db: Session = Depends(_get_db),
                               current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id.asc()).all()
    suppliers = dict(db.query(models.Supplier.id, models.Supplier.supplier_name).all())
    return _csv(
        ["id", "po_no", "supplier_id", "supplier_name", "item_name", "order_quantity",
         "received_quantity", "unit", "expected_delivery_date", "status", "created_at"],
        [[p.id, p.po_no, p.supplier_id, suppliers.get(p.supplier_id) or "", p.item_name,
          p.order_quantity, p.received_quantity or 0, p.unit,
          p.expected_delivery_date or "", p.status, p.created_at]
         for p in rows],
        "purchase_orders.csv")


@router.get("/inventory.csv")
def export_inventory_csv(db: Session = Depends(_get_db),
                         current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.InventoryItem).order_by(models.InventoryItem.id.asc()).all()
    return _csv(
        ["id", "item_code", "item_name", "category", "supplier", "unit",
         "current_stock", "reorder_level", "location"],
        [[i.id, i.item_code, i.item_name, i.category, i.supplier or "", i.unit,
          i.current_stock or 0, i.reorder_level or 0, i.location or ""]
         for i in rows],
        "inventory_items.csv")


@router.get("/quality.csv")
def export_quality_csv(db: Session = Depends(_get_db),
                       current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.QualityInspection).order_by(models.QualityInspection.id.asc()).all()
    names = _machine_names(db)
    return _csv(
        ["id", "inspection_no", "machine_id", "machine_name", "inspector",
         "inspected_quantity", "passed_quantity", "failed_quantity", "defect_category",
         "rework_quantity", "scrap_quantity", "status"],
        [[q.id, q.inspection_no, q.machine_id, names.get(q.machine_id) or "", q.inspector,
          q.inspected_quantity, q.passed_quantity or 0, q.failed_quantity or 0,
          q.defect_category or "", q.rework_quantity or 0, q.scrap_quantity or 0, q.status]
         for q in rows],
        "quality_inspections.csv")


@router.get("/escalations.csv")
def export_escalations_csv(db: Session = Depends(_get_db),
                           current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    rows = db.query(models.Escalation).order_by(models.Escalation.id.asc()).all()
    names = _machine_names(db)
    return _csv(
        ["id", "title", "severity", "department", "owner", "status", "source",
         "machine_id", "machine_name", "created_at", "resolved_at"],
        [[e.id, e.title, e.severity, e.department, e.owner, e.status, e.source,
          e.machine_id or "", names.get(e.machine_id) or "", e.created_at, e.resolved_at or ""]
         for e in rows],
        "escalations.csv")


@router.get("/oee.csv")
def export_oee_csv(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.asc()).all()
    names = _machine_names(db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "availability", "performance", "quality", "oee", "planned_minutes", "runtime_minutes", "total_count", "good_count", "rejected_count", "created_at"])
    for record in records:
        oee = calculate_oee_from_record(record)
        writer.writerow([record.id, record.machine_id, names.get(record.machine_id) or "", oee["availability"], oee["performance"], oee["quality"], oee["oee"], record.planned_minutes, record.runtime_minutes, record.total_count, record.good_count, record.rejected_count, record.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=oee_report.csv"})


@router.get("/intelligence-summary.txt")
def export_intelligence_summary(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    production_records = db.query(models.ProductionRecord).all()
    rate = tenant_unit_value(db, request_tenant(current_user))
    summary = build_management_summary(machines, downtime_logs, shifts, production_records, unit_value_gbp=rate)
    shift_kpis = build_shift_kpis(shifts)
    alerts = build_smart_alerts(machines, production_records, downtime_logs)
    report = build_daily_summary_text(summary, shift_kpis, alerts)
    return Response(content=report, media_type="text/plain", headers={"Content-Disposition": "attachment; filename=amp_intelligence_report.txt"})


@router.get("", response_model=List[schemas.ReportRequestResponse])
def get_reports(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.ReportRequest).order_by(models.ReportRequest.id.desc()).limit(300).all()


@router.post("", response_model=schemas.ReportRequestResponse)
def create_report(payload: schemas.ReportRequestCreate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    existing = db.query(models.ReportRequest).filter(models.ReportRequest.report_no == payload.report_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Report number already exists")
    row = models.ReportRequest(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Report number is already in use")
    db.refresh(row)
    return row

