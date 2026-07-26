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
from sqlalchemy import func, or_
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
    pooled_oee,
)
from auth import get_current_user, require_roles
from database import SessionLocal
from tenancy import request_tenant, tenant_unit_value


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
    # utilization is a nullable Integer (Column(Integer, default=0)); average only
    # the machines that actually have a reading so a single NULL row can't 500 the
    # summary (None in sum()) and an unset machine doesn't drag the mean toward 0.
    util_values = [m.utilization for m in machines if m.utilization is not None]
    avg_utilization = round(sum(util_values) / len(util_values)) if util_values else 0
    total_downtime_minutes = sum(parse_duration_to_minutes(log.duration) for log in logs)

    # Plant OEE pooled across records (ratio of sums), consistent with every other
    # surface; fall back to a utilization estimate only before any production exists.
    pooled = pooled_oee(records)
    avg_oee = pooled["oee"]
    avg_availability = pooled["availability"]
    avg_performance = pooled["performance"]
    avg_quality = pooled["quality"]
    if not records and machines:
        # Fallback only over machines with a utilization reading — calculate_fallback_oee
        # divides utilization by 100, so a NULL row would crash the estimate.
        fallback = [calculate_fallback_oee(m.utilization) for m in machines if m.utilization is not None]
        avg_oee = round(sum(fallback) / len(fallback)) if fallback else 0

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


router = APIRouter(tags=["Analytics"])


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
    rate = tenant_unit_value(db, request_tenant(current_user))
    return build_management_summary(machines, downtime_logs, shifts, production_records, unit_value_gbp=rate)


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
    # Aggregate in SQL, not by hydrating the whole (growing) work_orders table and
    # counting/summing in Python (rule-4 antipattern, same fix already applied to
    # the agent-actions stats #270 and the roster #259). WorkOrder is in
    # SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes these
    # aggregate SELECTs exactly as it did the old .all() scan.
    #
    # actual_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / an update that cleared the field can
    # be NULL. The old sum(wo.actual_quantity ...) then did int + None and raised
    # TypeError, 500-ing this endpoint — the same NULL-count class of bug fixed in
    # the predictive scorer (#274). COALESCE(SUM(...), 0) treats a NULL actual as
    # the column's own default of 0 and never sees an empty-table NULL either.
    status_counts = dict(
        db.query(models.WorkOrder.status, func.count()).group_by(models.WorkOrder.status).all()
    )
    total_work_orders, total_target, total_actual = db.query(
        func.count(models.WorkOrder.id),
        func.coalesce(func.sum(models.WorkOrder.target_quantity), 0),
        func.coalesce(func.sum(models.WorkOrder.actual_quantity), 0),
    ).one()
    total_target = int(total_target)
    total_actual = int(total_actual)
    achievement = round((total_actual / total_target) * 100) if total_target else 0
    return {
        "total_work_orders": total_work_orders,
        "planned": status_counts.get("Planned", 0),
        "running": status_counts.get("Running", 0),
        "completed": status_counts.get("Completed", 0),
        "delayed": status_counts.get("Delayed", 0),
        "total_target": total_target,
        "total_actual": total_actual,
        "achievement": achievement,
    }


@router.get("/analytics/production-plans")
def get_production_plan_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Same hardening as /analytics/work-orders above: aggregate the growing
    # production_plans table in SQL rather than pulling it into Python (rule-4),
    # and COALESCE(SUM(actual_quantity), 0) so a NULL actual (the column is
    # Integer default=0, not NOT NULL) counts as 0 instead of raising TypeError.
    # ProductionPlan is in SCOPED_MODELS, so these aggregates stay tenant-scoped
    # by the ORM hook (ADR-0002).
    status_counts = dict(
        db.query(models.ProductionPlan.status, func.count()).group_by(models.ProductionPlan.status).all()
    )
    total_plans, planned_quantity, actual_quantity = db.query(
        func.count(models.ProductionPlan.id),
        func.coalesce(func.sum(models.ProductionPlan.planned_quantity), 0),
        func.coalesce(func.sum(models.ProductionPlan.actual_quantity), 0),
    ).one()
    planned_quantity = int(planned_quantity)
    actual_quantity = int(actual_quantity)
    achievement = round((actual_quantity / planned_quantity) * 100) if planned_quantity else 0
    return {
        "total_plans": total_plans,
        "planned_quantity": planned_quantity,
        "actual_quantity": actual_quantity,
        "achievement": achievement,
        "planned": status_counts.get("Planned", 0),
        "running": status_counts.get("Running", 0),
        "completed": status_counts.get("Completed", 0),
        "behind": status_counts.get("Behind", 0),
    }


@router.get("/analytics/escalations")
def get_escalation_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    # Aggregate the growing escalations table in SQL rather than hydrating every
    # row into Python just to bucket it with list comprehensions (rule-4
    # antipattern, same fix already applied to /analytics/work-orders and
    # /analytics/production-plans directly above, and the agent-actions stats
    # #270). Escalation is in SCOPED_MODELS, so the do_orm_execute hook
    # (ADR-0002) tenant-scopes these aggregate SELECTs exactly as it did the old
    # .all() scan. GROUP BY reads only the distinct (status|severity) rows, and
    # the total is a single COUNT — no per-row transfer.
    total = db.query(func.count(models.Escalation.id)).scalar() or 0
    status_counts = dict(
        db.query(models.Escalation.status, func.count())
        .group_by(models.Escalation.status)
        .all()
    )
    severity_counts = dict(
        db.query(models.Escalation.severity, func.count())
        .group_by(models.Escalation.severity)
        .all()
    )

    return {
        "total": total,
        "open": status_counts.get("Open", 0),
        "in_progress": status_counts.get("In Progress", 0),
        "resolved": status_counts.get("Resolved", 0),
        "critical": severity_counts.get("Critical", 0),
        "high": severity_counts.get("High", 0),
        "medium": severity_counts.get("Medium", 0),
        "low": severity_counts.get("Low", 0),
    }


@router.get("/analytics/inventory")
def get_inventory_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    items = db.query(models.InventoryItem).all()
    # We only need the ledger's size, never the rows. Loading the whole
    # inventory_transactions table into Python just to call len() is unbounded on
    # a per-movement table that grows with every stock in/out; count it in SQL
    # instead. The count is auto-scoped to the tenant by the do_orm_execute hook
    # exactly like the .all() it replaces (ADR-0002).
    transaction_count = db.query(func.count(models.InventoryTransaction.id)).scalar() or 0

    # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
    # nullable=False — the ORM default only fills a value the *inserter* omitted,
    # so a row written by raw SQL, a migration, or an update that clears the field
    # can legitimately be NULL. `None <= int` and `int + None` raise TypeError and
    # 500 the endpoint; coalesce a missing count to the column's own default of 0,
    # matching the sibling /analytics/system-health rollup which already guards it.
    low_stock_items = [
        item for item in items
        if (item.current_stock or 0) <= (item.reorder_level or 0)
    ]

    total_stock_units = sum((item.current_stock or 0) for item in items)

    category_counts = {}
    supplier_counts = {}

    for item in items:
        stock = item.current_stock or 0
        category_counts[item.category] = category_counts.get(item.category, 0) + stock
        supplier = item.supplier or "Unknown"
        supplier_counts[supplier] = supplier_counts.get(supplier, 0) + stock

    return {
        "total_items": len(items),
        "low_stock_items": len(low_stock_items),
        "total_stock_units": total_stock_units,
        "transactions": transaction_count,
        "category_counts": category_counts,
        "supplier_counts": supplier_counts,
    }


@router.get("/analytics/quality")
def get_quality_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    QI = models.QualityInspection

    # quality_inspections grows with every inspection, so never load the whole
    # table into Python to aggregate it (the rule-4 antipattern the sibling
    # command centres — /analytics/inventory #286, /escalations #288,
    # /operator-terminal #285 — were already bounded away from). Sum the totals
    # in ONE aggregate SELECT and the drill-downs with GROUP BY, so we scan the
    # window in SQL and materialise only the distinct categories/machines, not
    # every row. All of these are auto-scoped to the tenant by the do_orm_execute
    # hook exactly like the .all() scan they replace (ADR-0002).
    #
    # passed / failed / rework / scrap_quantity are Column(Integer, default=0)
    # WITHOUT nullable=False — the ORM default only fills a value the *inserter*
    # omitted, so a row written by raw SQL, a migration, or an update that clears
    # the field can legitimately be NULL. SUM ignores NULLs and returns NULL for
    # an all-NULL/empty group, so coalesce each to the column's own default of 0
    # (inspected_quantity IS nullable=False, so its SUM stays exact). This is the
    # same NULL guard the ai/* quality read-models and the sibling
    # /analytics/final-executive-summary (#281) already apply to these columns.
    total_inspections, inspected, passed, failed, rework, scrap = db.query(
        func.count(QI.id),
        func.coalesce(func.sum(QI.inspected_quantity), 0),
        func.coalesce(func.sum(QI.passed_quantity), 0),
        func.coalesce(func.sum(QI.failed_quantity), 0),
        func.coalesce(func.sum(QI.rework_quantity), 0),
        func.coalesce(func.sum(QI.scrap_quantity), 0),
    ).one()

    # int() so a DB that returns Decimal for SUM (Postgres) matches the plain-int
    # payload the frontend type expects, and so pass/fail rates divide cleanly.
    total_inspections = int(total_inspections or 0)
    inspected = int(inspected or 0)
    passed = int(passed or 0)
    failed = int(failed or 0)
    rework = int(rework or 0)
    scrap = int(scrap or 0)

    # pass_rate / fail_rate share `inspected` as their denominator with the
    # headline totals above — same basis, reconciled (rule 3). 0/0 guarded -> 0.
    pass_rate = round((passed / inspected) * 100) if inspected else 0
    fail_rate = round((failed / inspected) * 100) if inspected else 0

    # Defect breakdown: SUM(failed) per category. `category or "No Defect"` still
    # folds both a NULL and an empty-string category into one bucket (and merges
    # them if both exist), matching the old per-row accumulation exactly.
    defect_counts = {}
    for category, cat_failed in (
        db.query(QI.defect_category, func.coalesce(func.sum(QI.failed_quantity), 0))
        .group_by(QI.defect_category)
        .all()
    ):
        key = category or "No Defect"
        defect_counts[key] = defect_counts.get(key, 0) + int(cat_failed or 0)

    # Per-machine failures: SUM(failed) per machine, skipping the NULL/0 machine_id
    # the old `if row.machine_id` guard dropped (unattributed inspections).
    machine_failures = {}
    for machine_id, m_failed in (
        db.query(QI.machine_id, func.coalesce(func.sum(QI.failed_quantity), 0))
        .filter(QI.machine_id.isnot(None))
        .group_by(QI.machine_id)
        .all()
    ):
        if machine_id:
            machine_failures[machine_id] = int(m_failed or 0)

    return {
        "total_inspections": total_inspections,
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
        # passed / failed / scrap / rework_quantity are nullable Integers (default=0,
        # no nullable=False) — coalesce to 0 so a real SQL NULL can't 500 the exec
        # rollup via int + None (inspected_quantity is nullable=False).
        bucket["inspected"] += row.inspected_quantity
        bucket["passed"] += row.passed_quantity or 0
        bucket["failed"] += row.failed_quantity or 0
        bucket["scrap"] += row.scrap_quantity or 0
        bucket["rework"] += row.rework_quantity or 0

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
            # No production for this machine — fall back to its utilization as a
            # rough availability. utilization is nullable, so treat an unset reading
            # as 0 (max() also floors any stray negative), never `max(None, 0)`.
            availability = max(machine.utilization or 0, 0)

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

    # Plant rollup is POOLED (ratio of sums) — the single standardised OEE
    # definition (analytics_engine.pooled_oee), so /analytics/executive-oee agrees
    # with /oee-summary and every other surface. Averaging per-machine OEE was a
    # mean of ratios that over-weighted small runs AND folded in the no-data
    # fallback constants (utilization / 90-or-60 / 95), giving the exec home a
    # plant OEE that contradicted the pooled one shown elsewhere.
    plant = pooled_oee([rec for recs in production_by_machine.values() for rec in recs])
    plant_availability = plant["availability"]
    plant_performance = plant["performance"]
    plant_quality = plant["quality"]
    plant_oee = plant["oee"]

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
        quality_defects[key] = quality_defects.get(key, 0) + (row.failed_quantity or 0)

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
    # current_stock / reorder_level and failed_quantity are nullable Integers
    # (default=0, no nullable=False); a real SQL NULL made `None <= None` and
    # `sum(... None ...)` 500 this command centre. Coalesce to the column default
    # of 0, exactly like the sibling /analytics/inventory (#286) already does.
    low_stock = len([item for item in inventory_items if (item.current_stock or 0) <= (item.reorder_level or 0)])

    inspected = sum(row.inspected_quantity for row in quality_rows)
    failed = sum((row.failed_quantity or 0) for row in quality_rows)
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
    # Aggregate the growing compliance_documents table in SQL rather than hydrating
    # every row into Python just to bucket it with list comprehensions (rule-4
    # antipattern, the same GROUP BY fix already applied to /analytics/maintenance
    # just below, and to /analytics/work-orders, /analytics/escalations, etc.).
    # ComplianceDocument is in SCOPED_MODELS, so the do_orm_execute hook (ADR-0002)
    # tenant-scopes each aggregate SELECT exactly as it did the old .all() scan —
    # same basis, so the parts still reconcile against the whole.
    today = datetime.utcnow().date()

    total_documents = db.query(func.count(models.ComplianceDocument.id)).scalar() or 0

    status_counts = dict(
        db.query(models.ComplianceDocument.approval_status, func.count())
        .group_by(models.ComplianceDocument.approval_status)
        .all()
    )
    type_counts = {
        document_type: int(count)
        for document_type, count in db.query(
            models.ComplianceDocument.document_type, func.count()
        ).group_by(models.ComplianceDocument.document_type).all()
    }
    department_counts = {
        department: int(count)
        for department, count in db.query(
            models.ComplianceDocument.department, func.count()
        ).group_by(models.ComplianceDocument.department).all()
    }

    # review_due = past its review date and not retired (Obsolete). Filtered in SQL
    # on the now-indexed review_due_date (see main._ensure_index) so a growing
    # register never streams every row back just to count the due ones.
    # approval_status is String default="Draft" (not NOT NULL); the old Python
    # `row.approval_status != "Obsolete"` counted a NULL-status past-due doc as due
    # (None != "Obsolete" is True), but SQL's `approval_status != 'Obsolete'` is
    # NULL — not TRUE — for a NULL status, silently dropping that row. OR the NULL
    # back in to keep the same basis (the same subtlety as the late-order count,
    # #295/#298).
    review_due = db.query(func.count(models.ComplianceDocument.id)).filter(
        models.ComplianceDocument.review_due_date < today,
        or_(
            models.ComplianceDocument.approval_status.is_(None),
            models.ComplianceDocument.approval_status != "Obsolete",
        ),
    ).scalar() or 0

    return {
        "total_documents": total_documents,
        "draft": status_counts.get("Draft", 0),
        "approved": status_counts.get("Approved", 0),
        "under_review": status_counts.get("Under Review", 0),
        "obsolete": status_counts.get("Obsolete", 0),
        "review_due": review_due,
        "type_counts": type_counts,
        "department_counts": department_counts,
    }


@router.get("/analytics/maintenance")
def get_maintenance_analytics(
    db: Session = Depends(_get_db),
    current_user: dict = Depends(get_current_user),
):
    # Aggregate the growing maintenance_tasks table in SQL rather than hydrating
    # every row into Python just to bucket it with list comprehensions (rule-4
    # antipattern, same fix already applied to /analytics/work-orders,
    # /analytics/production-plans and /analytics/escalations). MaintenanceTask is
    # in SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes each of
    # these aggregate SELECTs exactly as it did the old .all() scan — same basis,
    # so the parts still reconcile against the whole.
    today = datetime.utcnow().date()

    total_tasks = db.query(func.count(models.MaintenanceTask.id)).scalar() or 0
    status_counts = dict(
        db.query(models.MaintenanceTask.status, func.count())
        .group_by(models.MaintenanceTask.status)
        .all()
    )
    type_counts = dict(
        db.query(models.MaintenanceTask.task_type, func.count())
        .group_by(models.MaintenanceTask.task_type)
        .all()
    )
    open_count = status_counts.get("Open", 0)
    in_progress = status_counts.get("In Progress", 0)
    completed = status_counts.get("Completed", 0)
    preventive = type_counts.get("Preventive", 0)
    breakdown = type_counts.get("Breakdown", 0)

    # Overdue = planned in the past and not yet finished. Filtered in SQL (on the
    # now-indexed planned_date, see main._ensure_index) so a growing backlog never
    # streams every row back just to count the late ones.
    overdue = db.query(func.count(models.MaintenanceTask.id)).filter(
        models.MaintenanceTask.planned_date < today,
        models.MaintenanceTask.status != "Completed",
    ).scalar() or 0

    # total_downtime_minutes is the honest sum over EVERY task (an open task can
    # already carry accumulated downtime). Mean-time-to-repair, though, is a
    # per-COMPLETED-repair average, so its numerator must be ONLY the completed
    # tasks' downtime — dividing the all-task total by the completed count inflated
    # the average with downtime from repairs that haven't finished (#269).
    # COALESCE(SUM(...), 0) guards the nullable downtime_minutes column (a NULL -> 0,
    # never a None -> TypeError 500) and the empty-table NULL alike.
    total_downtime = int(
        db.query(func.coalesce(func.sum(models.MaintenanceTask.downtime_minutes), 0)).scalar() or 0
    )
    completed_downtime = int(
        db.query(func.coalesce(func.sum(models.MaintenanceTask.downtime_minutes), 0))
        .filter(models.MaintenanceTask.status == "Completed")
        .scalar() or 0
    )
    avg_repair = round(completed_downtime / completed) if completed else 0

    # Per-machine task counts: one GROUP BY on machine_id, then a single name
    # lookup (tenant-scoped like the aggregate itself) — never a per-task Machine
    # query (the old N+1 on a growing table). An unknown machine_id keeps its
    # "Machine {id}" fallback label, exactly as before.
    machine_id_counts = (
        db.query(models.MaintenanceTask.machine_id, func.count())
        .group_by(models.MaintenanceTask.machine_id)
        .all()
    )
    machine_names = dict(db.query(models.Machine.id, models.Machine.name).all())
    machine_counts = {}
    for machine_id, count in machine_id_counts:
        name = machine_names.get(machine_id, f"Machine {machine_id}")
        machine_counts[name] = machine_counts.get(name, 0) + count

    return {
        "total_tasks": total_tasks,
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
    # estimated_minutes is nullable (it carries only a column default); a legacy
    # NULL must read as 0, not crash the summation with a None -> TypeError 500.
    total_minutes = sum((row.estimated_minutes or 0) for row in schedules)

    # One name lookup for every machine (tenant-scoped like the schedule query
    # itself), instead of a per-schedule Machine query inside the loop — that was
    # an N+1 on a growing table, the same anti-pattern the maintenance rollup
    # already dropped.
    machine_names = dict(db.query(models.Machine.id, models.Machine.name).all())
    machine_load = {}
    shift_load = {}

    for row in schedules:
        machine_name = machine_names.get(row.machine_id, f"Machine {row.machine_id}")
        machine_load[machine_name] = machine_load.get(machine_name, 0) + (row.estimated_minutes or 0)
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

    # Resolve machine names from the roster already loaded above rather than a
    # per-signal SELECT inside the loop — that inner query was an N+1 firing once
    # for every distinct (machine, signal) in the window (up to 300 point lookups
    # on a single call), the same anti-pattern already dropped from the
    # maintenance / production-schedule rollups (#273) and the purchasing
    # analytics (#276). Machine is in SCOPED_MODELS, so this tenant-scoped roster
    # holds exactly the machines the per-row query could have returned; an
    # unknown/foreign machine_id is simply absent from the map and still falls
    # back to the "Machine {id}" label — output unchanged, one scan instead of N.
    machine_names = {machine.id: machine.name for machine in machines}

    latest_rows = []
    for row in latest.values():
        latest_rows.append({
            "machine_id": row.machine_id,
            "machine_name": machine_names.get(row.machine_id, f"Machine {row.machine_id}"),
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
    # Aggregate the growing operator_job_executions table in SQL, not by hydrating
    # every row and counting/summing in Python (rule-4 antipattern, same fix as
    # /analytics/work-orders #275 and the roster #259). OperatorJobExecution is in
    # SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes these
    # aggregate SELECTs exactly as it did the old .all() scan.
    #
    # good_count / rejected_count are Column(Integer, default=0) WITHOUT
    # nullable=False, so a row written by raw SQL / a migration / an update that
    # cleared the field can be NULL. The old sum(row.good_count for ...) then did
    # int + None and raised TypeError, 500-ing this endpoint — the same NULL-count
    # class fixed in the work-order rollup (#275) and predictive scorer (#274).
    # COALESCE(SUM(...), 0) treats a NULL count as the column's own default of 0
    # and never sees an empty-table NULL either.
    status_counts = dict(
        db.query(models.OperatorJobExecution.job_status, func.count())
        .group_by(models.OperatorJobExecution.job_status)
        .all()
    )
    total_jobs, good, rejected = db.query(
        func.count(models.OperatorJobExecution.id),
        func.coalesce(func.sum(models.OperatorJobExecution.good_count), 0),
        func.coalesce(func.sum(models.OperatorJobExecution.rejected_count), 0),
    ).one()
    good = int(good)
    rejected = int(rejected)
    total = good + rejected
    quality_rate = round((good / total) * 100) if total else 0

    return {
        "total_jobs": total_jobs,
        "started": status_counts.get("Started", 0),
        "paused": status_counts.get("Paused", 0),
        "completed": status_counts.get("Completed", 0),
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

    # Several count columns are Column(Integer, default=0) WITHOUT nullable=False
    # (passed_quantity, dispatched_quantity, current_stock, reorder_level, amount).
    # The ORM default only fills a value the inserter omitted, so a row written by
    # raw SQL, a migration, or an update that clears the field can legitimately be
    # NULL. The old raw sum(...) / None comparisons then did int + None (or
    # None <= None) and raised TypeError, 500-ing this whole executive summary on a
    # single unset row — the same NULL-count class fixed in the order/purchasing
    # analytics (#278) and the work-order rollup. Coalesce each to the column's own
    # default of 0. inspected_quantity and order_quantity are nullable=False and
    # need no guard (but stay the divisors, so the divide-by-zero guards remain).
    inspected = sum(row.inspected_quantity for row in quality)
    passed = sum((row.passed_quantity or 0) for row in quality)
    quality_rate = round((passed / inspected) * 100) if inspected else 0

    order_qty = sum(row.order_quantity for row in orders)
    dispatched_qty = sum((row.dispatched_quantity or 0) for row in orders)
    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else 0

    return {
        "machine_count": len(machines),
        "running_machines": len([m for m in machines if m.status == "Running"]),
        "work_orders": len(work_orders),
        "production_plans": len(production_plans),
        "quality_rate": quality_rate,
        "low_stock_items": len([
            item for item in inventory
            if (item.current_stock or 0) <= (item.reorder_level or 0)
        ]),
        "customer_orders": len(orders),
        "dispatch_rate": dispatch_rate,
        "purchase_orders": len(purchase_orders),
        "total_cost": sum((row.amount or 0) for row in cost_records),
    }


@router.get("/analytics/industrial-gateway")
def get_industrial_gateway_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    devices = db.query(models.IndustrialDevice).all()
    signals = db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.desc()).limit(500).all()
    mappings = db.query(models.PlcSignalMapping).all()

    # Resolve device and machine names from rosters read ONCE, not with a fresh
    # SELECT per signal inside the loop — that was an N+1 firing twice for every
    # distinct (device, signal) in the window (up to ~1,000 point lookups on a
    # single call) on the growing industrial_signals table, the same anti-pattern
    # already dropped from the sibling /analytics/iot-command (#294) and the
    # maintenance / production-schedule / purchasing rollups (#273, #276).
    # `devices` above is already the full roster, and IndustrialDevice / Machine
    # are both in SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes
    # these maps exactly as it scoped the per-row queries — each map holds exactly
    # the rows the inner query could have returned. An unknown/foreign id is simply
    # absent and still falls back to the SAME label. Output unchanged, one scan
    # instead of N.
    device_names = {device.id: device.device_name for device in devices}
    machine_names = dict(db.query(models.Machine.id, models.Machine.name).all())

    latest = []
    seen = set()
    for signal in signals:
        key = f"{signal.device_id}:{signal.signal_name}"
        if key in seen:
            continue
        seen.add(key)
        latest.append({
            "device_id": signal.device_id,
            "device_name": device_names.get(signal.device_id, f"Device {signal.device_id}"),
            "machine_name": machine_names.get(signal.machine_id, "-"),
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
