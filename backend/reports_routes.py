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
from report_generator import build_daily_summary_text


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("/downtime.csv")
def export_downtime_csv(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    logs = db.query(models.DowntimeLog).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "reason", "duration", "notes", "created_at"])
    for log in logs:
        machine = db.query(models.Machine).filter(models.Machine.id == log.machine_id).first()
        writer.writerow([log.id, log.machine_id, machine.name if machine else "", log.reason, log.duration, log.notes or "", log.created_at])
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


@router.get("/oee.csv")
def export_oee_csv(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    records = db.query(models.ProductionRecord).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "machine_id", "machine_name", "availability", "performance", "quality", "oee", "planned_minutes", "runtime_minutes", "total_count", "good_count", "rejected_count", "created_at"])
    for record in records:
        oee = calculate_oee_from_record(record)
        writer.writerow([record.id, record.machine_id, record.machine.name if record.machine else "", oee["availability"], oee["performance"], oee["quality"], oee["oee"], record.planned_minutes, record.runtime_minutes, record.total_count, record.good_count, record.rejected_count, record.created_at])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=oee_report.csv"})


@router.get("/intelligence-summary.txt")
def export_intelligence_summary(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    production_records = db.query(models.ProductionRecord).all()
    summary = build_management_summary(machines, downtime_logs, shifts, production_records)
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
    db.commit()
    db.refresh(row)
    return row

