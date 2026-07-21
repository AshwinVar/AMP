"""Analytics & alerts routes — the read-model / intelligence surface.

The dashboard's aggregation layer: the OEE + alerts summary, per-page command
centres (work-orders / inventory / quality / factory / IoT / operator / ...),
executive rollups, predictive maintenance, and machine health. These are
read-only projections — compute comes from the shared engines (analytics_engine
build_* + generate_alerts + calculate_*), the AI read-models (ai.*), and the
digital twin (ai.twin) — so nothing here couples back to main. Tenant scoping is
the ORM chokepoint (ADR-0002); a couple of endpoints read the effective tenant
via request_tenant. Peeled out of main.py per ADR-0009.

analytics_summary is defined at MODULE LEVEL (not nested in register) because
main's /reports/daily-summary.txt calls it directly; it is registered as
/analytics/summary here and imported by main.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import ai
import ai.prediction
import ai.twin
import models
from analytics_engine import (
    build_management_summary,
    build_oee_trends,
    build_shift_kpis,
    build_smart_alerts,
    calculate_fallback_oee,
    calculate_oee_from_record,
    generate_alerts,
    parse_duration_to_minutes,
)
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def analytics_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    records = db.query(models.ProductionRecord).all()

    running = len([m for m in machines if m.status == "Running"])
    idle = len([m for m in machines if m.status == "Idle"])
    breakdown = len([m for m in machines if m.status == "Breakdown"])
    maintenance = len([m for m in machines if m.status == "Maintenance"])
    avg_utilization = round(sum(m.utilization for m in machines) / len(machines)) if machines else 0
    total_downtime_minutes = sum(parse_duration_to_minutes(log.duration) for log in logs)

    avg_oee = 0
    avg_availability = 0
    avg_performance = 0
    avg_quality = 0
    if records:
        oee_rows = [calculate_oee_from_record(record) for record in records]
        avg_oee = round(sum(row["oee"] for row in oee_rows) / len(oee_rows))
        avg_availability = round(sum(row["availability"] for row in oee_rows) / len(oee_rows))
        avg_performance = round(sum(row["performance"] for row in oee_rows) / len(oee_rows))
        avg_quality = round(sum(row["quality"] for row in oee_rows) / len(oee_rows))
    elif machines:
        avg_oee = round(sum(calculate_fallback_oee(m.utilization) for m in machines) / len(machines))

    avg_shift_efficiency = (
        round(sum((s.actual_output / s.target_output) * 100 if s.target_output else 0 for s in shifts) / len(shifts))
        if shifts else 0
    )

    reason_counts = {}
    machine_downtime = {}
    for log in logs:
        reason_counts[log.reason] = reason_counts.get(log.reason, 0) + 1
        machine_downtime[log.machine_id] = machine_downtime.get(log.machine_id, 0) + parse_duration_to_minutes(log.duration)

    top_reason = max(reason_counts.items(), key=lambda x: x[1])[0] if reason_counts else "No data"
    top_machine_id = max(machine_downtime.items(), key=lambda x: x[1])[0] if machine_downtime else None
    top_machine_name = "No data"
    if top_machine_id:
        machine = db.query(models.Machine).filter(models.Machine.id == top_machine_id).first()
        if machine:
            top_machine_name = machine.name

    alerts = generate_alerts(db)

    return {
        "machines": len(machines),
        "running": running,
        "idle": idle,
        "breakdown": breakdown,
        "maintenance": maintenance,
        "avg_utilization": avg_utilization,
        "avg_oee": avg_oee,
        "avg_availability": avg_availability,
        "avg_performance": avg_performance,
        "avg_quality": avg_quality,
        "downtime_events": len(logs),
        "total_downtime_minutes": total_downtime_minutes,
        "avg_shift_efficiency": avg_shift_efficiency,
        "top_reason": top_reason,
        "top_machine": top_machine_name,
        "reason_counts": reason_counts,
        "alerts": alerts,
    }


router = APIRouter()


@router.get("/oee/summary")
def oee_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()
    data = []
    for record in records:
        oee = calculate_oee_from_record(record)
        data.append(
            {
                "id": record.id,
                "machine_id": record.machine_id,
                "machine_name": record.machine.name if record.machine else f"Machine {record.machine_id}",
                "availability": oee["availability"],
                "performance": oee["performance"],
                "quality": oee["quality"],
                "oee": oee["oee"],
                "created_at": record.created_at,
            }
        )
    return data

router.get("/analytics/summary")(analytics_summary)


@router.get("/alerts")
def get_alerts(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return generate_alerts(db)


@router.get("/analytics/machine-timeline")
def get_machine_timeline(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    events = db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(200).all()
    return [
        {
            "id": event.id,
            "machine_id": event.machine_id,
            "machine_name": event.machine_name,
            "old_status": event.old_status,
            "new_status": event.new_status,
            "utilization": event.utilization,
            "source": event.source,
            "created_at": event.created_at,
        }
        for event in events
    ]


@router.get("/analytics/machine-state-summary")
def get_machine_state_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    events = db.query(models.MachineEvent).order_by(models.MachineEvent.id.desc()).limit(300).all()
    summary = {}
    for event in events:
        machine = summary.setdefault(
            event.machine_name,
            {"machine_name": event.machine_name, "Running": 0, "Idle": 0, "Breakdown": 0, "Maintenance": 0, "total_events": 0},
        )
        if event.new_status in machine:
            machine[event.new_status] += 1
        machine["total_events"] += 1
    return list(summary.values())


@router.get("/analytics/oee-trends")
def get_oee_trends(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.asc()).limit(200).all()
    return build_oee_trends(records)


@router.get("/analytics/shift-kpis")
def get_shift_kpis(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(50).all()
    return build_shift_kpis(shifts)


@router.get("/analytics/management")
def get_management_dashboard(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    production_records = db.query(models.ProductionRecord).all()
    return build_management_summary(machines, downtime_logs, shifts, production_records)


@router.get("/alerts/smart")
def get_smart_alerts(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    production_records = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(100).all()
    downtime_logs = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(100).all()
    return build_smart_alerts(machines, production_records, downtime_logs)


@router.get("/analytics/predictive-maintenance")
def get_predictive_maintenance(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Predictive maintenance now runs through the AI platform (ADR-0003) rather
    # than the engine directly - same rule-based result today, swappable for
    # ML/LLM behind ai.prediction without touching this endpoint.
    return ai.prediction.assess_from_db(db)


@router.get("/machine-health/{machine_id}")
def get_machine_detail(machine_id: int, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Machine Health detail (ADR-0006): the single-machine cockpit — the twin
    # snapshot plus a risk-factor breakdown, a unified event timeline, and the
    # agent actions awaiting approval for this machine.
    detail = ai.twin.build_machine_detail(db, request_tenant(current_user), machine_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    return detail


@router.get("/analytics/work-orders")
def get_work_order_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    work_orders = db.query(models.WorkOrder).all()
    planned = len([wo for wo in work_orders if wo.status == "Planned"])
    running = len([wo for wo in work_orders if wo.status == "Running"])
    completed = len([wo for wo in work_orders if wo.status == "Completed"])
    delayed = len([wo for wo in work_orders if wo.status == "Delayed"])
    total_target = sum(wo.target_quantity for wo in work_orders)
    total_actual = sum(wo.actual_quantity for wo in work_orders)
    achievement = round((total_actual / total_target) * 100) if total_target else 0
    return {
        "total_work_orders": len(work_orders),
        "planned": planned,
        "running": running,
        "completed": completed,
        "delayed": delayed,
        "total_target": total_target,
        "total_actual": total_actual,
        "achievement": achievement,
    }


@router.get("/analytics/production-plans")
def get_production_plan_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    plans = db.query(models.ProductionPlan).all()
    planned_quantity = sum(plan.planned_quantity for plan in plans)
    actual_quantity = sum(plan.actual_quantity for plan in plans)
    achievement = round((actual_quantity / planned_quantity) * 100) if planned_quantity else 0
    return {
        "total_plans": len(plans),
        "planned_quantity": planned_quantity,
        "actual_quantity": actual_quantity,
        "achievement": achievement,
        "planned": len([plan for plan in plans if plan.status == "Planned"]),
        "running": len([plan for plan in plans if plan.status == "Running"]),
        "completed": len([plan for plan in plans if plan.status == "Completed"]),
        "behind": len([plan for plan in plans if plan.status == "Behind"]),
    }


@router.get("/analytics/escalations")
def get_escalation_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = db.query(models.Escalation).all()

    return {
        "total": len(rows),
        "open": len([row for row in rows if row.status == "Open"]),
        "in_progress": len([row for row in rows if row.status == "In Progress"]),
        "resolved": len([row for row in rows if row.status == "Resolved"]),
        "critical": len([row for row in rows if row.severity == "Critical"]),
        "high": len([row for row in rows if row.severity == "High"]),
        "medium": len([row for row in rows if row.severity == "Medium"]),
        "low": len([row for row in rows if row.severity == "Low"]),
    }


@router.get("/analytics/inventory")
def get_inventory_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    items = db.query(models.InventoryItem).all()
    transactions = db.query(models.InventoryTransaction).all()

    low_stock_items = [
        item for item in items
        if item.current_stock <= item.reorder_level
    ]

    total_stock_units = sum(item.current_stock for item in items)

    category_counts = {}
    supplier_counts = {}

    for item in items:
        category_counts[item.category] = category_counts.get(item.category, 0) + item.current_stock
        supplier = item.supplier or "Unknown"
        supplier_counts[supplier] = supplier_counts.get(supplier, 0) + item.current_stock

    return {
        "total_items": len(items),
        "low_stock_items": len(low_stock_items),
        "total_stock_units": total_stock_units,
        "transactions": len(transactions),
        "category_counts": category_counts,
        "supplier_counts": supplier_counts,
    }


@router.get("/analytics/quality")
def get_quality_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    inspections = db.query(models.QualityInspection).all()

    inspected = sum(row.inspected_quantity for row in inspections)
    passed = sum(row.passed_quantity for row in inspections)
    failed = sum(row.failed_quantity for row in inspections)
    rework = sum(row.rework_quantity for row in inspections)
    scrap = sum(row.scrap_quantity for row in inspections)

    pass_rate = round((passed / inspected) * 100) if inspected else 0
    fail_rate = round((failed / inspected) * 100) if inspected else 0

    defect_counts = {}
    machine_failures = {}

    for row in inspections:
        category = row.defect_category or "No Defect"
        defect_counts[category] = defect_counts.get(category, 0) + row.failed_quantity

        if row.machine_id:
            machine_failures[row.machine_id] = machine_failures.get(row.machine_id, 0) + row.failed_quantity

    return {
        "total_inspections": len(inspections),
        "inspected_quantity": inspected,
        "passed_quantity": passed,
        "failed_quantity": failed,
        "rework_quantity": rework,
        "scrap_quantity": scrap,
        "pass_rate": pass_rate,
        "fail_rate": fail_rate,
        "defect_counts": defect_counts,
        "machine_failures": machine_failures,
    }


@router.get("/analytics/executive-oee")
def get_executive_oee(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    production_records = db.query(models.ProductionRecord).all()
    shifts = db.query(models.ShiftData).all()
    quality_rows = db.query(models.QualityInspection).all()

    machine_map = {machine.id: machine.name for machine in machines}

    production_by_machine = {}
    for record in production_records:
        production_by_machine.setdefault(record.machine_id, []).append(record)

    downtime_by_machine = {}
    reason_counts = {}

    for log in downtime_logs:
        minutes = parse_duration_to_minutes(log.duration)
        downtime_by_machine[log.machine_id] = downtime_by_machine.get(log.machine_id, 0) + minutes
        reason_counts[log.reason] = reason_counts.get(log.reason, 0) + minutes

    quality_by_machine = {}
    for row in quality_rows:
        if not row.machine_id:
            continue
        bucket = quality_by_machine.setdefault(
            row.machine_id,
            {"inspected": 0, "passed": 0, "failed": 0, "scrap": 0, "rework": 0},
        )
        bucket["inspected"] += row.inspected_quantity
        bucket["passed"] += row.passed_quantity
        bucket["failed"] += row.failed_quantity
        bucket["scrap"] += row.scrap_quantity
        bucket["rework"] += row.rework_quantity

    machine_rows = []

    for machine in machines:
        records = production_by_machine.get(machine.id, [])
        planned_minutes = sum(record.planned_minutes for record in records)
        runtime_minutes = sum(record.runtime_minutes for record in records)
        ideal_cycle_total = sum(
            record.ideal_cycle_time_seconds * record.total_count
            for record in records
        )
        total_count = sum(record.total_count for record in records)
        good_count = sum(record.good_count for record in records)
        rejected_count = sum(record.rejected_count for record in records)

        if planned_minutes > 0:
            availability = round((runtime_minutes / planned_minutes) * 100)
        else:
            availability = max(machine.utilization, 0)

        runtime_seconds = runtime_minutes * 60
        if runtime_seconds > 0:
            performance = round(min((ideal_cycle_total / runtime_seconds), 1) * 100)
        else:
            performance = 90 if machine.status == "Running" else 60

        if total_count > 0:
            quality = round((good_count / total_count) * 100)
        else:
            q = quality_by_machine.get(machine.id)
            if q and q["inspected"] > 0:
                quality = round((q["passed"] / q["inspected"]) * 100)
            else:
                quality = 95

        oee = round((availability / 100) * (performance / 100) * (quality / 100) * 100)

        machine_rows.append(
            {
                "machine_id": machine.id,
                "machine_name": machine.name,
                "status": machine.status,
                "availability": availability,
                "performance": performance,
                "quality": quality,
                "oee": oee,
                "downtime_minutes": downtime_by_machine.get(machine.id, 0),
                "total_count": total_count,
                "good_count": good_count,
                "rejected_count": rejected_count,
                "utilization": machine.utilization,
            }
        )

    machine_rows.sort(key=lambda row: row["oee"], reverse=True)

    plant_availability = round(sum(row["availability"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_performance = round(sum(row["performance"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_quality = round(sum(row["quality"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0
    plant_oee = round(sum(row["oee"] for row in machine_rows) / len(machine_rows)) if machine_rows else 0

    total_target = sum(shift.target_output for shift in shifts)
    total_actual = sum(shift.actual_output for shift in shifts)
    plan_achievement = round((total_actual / total_target) * 100) if total_target else 0

    downtime_pareto = [
        {"reason": reason, "minutes": minutes}
        for reason, minutes in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    shift_rows = []
    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        shift_rows.append(
            {
                "shift_name": shift.shift_name,
                "target_output": shift.target_output,
                "actual_output": shift.actual_output,
                "efficiency": efficiency,
            }
        )

    quality_defects = {}
    for row in quality_rows:
        key = row.defect_category or "No Defect"
        quality_defects[key] = quality_defects.get(key, 0) + row.failed_quantity

    quality_trend = [
        {"defect": defect, "failed_quantity": qty}
        for defect, qty in sorted(quality_defects.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "plant_availability": plant_availability,
        "plant_performance": plant_performance,
        "plant_quality": plant_quality,
        "plant_oee": plant_oee,
        "machine_ranking": machine_rows,
        "downtime_pareto": downtime_pareto,
        "shift_oee": shift_rows,
        "quality_trend": quality_trend,
        "production_target": total_target,
        "production_actual": total_actual,
        "production_achievement": plan_achievement,
        "running_machines": len([machine for machine in machines if machine.status == "Running"]),
        "breakdown_machines": len([machine for machine in machines if machine.status == "Breakdown"]),
    }


@router.get("/analytics/factory-command-center")
def get_factory_command_center(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    work_orders = db.query(models.WorkOrder).all()
    production_plans = db.query(models.ProductionPlan).all()
    escalations = db.query(models.Escalation).all()
    inventory_items = db.query(models.InventoryItem).all()
    quality_rows = db.query(models.QualityInspection).all()
    nodes = db.query(models.FactoryLayoutNode).all()

    total_downtime = sum(parse_duration_to_minutes(log.duration) for log in downtime_logs)
    running = len([machine for machine in machines if machine.status == "Running"])
    breakdown = len([machine for machine in machines if machine.status == "Breakdown"])
    idle = len([machine for machine in machines if machine.status == "Idle"])
    maintenance = len([machine for machine in machines if machine.status == "Maintenance"])
    active_work_orders = len([row for row in work_orders if row.status in ["Running", "Planned"]])
    behind_plans = len([row for row in production_plans if row.status == "Behind"])
    open_escalations = len([row for row in escalations if row.status != "Resolved"])
    low_stock = len([item for item in inventory_items if item.current_stock <= item.reorder_level])

    inspected = sum(row.inspected_quantity for row in quality_rows)
    failed = sum(row.failed_quantity for row in quality_rows)
    quality_fail_rate = round((failed / inspected) * 100) if inspected else 0

    machine_map = {machine.id: machine for machine in machines}
    zone_summary = {}

    for node in nodes:
        zone = zone_summary.setdefault(
            node.zone,
            {"zone": node.zone, "nodes": 0, "running": 0, "breakdown": 0, "idle": 0, "maintenance": 0},
        )
        zone["nodes"] += 1

        if node.machine_id and node.machine_id in machine_map:
            status = machine_map[node.machine_id].status
            if status == "Running":
                zone["running"] += 1
            elif status == "Breakdown":
                zone["breakdown"] += 1
            elif status == "Idle":
                zone["idle"] += 1
            elif status == "Maintenance":
                zone["maintenance"] += 1

    return {
        "machines": len(machines),
        "running": running,
        "breakdown": breakdown,
        "idle": idle,
        "maintenance": maintenance,
        "total_downtime_minutes": total_downtime,
        "active_work_orders": active_work_orders,
        "behind_plans": behind_plans,
        "open_escalations": open_escalations,
        "low_stock_items": low_stock,
        "quality_fail_rate": quality_fail_rate,
        "zone_summary": list(zone_summary.values()),
    }


@router.get("/analytics/documents")
def get_document_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    documents = db.query(models.ComplianceDocument).all()
    today = datetime.utcnow().date()

    draft = len([row for row in documents if row.approval_status == "Draft"])
    approved = len([row for row in documents if row.approval_status == "Approved"])
    under_review = len([row for row in documents if row.approval_status == "Under Review"])
    obsolete = len([row for row in documents if row.approval_status == "Obsolete"])
    review_due = len([row for row in documents if row.review_due_date < today and row.approval_status != "Obsolete"])

    type_counts = {}
    department_counts = {}

    for row in documents:
        type_counts[row.document_type] = type_counts.get(row.document_type, 0) + 1
        department_counts[row.department] = department_counts.get(row.department, 0) + 1

    return {
        "total_documents": len(documents),
        "draft": draft,
        "approved": approved,
        "under_review": under_review,
        "obsolete": obsolete,
        "review_due": review_due,
        "type_counts": type_counts,
        "department_counts": department_counts,
    }


@router.get("/analytics/maintenance")
def get_maintenance_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    tasks = db.query(models.MaintenanceTask).all()
    today = datetime.utcnow().date()

    open_count = len([row for row in tasks if row.status == "Open"])
    in_progress = len([row for row in tasks if row.status == "In Progress"])
    completed = len([row for row in tasks if row.status == "Completed"])
    overdue = len([row for row in tasks if row.planned_date < today and row.status != "Completed"])
    preventive = len([row for row in tasks if row.task_type == "Preventive"])
    breakdown = len([row for row in tasks if row.task_type == "Breakdown"])

    total_downtime = sum(row.downtime_minutes for row in tasks)
    avg_repair = round(total_downtime / completed) if completed else 0

    machine_counts = {}
    for row in tasks:
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        name = machine.name if machine else f"Machine {row.machine_id}"
        machine_counts[name] = machine_counts.get(name, 0) + 1

    return {
        "total_tasks": len(tasks),
        "open": open_count,
        "in_progress": in_progress,
        "completed": completed,
        "overdue": overdue,
        "preventive": preventive,
        "breakdown": breakdown,
        "total_downtime_minutes": total_downtime,
        "avg_repair_minutes": avg_repair,
        "machine_counts": machine_counts,
    }


@router.get("/analytics/production-schedules")
def get_production_schedule_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    schedules = db.query(models.ProductionSchedule).all()

    scheduled = len([row for row in schedules if row.status == "Scheduled"])
    running = len([row for row in schedules if row.status == "Running"])
    completed = len([row for row in schedules if row.status == "Completed"])
    delayed = len([row for row in schedules if row.status == "Delayed"])

    total_quantity = sum(row.planned_quantity for row in schedules)
    total_minutes = sum(row.estimated_minutes for row in schedules)

    machine_load = {}
    shift_load = {}

    for row in schedules:
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        machine_name = machine.name if machine else f"Machine {row.machine_id}"
        machine_load[machine_name] = machine_load.get(machine_name, 0) + row.estimated_minutes
        shift_load[row.shift_name] = shift_load.get(row.shift_name, 0) + row.planned_quantity

    bottlenecks = [
        {"machine": name, "load_minutes": minutes}
        for name, minutes in sorted(machine_load.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "total_schedules": len(schedules),
        "scheduled": scheduled,
        "running": running,
        "completed": completed,
        "delayed": delayed,
        "total_quantity": total_quantity,
        "total_minutes": total_minutes,
        "machine_load": machine_load,
        "shift_load": shift_load,
        "bottlenecks": bottlenecks,
    }


@router.get("/analytics/iot-command")
def get_iot_command_center(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    telemetry = db.query(models.IoTTelemetry).order_by(models.IoTTelemetry.id.desc()).limit(300).all()

    latest = {}
    for row in telemetry:
        key = f"{row.machine_id}:{row.signal_name}"
        if key not in latest:
            latest[key] = row

    latest_rows = []
    for row in latest.values():
        machine = db.query(models.Machine).filter(models.Machine.id == row.machine_id).first()
        latest_rows.append({
            "machine_id": row.machine_id,
            "machine_name": machine.name if machine else f"Machine {row.machine_id}",
            "signal_name": row.signal_name,
            "signal_value": row.signal_value,
            "numeric_value": row.numeric_value,
            "unit": row.unit,
            "source": row.source,
            "created_at": row.created_at,
        })

    return {
        "machines": len(machines),
        "signals": len(telemetry),
        "live_machines": len(set([row.machine_id for row in telemetry])),
        "latest_signals": latest_rows,
    }


@router.get("/analytics/ai-insights")
def get_ai_insights(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.AIRecommendation).all()
    return {
        "total": len(rows),
        "open": len([row for row in rows if row.status == "Open"]),
        "acknowledged": len([row for row in rows if row.status == "Acknowledged"]),
        "closed": len([row for row in rows if row.status == "Closed"]),
        "critical": len([row for row in rows if row.severity == "Critical"]),
        "high": len([row for row in rows if row.severity == "High"]),
        "medium": len([row for row in rows if row.severity == "Medium"]),
        "low": len([row for row in rows if row.severity == "Low"]),
    }


@router.get("/analytics/operator-terminal")
def get_operator_terminal_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    rows = db.query(models.OperatorJobExecution).all()
    good = sum(row.good_count for row in rows)
    rejected = sum(row.rejected_count for row in rows)
    total = good + rejected
    quality_rate = round((good / total) * 100) if total else 0

    return {
        "total_jobs": len(rows),
        "started": len([r for r in rows if r.job_status == "Started"]),
        "paused": len([r for r in rows if r.job_status == "Paused"]),
        "completed": len([r for r in rows if r.job_status == "Completed"]),
        "good_count": good,
        "rejected_count": rejected,
        "quality_rate": quality_rate,
    }


@router.get("/analytics/system-health")
def get_system_health(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    users = db.query(models.User).all()
    alerts = db.query(models.Alert).all()
    escalations = db.query(models.Escalation).all()
    notifications = db.query(models.Notification).all()
    audit_logs = db.query(models.AuditLog).all()

    return {
        "api_status": "Healthy",
        "database_status": "Connected",
        "machines": len(machines),
        "users": len(users),
        "alerts": len(alerts),
        "open_escalations": len([row for row in escalations if row.status != "Resolved"]),
        "unread_notifications": len([row for row in notifications if row.status == "Unread"]),
        "audit_logs": len(audit_logs),
        "modules_enabled": [
            "MES",
            "OEE",
            "Digital Twin",
            "Quality",
            "Inventory",
            "Purchasing",
            "Orders",
            "CMMS",
            "Scheduling",
            "IoT",
            "AI",
            "SaaS",
            "Costing",
            "Operator Terminal",
            "Compliance",
        ],
    }


@router.get("/analytics/final-executive-summary")
def get_final_executive_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    work_orders = db.query(models.WorkOrder).all()
    production_plans = db.query(models.ProductionPlan).all()
    quality = db.query(models.QualityInspection).all()
    inventory = db.query(models.InventoryItem).all()
    orders = db.query(models.CustomerOrder).all()
    purchase_orders = db.query(models.PurchaseOrder).all()
    cost_records = db.query(models.CostRecord).all()

    inspected = sum(row.inspected_quantity for row in quality)
    passed = sum(row.passed_quantity for row in quality)
    quality_rate = round((passed / inspected) * 100) if inspected else 0

    order_qty = sum(row.order_quantity for row in orders)
    dispatched_qty = sum(row.dispatched_quantity for row in orders)
    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else 0

    return {
        "machine_count": len(machines),
        "running_machines": len([m for m in machines if m.status == "Running"]),
        "work_orders": len(work_orders),
        "production_plans": len(production_plans),
        "quality_rate": quality_rate,
        "low_stock_items": len([item for item in inventory if item.current_stock <= item.reorder_level]),
        "customer_orders": len(orders),
        "dispatch_rate": dispatch_rate,
        "purchase_orders": len(purchase_orders),
        "total_cost": sum(row.amount for row in cost_records),
    }


@router.get("/analytics/industrial-gateway")
def get_industrial_gateway_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    devices = db.query(models.IndustrialDevice).all()
    signals = db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.desc()).limit(500).all()
    mappings = db.query(models.PlcSignalMapping).all()

    latest = []
    seen = set()
    for signal in signals:
        key = f"{signal.device_id}:{signal.signal_name}"
        if key in seen:
            continue
        seen.add(key)
        device = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.id == signal.device_id).first()
        machine = db.query(models.Machine).filter(models.Machine.id == signal.machine_id).first() if signal.machine_id else None
        latest.append({
            "device_id": signal.device_id,
            "device_name": device.device_name if device else f"Device {signal.device_id}",
            "machine_name": machine.name if machine else "-",
            "signal_name": signal.signal_name,
            "signal_value": signal.signal_value,
            "numeric_value": signal.numeric_value,
            "unit": signal.unit,
            "quality": signal.quality,
            "source_protocol": signal.source_protocol,
            "created_at": signal.created_at,
        })

    return {
        "devices": len(devices),
        "online_devices": len([d for d in devices if d.status == "Online"]),
        "offline_devices": len([d for d in devices if d.status == "Offline"]),
        "signals": len(signals),
        "mappings": len(mappings),
        "enabled_mappings": len([m for m in mappings if m.enabled == "Yes"]),
        "latest_signals": latest[:30],
    }
