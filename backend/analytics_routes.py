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
from sqlalchemy.orm import Session, joinedload

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
    normalize_downtime_reason,
    parse_duration_to_minutes,
    pooled_oee_from_sums,
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

    # Plant OEE is a pure ratio of sums (pooled_oee), so the growing
    # production_records table belongs in SQL — not hydrated whole into Python just
    # to sum it, the rule-4 antipattern already retired on the sibling rollups
    # (/analytics/executive-oee #405, /analytics/final-executive-summary #370,
    # /analytics/factory-command-center #372). At a year of a twenty-machine plant
    # the old `.all()` was thousands of ORM objects per call on an endpoint the
    # dashboard polls. ProductionRecord is in SCOPED_MODELS, so this aggregate stays
    # tenant-scoped by the do_orm_execute hook (ADR-0002) exactly as the scan did.
    #
    # coalesce keeps the arithmetic total: SUM over no rows is NULL. The counted
    # columns are all nullable=False, but the ideal*count product and the empty
    # table can still yield NULL, and 0 is the reading pooled_oee_from_sums takes.
    # The record COUNT drives the no-production fallback below (was `if not records`).
    planned_sum, runtime_sum, total_sum, good_sum, ideal_sum, record_count = db.query(
        func.coalesce(func.sum(models.ProductionRecord.planned_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.runtime_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.total_count), 0),
        func.coalesce(func.sum(models.ProductionRecord.good_count), 0),
        func.coalesce(
            func.sum(
                models.ProductionRecord.ideal_cycle_time_seconds
                * models.ProductionRecord.total_count
            ),
            0,
        ),
        func.count(models.ProductionRecord.id),
    ).one()

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
    # pooled_oee_from_sums is the same pooling pooled_oee(records) delegates to — one
    # definition — so the number is byte-for-byte what the old whole-table scan gave.
    pooled = pooled_oee_from_sums(
        planned=int(planned_sum),
        runtime=int(runtime_sum),
        total=int(total_sum),
        good=int(good_sum),
        ideal_seconds=int(ideal_sum),
        has_data=record_count > 0,
    )
    avg_oee = pooled["oee"]
    avg_availability = pooled["availability"]
    avg_performance = pooled["performance"]
    avg_quality = pooled["quality"]
    if record_count == 0 and machines:
        # Fallback only over machines with a utilization reading — calculate_fallback_oee
        # divides utilization by 100, so a NULL row would crash the estimate.
        fallback = [calculate_fallback_oee(m.utilization) for m in machines if m.utilization is not None]
        avg_oee = round(sum(fallback) / len(fallback)) if fallback else 0

    # Shift efficiency is POOLED (total actual / total target), the same basis as
    # build_management_summary's target_achievement and the shift read-model
    # (ai/shift.py) — never a mean of per-shift ratios. Averaging ratios has two
    # faults the pooled form fixes: (1) it over-weights a small-target shift (a
    # 10/10 shift and a 900/1000 shift averaged to 95%, when the plant really made
    # 910 of 1010 = 90%), and (2) it counted an UNPLANNED shift (target 0) as a
    # real 0% and divided by its slot, dragging the headline down — the exact
    # "never score a shift the data can't measure at 0%" honesty rule ai/shift.py
    # already follows. Pooling adds a 0-target shift's real output to the numerator
    # and nothing to the denominator, so an unplanned shift neither fabricates a 0%
    # nor disagrees with the attainment surfaces on the same shifts.
    #
    # Pooling is a pure ratio of sums, so the growing shift_data table belongs in
    # SQL — not hydrated whole into Python just to sum it, the same rule-4 fix the
    # production_records scan 60 lines above already carries (#411) and the sibling
    # executive rollups do (#405/#370/#372). The old `db.query(ShiftData).all()`
    # returned every shift ever recorded on an endpoint the dashboard polls, only
    # to sum two columns and discard the rows. ShiftData is in SCOPED_MODELS, so the
    # do_orm_execute hook (ADR-0002) tenant-scopes this aggregate exactly as it
    # scoped the scan. COALESCE(SUM(..), 0) matches the old `sum(.. or 0 ..)` byte
    # for byte: SQL SUM already skips a (theoretical, nullable-legacy) NULL row the
    # way `or 0` did, and COALESCE returns 0 for the empty-table SUM (NULL).
    total_shift_target, total_shift_actual = db.query(
        func.coalesce(func.sum(models.ShiftData.target_output), 0),
        func.coalesce(func.sum(models.ShiftData.actual_output), 0),
    ).one()
    total_shift_target = int(total_shift_target)
    total_shift_actual = int(total_shift_actual)
    avg_shift_efficiency = round((total_shift_actual / total_shift_target) * 100) if total_shift_target else 0

    reason_counts = {}
    machine_downtime = {}
    for log in logs:
        # Coalesce a NULL/empty reason to "Unknown" (normalize_downtime_reason) so
        # neither the reason_counts keys nor the derived top_reason surface a literal
        # `null` label — the same convention the canonical downtime read-model uses.
        reason = normalize_downtime_reason(log.reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
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
    # Eager-load the machine: the row below reads `record.machine.name`, and
    # lazily that is one SELECT per distinct machine in the page — 26 statements
    # for 100 records over 25 machines, 101 when every record names a different
    # one. SQLite makes that look cheap; production is Postgres over a network,
    # where each is a round trip.
    #
    # joinedload rather than selectinload because `machine` is many-to-one: the
    # LEFT OUTER JOIN cannot multiply rows, so it stays correct under .limit(100)
    # (a joinedload on a one-to-many would need the subquery form to avoid the
    # limit applying to the joined rows).
    records = (
        db.query(models.ProductionRecord)
        .options(joinedload(models.ProductionRecord.machine))
        .order_by(models.ProductionRecord.id.desc())
        .limit(100)
        .all()
    )
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
    # "Frequency of state transitions by machine" — a per-machine tally over the
    # machine_events stream. The old code pulled the most-recent 300 events GLOBALLY
    # into Python and bucketed them there, which was wrong two ways on a growing
    # table (rule-4 antipattern, same fix already applied to the sibling command
    # centres — /analytics/quality #299, /escalations #288, /operator-terminal #285):
    #   1. Unbounded intent, bounded reality: once machine_events passes 300 rows the
    #      window is a GLOBAL slice, so a quiet machine drops out of the chart
    #      entirely and a busy one shows only its share of the last 300 — never its
    #      true transition frequency the chart's own subtitle promises.
    #   2. It hydrated a slice of a growing table just to count it in Python.
    # Aggregate in SQL with GROUP BY instead: one row per (machine, status), so we
    # materialise only the distinct groups (naturally small) and report TRUE totals
    # over the whole history. MachineEvent is in SCOPED_MODELS, so the do_orm_execute
    # hook (ADR-0002) tenant-scopes this aggregate exactly as it did the .all() scan.
    #
    # total_events counts EVERY event (incl. Offline / any non-tracked status), so it
    # stays >= the sum of the four charted buckets — the same reconciliation the old
    # per-row loop kept (an Offline event bumped total_events but no bucket).
    tracked = ("Running", "Idle", "Breakdown", "Maintenance")
    summary = {}
    for machine_name, new_status, count in (
        db.query(models.MachineEvent.machine_name, models.MachineEvent.new_status, func.count())
        .group_by(models.MachineEvent.machine_name, models.MachineEvent.new_status)
        .all()
    ):
        machine = summary.setdefault(
            machine_name,
            {"machine_name": machine_name, "Running": 0, "Idle": 0, "Breakdown": 0, "Maintenance": 0, "total_events": 0},
        )
        count = int(count)
        if new_status in tracked:
            machine[new_status] += count
        machine["total_events"] += count
    # Deterministic, meaningful order: busiest machines first (the old insertion
    # order was just "whichever machine had the most recent event", not meaningful).
    return sorted(summary.values(), key=lambda m: m["total_events"], reverse=True)


@router.get("/analytics/oee-trends")
def get_oee_trends(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # The most-recent 200 records, then flipped back to chronological order so the
    # trend still reads oldest -> newest along the chart's x-axis.
    #
    # The old query ordered id ASC and then limited to 200 — that pins the window
    # to the FIRST 200 records ever written. On a growing table (production_records
    # only ever appends) the "trend" freezes on ancient production the moment the
    # table passes 200 rows and never shows a recent run again — a trend that can't
    # move. Ordering id DESC selects the newest 200; reversing restores the
    # oldest-first order build_oee_trends indexes onto record #1..N.
    records = (
        db.query(models.ProductionRecord)
        .order_by(models.ProductionRecord.id.desc())
        .limit(200)
        .all()
    )
    records.reverse()
    return build_oee_trends(records)


@router.get("/analytics/shift-kpis")
def get_shift_kpis(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(50).all()
    return build_shift_kpis(shifts)


@router.get("/analytics/management")
def get_management_dashboard(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()

    # Pool plant OEE and shift attainment straight out of SQL rather than hydrating
    # the whole (growing) production_records and shift_data tables into Python just to
    # sum them (rule-4) — the identical fix /analytics/summary already carries
    # (#411/#419). build_management_summary reduces both tables to sums, so passing the
    # pre-aggregated sums yields byte-for-byte the same summary while reading two
    # aggregate rows instead of every row on this Admin-polled endpoint. Both models
    # are in SCOPED_MODELS, so these aggregates stay tenant-scoped by the do_orm_execute
    # hook (ADR-0002) exactly as the old .all() scans did. Downtime stays a row scan
    # (free-text durations — only parse_duration_to_minutes can total them), the same
    # accepted exception /analytics/summary makes.
    production_sums = db.query(
        func.coalesce(func.sum(models.ProductionRecord.planned_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.runtime_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.total_count), 0),
        func.coalesce(func.sum(models.ProductionRecord.good_count), 0),
        func.coalesce(func.sum(
            models.ProductionRecord.ideal_cycle_time_seconds
            * models.ProductionRecord.total_count
        ), 0),
        func.count(models.ProductionRecord.id),
    ).one()
    shift_sums = db.query(
        func.coalesce(func.sum(models.ShiftData.target_output), 0),
        func.coalesce(func.sum(models.ShiftData.actual_output), 0),
    ).one()

    rate = tenant_unit_value(db, request_tenant(current_user))
    return build_management_summary(
        machines, downtime_logs, [], [], unit_value_gbp=rate,
        production_sums=tuple(int(v) for v in production_sums),
        shift_sums=tuple(int(v) for v in shift_sums),
    )


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
        # "Running" and "In Progress" are two spellings of the SAME state (a work
        # order actively being worked) written by two code paths in this app: the
        # UI status dropdown (WorkOrdersSection) writes "Running", the factory
        # simulator (_work_orders / the tick that advances Planned -> In Progress
        # -> Completed) and the e2e sim write "In Progress". Reading the "Running"
        # bucket alone reported 0 in-progress work orders on a seeded/simulated
        # plant while those rows sat plainly in the work-order table below it — and,
        # because total_work_orders counts EVERY row, the four named buckets no
        # longer summed to the headline (an "In Progress" row landed in none of
        # them). Fold the synonym so the Running headline counts every in-progress
        # work order and the breakdown reconciles with the total (rule-1: one
        # metric, not two spellings; rule-3: a breakdown must reconcile with its
        # headline) — exactly the fold the sibling customer-order (#444) and
        # production-schedule (#463) rollups already apply.
        "running": status_counts.get("Running", 0) + status_counts.get("In Progress", 0),
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
        # "Running" and "In Progress" are two spellings of the SAME state (the plan
        # is actively being worked) written by two code paths in this app: the UI
        # status dropdown (ProductionPlanSection) writes "Running", the factory
        # simulator (_production_plans copies the work order's status, and the
        # work-order statuses are 3-of-6 "In Progress") writes "In Progress".
        # Reading the "Running" bucket alone reported 0 in-progress plans on a
        # seeded/simulated plant while those rows sat plainly in the plan table below
        # it — and, because total_plans counts EVERY row, the four named buckets no
        # longer summed to the headline (an "In Progress" row landed in none of them).
        # Fold the synonym so the Running headline counts every in-progress plan and
        # the breakdown reconciles with the total (rule-1: one metric, not two
        # spellings; rule-3: a breakdown must reconcile with its headline) — exactly
        # the fold the sibling /analytics/work-orders (#470) and
        # /analytics/production-schedules (#463) rollups already apply.
        "running": status_counts.get("Running", 0) + status_counts.get("In Progress", 0),
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
    # The machine roster is a bounded reference set — a plant has dozens of
    # machines, and every machine is returned in `machine_ranking`, so reading it
    # whole is what the response needs anyway.
    machines = db.query(models.Machine).all()

    # shift_data, in contrast, GROWS without bound: factory_simulator.tick_shift_entry
    # appends a fresh dated row ("Shift A – 17 Jul") every tick, which is why
    # /analytics/summary was pooled off it in SQL (#419) and ai/shift.py windows it to a
    # week. Reading it whole here streamed every shift ever recorded into Python on an
    # Admin-polled endpoint AND returned one `shift_oee` row per entry — an unbounded,
    # ever-growing response (rule-4). Bound it to the most-recent 50 shifts, the exact
    # window the sibling /analytics/shift-kpis (build_shift_kpis, line ~310) already
    # uses, so the two per-shift attainment surfaces reconcile on ONE basis instead of
    # disagreeing (shift-kpis last-50 vs this endpoint all-time). Reverse to oldest-first
    # so the chart still reads left-to-right chronologically — the same id-desc-then-
    # reverse idiom /analytics/oee-trends uses, and the order the old SQLite `.all()`
    # scan happened to return. The headline production_target/actual below sum this SAME
    # bounded set, so the per-shift breakdown still sums to the headline (rule-3).
    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(50).all()
    shifts.reverse()

    # downtime_logs carries a FREE-TEXT duration ("2 hrs 15 min") that only
    # parse_duration_to_minutes can read — there is no SQL SUM for it — so this
    # is the one scan that genuinely cannot move into SQL, exactly as documented
    # on /analytics/factory-command-center.
    downtime_logs = db.query(models.DowntimeLog).all()

    machine_map = {machine.id: machine.name for machine in machines}

    # Production and quality used to be hydrated whole and summed in Python: at a
    # year of a twenty-machine plant that was 7,300 ORM objects per call, on an
    # endpoint the dashboard polls. Both are pure sums, so they belong in the
    # database — the same fix already applied to /analytics/final-executive-summary
    # (#370), /analytics/factory-command-center (#372) and the sibling rollups.
    # Every model here is in tenancy.SCOPED_MODELS, so the do_orm_execute hook
    # (ADR-0002) tenant-scopes these aggregate SELECTs exactly as it did the
    # .all() scans they replace.
    #
    # coalesce keeps the arithmetic total: SUM over no rows is NULL, and these
    # counted columns are Column(Integer, default=0) WITHOUT nullable=False, so a
    # raw-SQL or migration row can hold NULL. The old Python `sum(...)` over a
    # NULL raised TypeError and 500-ed the whole rollup; 0 is the column's own
    # declared default and the reading the sibling response models already take.
    production_totals = (
        db.query(
            models.ProductionRecord.machine_id,
            func.coalesce(func.sum(models.ProductionRecord.planned_minutes), 0),
            func.coalesce(func.sum(models.ProductionRecord.runtime_minutes), 0),
            func.coalesce(
                func.sum(
                    models.ProductionRecord.ideal_cycle_time_seconds
                    * models.ProductionRecord.total_count
                ),
                0,
            ),
            func.coalesce(func.sum(models.ProductionRecord.total_count), 0),
            func.coalesce(func.sum(models.ProductionRecord.good_count), 0),
            func.coalesce(func.sum(models.ProductionRecord.rejected_count), 0),
        )
        .group_by(models.ProductionRecord.machine_id)
        .all()
    )
    production_by_machine = {row[0]: row[1:] for row in production_totals}

    downtime_by_machine = {}
    reason_counts = {}

    for log in downtime_logs:
        minutes = parse_duration_to_minutes(log.duration)
        downtime_by_machine[log.machine_id] = downtime_by_machine.get(log.machine_id, 0) + minutes
        # Coalesce a NULL/empty reason to "Unknown" so the downtime Pareto never
        # emits a `{"reason": null, ...}` slice — matching the read-model convention.
        reason = normalize_downtime_reason(log.reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + minutes

    # Only `inspected` and `passed` are ever read (the no-production quality
    # fallback below); the old loop also summed failed/scrap/rework and threw
    # them away. passed_quantity is a nullable Integer, hence the coalesce.
    quality_by_machine = {
        machine_id: {"inspected": inspected, "passed": passed}
        for machine_id, inspected, passed in db.query(
            models.QualityInspection.machine_id,
            func.coalesce(func.sum(models.QualityInspection.inspected_quantity), 0),
            func.coalesce(func.sum(models.QualityInspection.passed_quantity), 0),
        )
        .filter(models.QualityInspection.machine_id.isnot(None))
        .group_by(models.QualityInspection.machine_id)
        .all()
    }

    machine_rows = []

    for machine in machines:
        (
            planned_minutes,
            runtime_minutes,
            ideal_cycle_total,
            total_count,
            good_count,
            rejected_count,
        ) = production_by_machine.get(machine.id, (0, 0, 0, 0, 0, 0))

        if planned_minutes > 0:
            # Cap at 100% like pooled_oee / calculate_oee_from_record cap every
            # component: the HTTP ingest (machines_routes.create_production_record)
            # rejects negatives and enforces good+rejected==total, but it does NOT
            # require runtime_minutes <= planned_minutes, so a machine that ran past
            # its planned window (runtime > planned) computed availability > 100%.
            # An availability the data can't support (>100%) then inflated THIS
            # machine's OEE above the physical bound and disagreed with the capped
            # pooled plant rollup below — the exact honesty/reconciliation rule the
            # shared OEE definition already follows (performance is clamped the same
            # way three lines down). min(ratio, 1) before rounding matches pooled_oee.
            availability = round(min(runtime_minutes / planned_minutes, 1) * 100)
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
            # Clamp to 100% too (same shared OEE definition): the main write paths
            # enforce good <= total, but a data-entry slip / raw-SQL write can store
            # good_count > total_count, and an uncapped good/total would print a
            # quality above 100% — a figure the data can't support. min(ratio, 1)
            # keeps every normal record unchanged (good <= total -> ratio <= 1).
            quality = round(min(good_count / total_count, 1) * 100)
        else:
            q = quality_by_machine.get(machine.id)
            if q and q["inspected"] > 0:
                quality = round((q["passed"] / q["inspected"]) * 100)
            else:
                quality = 95

        # FLOOR every component at 0 as well as capping it at 100 — the SAME
        # symmetric clamp the shared OEE helper (analytics_engine.pooled_oee_from_sums
        # / calculate_oee_from_record, #414) and the pooled plant rollup below already
        # apply. The data branches above cap at 100% (min(ratio, 1)) but never floored:
        # the ingest rejects negatives, yet a legacy / raw-SQL / migration row can
        # still hold a negative runtime / good_count / (ideal_cycle * total), whose SUM
        # makes runtime/planned, ideal_seconds/runtime or good/total NEGATIVE — printing
        # e.g. quality -50% and an OEE below zero for THIS machine row, while the pooled
        # plant rollup on the SAME response floors the identical sums to 0 (rule-3: the
        # per-machine parts must reconcile with the pooled whole). The inspection-based
        # quality fallback was uncapped in BOTH directions (a raw-SQL row's passed can
        # exceed or undershoot inspected), so it gets the same treatment. max(0, min(100,
        # x)) is a strict no-op on every well-formed machine.
        availability = max(0, min(100, availability))
        performance = max(0, min(100, performance))
        quality = max(0, min(100, quality))

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
    # Pooling is a ratio of sums, so the per-machine totals already gathered add
    # up to the plant totals — no second pass over the rows, and the formula
    # still lives in exactly one place (analytics_engine.pooled_oee_from_sums,
    # which pooled_oee itself now delegates to).
    plant = pooled_oee_from_sums(
        planned=sum(totals[0] for totals in production_by_machine.values()),
        runtime=sum(totals[1] for totals in production_by_machine.values()),
        ideal_seconds=sum(totals[2] for totals in production_by_machine.values()),
        total=sum(totals[3] for totals in production_by_machine.values()),
        good=sum(totals[4] for totals in production_by_machine.values()),
        has_data=bool(production_by_machine),
    )
    plant_availability = plant["availability"]
    plant_performance = plant["performance"]
    plant_quality = plant["quality"]
    plant_oee = plant["oee"]

    # Coalesce per-row NULL shift outputs to 0 (parity with build_shift_kpis and the
    # COALESCE(SUM(..),0) this endpoint already applies to its production sums): a
    # NULL target/actual on a raw-SQL/legacy row (Integer nullable=False, no default —
    # not retro-applied to old rows) made `sum(...)` raise TypeError and 500 this
    # Admin-polled endpoint, while the SQL-summed /analytics/management returned a
    # number. SQL SUM skips a NULL row, so `or 0` per row reconciles the two (rule-3).
    total_target = sum((shift.target_output or 0) for shift in shifts)
    total_actual = sum((shift.actual_output or 0) for shift in shifts)
    plan_achievement = round((total_actual / total_target) * 100) if total_target else 0

    downtime_pareto = [
        {"reason": reason, "minutes": minutes}
        for reason, minutes in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    shift_rows = []
    for shift in shifts:
        # Same per-row NULL coalesce as total_target/total_actual above: a NULL
        # target/actual made `actual / target` and the JSON payload carry None,
        # 500-ing or corrupting the per-shift chart. 0 is the honest floor for an
        # unrecorded output, matching the pooled headline just computed.
        target = shift.target_output or 0
        actual = shift.actual_output or 0
        efficiency = round((actual / target) * 100) if target else 0
        shift_rows.append(
            {
                "shift_name": shift.shift_name,
                "target_output": target,
                "actual_output": actual,
                "efficiency": efficiency,
            }
        )

    # Ordered by first appearance (min id), because the sort below is stable and
    # ties would otherwise resolve differently than they did when this loop
    # walked the table in insertion order. NULL and "" both fold into
    # "No Defect", so the totals are accumulated rather than assigned.
    #
    # The parity test cannot prove this line: SQLite happens to return groups in
    # an order that already matches, so deleting the order_by leaves it green.
    # Production is Postgres (psycopg2), where GROUP BY without ORDER BY has no
    # guaranteed order at all — a HashAggregate can hand back the groups in any
    # order it likes, and the defect chart would reshuffle between requests.
    quality_defects = {}
    for category, failed in (
        db.query(
            models.QualityInspection.defect_category,
            func.coalesce(func.sum(models.QualityInspection.failed_quantity), 0),
        )
        .group_by(models.QualityInspection.defect_category)
        .order_by(func.min(models.QualityInspection.id))
        .all()
    ):
        key = category or "No Defect"
        quality_defects[key] = quality_defects.get(key, 0) + failed

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
    # The machine roster and the factory-layout nodes are the bounded reference
    # sets this snapshot is drawn on (a plant has dozens of machines and layout
    # nodes, not a per-event ledger that grows without limit), so they're read
    # whole — the same way the sibling /analytics/inventory reads its item
    # catalogue. The status headline and the zone rollup both need the identical
    # in-memory machine map, so resolve it once here.
    machines = db.query(models.Machine).all()
    nodes = db.query(models.FactoryLayoutNode).all()

    # Plant downtime total. downtime_logs carries a FREE-TEXT duration
    # ("2 hrs 15 min") that only parse_duration_to_minutes can read — there is no
    # SQL SUM for it — so this one total is still summed in Python, exactly as
    # every other downtime rollup does (build_management_summary, executive-oee).
    # It is the single scan here that genuinely can't move into SQL; the counts and
    # the quality sums below all now do, so the endpoint no longer hydrates five
    # growing tables into Python just to count them.
    downtime_logs = db.query(models.DowntimeLog).all()
    total_downtime = sum(parse_duration_to_minutes(log.duration) for log in downtime_logs)

    running = len([machine for machine in machines if machine.status == "Running"])
    breakdown = len([machine for machine in machines if machine.status == "Breakdown"])
    idle = len([machine for machine in machines if machine.status == "Idle"])
    maintenance = len([machine for machine in machines if machine.status == "Maintenance"])

    # The four counts and the quality rate below used to hydrate a whole growing
    # table into Python just to count/sum it with a list comprehension (rule-4
    # antipattern) — the same fix already applied to /analytics/work-orders,
    # /analytics/production-plans, /analytics/escalations and /analytics/quality.
    # Each model is in tenancy.SCOPED_MODELS, so the do_orm_execute hook (ADR-0002)
    # tenant-scopes every aggregate SELECT below exactly as it did the old .all()
    # scan, and each predicate is written to keep the numbers BYTE-IDENTICAL to the
    # Python versions, including the NULL-status subtleties.

    # active_work_orders: status IN (Running, Planned). A NULL status is not IN the
    # set (SQL and the old Python `in [...]` agree), so it stays excluded.
    active_work_orders = db.query(func.count(models.WorkOrder.id)).filter(
        models.WorkOrder.status.in_(["Running", "Planned"])
    ).scalar() or 0

    # behind_plans: status == "Behind" (a NULL status is excluded either way).
    behind_plans = db.query(func.count(models.ProductionPlan.id)).filter(
        models.ProductionPlan.status == "Behind"
    ).scalar() or 0

    # open_escalations = not Resolved. status is String default="Open" (not NOT
    # NULL); the old Python `row.status != "Resolved"` counted a NULL-status row as
    # open (None != "Resolved" is True), but SQL's `status != 'Resolved'` is NULL —
    # not TRUE — for a NULL status and would silently drop it. OR the NULL back in
    # to keep the same basis (the same subtlety as the late-order / review-due
    # counts, #295/#298).
    open_escalations = db.query(func.count(models.Escalation.id)).filter(
        or_(
            models.Escalation.status.is_(None),
            models.Escalation.status != "Resolved",
        )
    ).scalar() or 0

    # low_stock: current_stock <= reorder_level. Both are Column(Integer, default=0)
    # WITHOUT nullable=False, so either can be a real SQL NULL; COALESCE(.., 0)
    # reproduces the old `(current_stock or 0) <= (reorder_level or 0)` exactly (a
    # NULL reads as the column's own default of 0) without pulling the catalogue
    # into Python — matching the NULL guard /analytics/inventory (#286) applies.
    low_stock = db.query(func.count(models.InventoryItem.id)).filter(
        func.coalesce(models.InventoryItem.current_stock, 0)
        <= func.coalesce(models.InventoryItem.reorder_level, 0)
    ).scalar() or 0

    # Quality fail rate over the whole inspection register. inspected_quantity is
    # nullable=False; failed_quantity is Column(Integer, default=0) WITHOUT
    # nullable=False, so COALESCE(SUM(failed), 0) guards a real NULL (and the
    # empty-table NULL) exactly as the old `failed_quantity or 0` did. Numerator and
    # denominator read the same rows, so the rate reconciles (rule 3); 0/0 -> 0.
    inspected, failed = db.query(
        func.coalesce(func.sum(models.QualityInspection.inspected_quantity), 0),
        func.coalesce(func.sum(models.QualityInspection.failed_quantity), 0),
    ).one()
    inspected = int(inspected)
    failed = int(failed)
    quality_fail_rate = round((failed / inspected) * 100) if inspected else 0

    machine_map = {machine.id: machine for machine in machines}
    zone_summary = {}

    for node in nodes:
        # FactoryLayoutNode.zone is Column(String, default="Production") WITHOUT
        # nullable=False, so a raw-SQL / migration / cleared-field row can hold NULL.
        # This rollup reads node.zone RAW (it returns a plain dict, bypassing the
        # FactoryLayoutNodeResponse heal), so a NULL would surface here as a zone
        # bucket literally labelled null — a display of the missing value, not a real
        # zone (ADR-0010: a NULL/default must never leak into a displayed value).
        # Coalesce to the column's own declared default, matching the response heal.
        node_zone = node.zone or "Production"
        zone = zone_summary.setdefault(
            node_zone,
            {"zone": node_zone, "nodes": 0, "running": 0, "breakdown": 0, "idle": 0, "maintenance": 0},
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
    # streams every row back just to count the late ones. status is String
    # default="Open" (not NOT NULL), so a raw-SQL / migration / cleared-field row can
    # hold a NULL: SQL's `status != 'Completed'` is NULL — not TRUE — for that row and
    # would silently DROP an unfinished, past-dated task from the count. OR the NULL
    # back in (a NULL status is not-Completed, i.e. still overdue) to keep the same
    # basis as the escalation generator that acts on this very set, and matching the
    # late-order / review-due / open-escalation NULL-status convention (#295/#298).
    overdue = db.query(func.count(models.MaintenanceTask.id)).filter(
        models.MaintenanceTask.planned_date < today,
        or_(
            models.MaintenanceTask.status.is_(None),
            models.MaintenanceTask.status != "Completed",
        ),
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
    # Aggregate the growing production_schedules table in SQL rather than hydrating
    # every row into Python to bucket/sum it with list comprehensions (rule-4
    # antipattern; the same GROUP BY / COALESCE(SUM) fix already applied to
    # /analytics/work-orders (#275), /analytics/maintenance (#300),
    # /analytics/escalations (#288) and /analytics/customer-orders (#295)). This
    # endpoint kept the N+1 machine lookup dropped (#273) but still pulled the whole
    # table in. ProductionSchedule is in SCOPED_MODELS, so the do_orm_execute hook
    # (ADR-0002) tenant-scopes every aggregate SELECT below exactly as it did the
    # old .all() scan.
    status_counts = dict(
        db.query(models.ProductionSchedule.status, func.count())
        .group_by(models.ProductionSchedule.status)
        .all()
    )

    # planned_quantity is nullable=False, but SUM over an empty book is still NULL;
    # estimated_minutes is Column(Integer, default=480) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can be a true NULL,
    # which the old Python sum() coalesced with `or 0`. COALESCE(.., 0) does the same
    # in SQL AND never sees an empty-table NULL either.
    total_schedules, total_quantity, total_minutes = db.query(
        func.count(models.ProductionSchedule.id),
        func.coalesce(func.sum(models.ProductionSchedule.planned_quantity), 0),
        func.coalesce(func.sum(models.ProductionSchedule.estimated_minutes), 0),
    ).one()
    total_quantity = int(total_quantity)
    total_minutes = int(total_minutes)

    # Per-machine load minutes: one GROUP BY on machine_id, then a single name
    # lookup (tenant-scoped like the aggregate itself), then merge by name in Python
    # so two machine rows that share a name land in one bucket — exactly the
    # maintenance rollup's shape. An unknown machine_id keeps its "Machine {id}"
    # fallback label. Minutes coalesce so a NULL estimate reads as 0, keeping the
    # per-machine load on the same basis as total_minutes (parts sum to the whole).
    machine_id_minutes = (
        db.query(models.ProductionSchedule.machine_id,
                 func.coalesce(func.sum(models.ProductionSchedule.estimated_minutes), 0))
        .group_by(models.ProductionSchedule.machine_id)
        .all()
    )
    machine_names = dict(db.query(models.Machine.id, models.Machine.name).all())
    machine_load = {}
    for machine_id, minutes in machine_id_minutes:
        name = machine_names.get(machine_id, f"Machine {machine_id}")
        machine_load[name] = machine_load.get(name, 0) + int(minutes)

    # Per-shift planned quantity, grouped in SQL on the same planned-quantity basis
    # as total_quantity (parts sum to the whole).
    shift_load = {}
    for shift_name, qty in (
        db.query(models.ProductionSchedule.shift_name,
                 func.coalesce(func.sum(models.ProductionSchedule.planned_quantity), 0))
        .group_by(models.ProductionSchedule.shift_name)
        .all()
    ):
        shift_load[shift_name] = shift_load.get(shift_name, 0) + int(qty)

    # Heaviest load first; break ties by machine name so the ranking is fully
    # deterministic (the GROUP BY no longer returns rows in schedule-insertion
    # order, so a raw stable sort could order equal-load machines arbitrarily).
    bottlenecks = [
        {"machine": name, "load_minutes": minutes}
        for name, minutes in sorted(machine_load.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "total_schedules": total_schedules,
        "scheduled": status_counts.get("Scheduled", 0),
        # "Running" and "In Progress" are two spellings of the SAME state (the
        # schedule is actively being worked) written by two code paths in this
        # app: the UI status dropdown (SchedulingSection) writes "Running", the
        # factory simulator (_schedules) writes "In Progress". Reading the "Running"
        # bucket alone reported 0 in-progress schedules on a seeded/simulated
        # plant while those rows sat plainly in the schedule table below it — and,
        # because total_schedules counts EVERY row, the four named buckets no
        # longer summed to the headline (an "In Progress" row landed in none of
        # them). Fold the synonym so the Running headline counts every in-progress
        # schedule and the breakdown reconciles with the total (rule-1: one metric,
        # not two spellings; rule-3: a breakdown must reconcile with its headline) —
        # exactly the fold the sibling customer-order rollup already applies to
        # "Partial"/"Partially Dispatched" (#444).
        "running": status_counts.get("Running", 0) + status_counts.get("In Progress", 0),
        "completed": status_counts.get("Completed", 0),
        "delayed": status_counts.get("Delayed", 0),
        "total_quantity": total_quantity,
        "total_minutes": total_minutes,
        "machine_load": machine_load,
        "shift_load": shift_load,
        "bottlenecks": bottlenecks,
    }


@router.get("/analytics/iot-command")
def get_iot_command_center(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    machines = db.query(models.Machine).all()
    # The last-300 window is a DISPLAY bound for latest_signals only — it must not
    # define the headline counts. iot_telemetry is an unbounded, never-pruned
    # growing table (factory_simulator.tick_iot appends every tick), so once it
    # exceeds 300 rows the old `len(telemetry)` reported a flat 300 — the .limit()
    # cap leaking straight into the displayed "Signals" KPI (IoTCommandSection) —
    # and `len(set(machine_id))` counted only reporters inside that same window,
    # both frozen while the sibling "Machines" stayed a true roster total (an
    # inconsistent basis). Report the true totals with bounded SQL aggregates
    # instead: IoTTelemetry is in SCOPED_MODELS, so the do_orm_execute hook
    # (ADR-0002) tenant-scopes these COUNTs exactly as it scopes the window scan —
    # the same aggregate pattern the operator-terminal rollup already uses.
    total_signals = db.query(func.count(models.IoTTelemetry.id)).scalar() or 0
    live_machines = db.query(
        func.count(func.distinct(models.IoTTelemetry.machine_id))
    ).scalar() or 0
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
        "signals": total_signals,
        "live_machines": live_machines,
        "latest_signals": latest_rows,
    }


@router.get("/analytics/ai-insights")
def get_ai_insights(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Aggregate the growing ai_recommendations table in SQL, not by hydrating every
    # row and bucketing it with list comprehensions (rule-4 antipattern, the same
    # GROUP BY fix already applied to /analytics/work-orders #275,
    # /analytics/production-plans, /analytics/escalations #288 and the machine-state
    # summary #328 — this endpoint was simply missed). AIRecommendation is in
    # SCOPED_MODELS, so the do_orm_execute hook (ADR-0002) tenant-scopes each
    # aggregate SELECT below exactly as it did the old .all() scan.
    #
    # status default "Open" and severity default "Medium" are both nullable (no
    # nullable=False), so a row written by raw SQL / a migration / a cleared update
    # can carry a NULL status or severity. The old loop counted such a row in
    # `total` (len(rows)) but in no bucket (None never == "Open"); GROUP BY keys it
    # under None, which .get("Open"/... ) never reads, so total stays >= the sum of
    # either partition — identical semantics, one pass over the table instead of a
    # full hydrate.
    status_counts = dict(
        db.query(models.AIRecommendation.status, func.count())
        .group_by(models.AIRecommendation.status)
        .all()
    )
    severity_counts = dict(
        db.query(models.AIRecommendation.severity, func.count())
        .group_by(models.AIRecommendation.severity)
        .all()
    )
    total = db.query(func.count(models.AIRecommendation.id)).scalar() or 0
    return {
        "total": total,
        "open": status_counts.get("Open", 0),
        "acknowledged": status_counts.get("Acknowledged", 0),
        "closed": status_counts.get("Closed", 0),
        "critical": severity_counts.get("Critical", 0),
        "high": severity_counts.get("High", 0),
        "medium": severity_counts.get("Medium", 0),
        "low": severity_counts.get("Low", 0),
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
    # Count in SQL, never hydrate the whole (growing) table into Python just to
    # take len()/a filtered len() of it (rule-4 antipattern; the same COUNT/GROUP
    # BY fix already applied to the sibling rollups — /analytics/work-orders,
    # /escalations #288, /analytics/quality #299). audit_logs in particular grows
    # without bound (every login / mutation appends a row), so a full-table scan of
    # it pulled the tenant's entire history into memory on every dashboard poll —
    # the heaviest of the six scans this endpoint used to do. Each model here is
    # either in SCOPED_MODELS or global exactly as before, so the do_orm_execute
    # hook (ADR-0002) tenant-scopes each COUNT precisely as the prior scan was.
    machines = db.query(func.count(models.Machine.id)).scalar() or 0
    users = db.query(func.count(models.User.id)).scalar() or 0
    alerts = db.query(func.count(models.Alert.id)).scalar() or 0
    # "Open" = every escalation NOT explicitly Resolved. status is
    # Column(String, default="Open") WITHOUT nullable=False, so a raw-SQL /
    # migration / cleared write can store NULL. The old Python `row.status !=
    # "Resolved"` counted a NULL as open (None != "Resolved" is True), but SQL's
    # `status != 'Resolved'` is NULL — not TRUE — for a NULL status and would
    # silently drop that row. OR the NULL back in to keep the exact same basis.
    open_escalations = db.query(func.count(models.Escalation.id)).filter(
        or_(
            models.Escalation.status.is_(None),
            models.Escalation.status != "Resolved",
        )
    ).scalar() or 0
    # "Unread" is an equality match, so a NULL status is excluded either way
    # (Python `None == "Unread"` and SQL `status = 'Unread'` are both false/NULL)
    # — parity holds without an extra NULL clause.
    unread_notifications = db.query(func.count(models.Notification.id)).filter(
        models.Notification.status == "Unread"
    ).scalar() or 0
    audit_logs = db.query(func.count(models.AuditLog.id)).scalar() or 0

    return {
        "api_status": "Healthy",
        "database_status": "Connected",
        "machines": machines,
        "users": users,
        "alerts": alerts,
        "open_escalations": open_escalations,
        "unread_notifications": unread_notifications,
        "audit_logs": audit_logs,
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
    # Count/sum in SQL, never hydrate EIGHT whole (growing) tables into Python just
    # to take len()/a filtered len()/sum() of them (rule-4 antipattern; the same
    # COUNT / COALESCE(SUM(..),0) fix already applied to the sibling rollups —
    # /analytics/system-health, /analytics/work-orders, /escalations #288,
    # /analytics/quality #299). Each model here is in SCOPED_MODELS, so the
    # do_orm_execute hook (ADR-0002) tenant-scopes every aggregate exactly as it
    # scoped the prior whole-table scans — the numbers are identical, only the
    # per-row hydration into Python is gone.
    #
    # Several count columns are Column(Integer, default=0) WITHOUT nullable=False
    # (passed_quantity, dispatched_quantity, current_stock, reorder_level, amount).
    # The ORM default only fills a value the inserter omitted, so a row written by
    # raw SQL, a migration, or an update that clears the field can legitimately be
    # NULL. COALESCE(SUM(..),0) reproduces the old `sum(col or 0)` exactly (SUM
    # skips NULLs, and COALESCE turns an all-NULL/empty group's NULL sum into 0),
    # and COALESCE in the low-stock predicate mirrors `(current_stock or 0) <=
    # (reorder_level or 0)`. inspected_quantity and order_quantity are nullable=False
    # and stay the divisors, so the divide-by-zero guards remain.
    machine_count = db.query(func.count(models.Machine.id)).scalar() or 0
    # Equality match, so a NULL status is excluded either way (Python
    # `None == "Running"` and SQL `status = 'Running'` are both false/NULL) —
    # parity holds without an extra NULL clause.
    running_machines = db.query(func.count(models.Machine.id)).filter(
        models.Machine.status == "Running"
    ).scalar() or 0
    work_orders = db.query(func.count(models.WorkOrder.id)).scalar() or 0
    production_plans = db.query(func.count(models.ProductionPlan.id)).scalar() or 0
    customer_orders = db.query(func.count(models.CustomerOrder.id)).scalar() or 0
    purchase_orders = db.query(func.count(models.PurchaseOrder.id)).scalar() or 0

    # int() so a DB that returns Decimal for SUM (Postgres) matches the plain-int
    # payload the frontend type expects and divides cleanly.
    inspected = int(db.query(func.coalesce(func.sum(models.QualityInspection.inspected_quantity), 0)).scalar() or 0)
    passed = int(db.query(func.coalesce(func.sum(models.QualityInspection.passed_quantity), 0)).scalar() or 0)
    quality_rate = round((passed / inspected) * 100) if inspected else 0

    order_qty = int(db.query(func.coalesce(func.sum(models.CustomerOrder.order_quantity), 0)).scalar() or 0)
    dispatched_qty = int(db.query(func.coalesce(func.sum(models.CustomerOrder.dispatched_quantity), 0)).scalar() or 0)
    dispatch_rate = round((dispatched_qty / order_qty) * 100) if order_qty else 0

    # Low stock = COALESCE(current_stock,0) <= COALESCE(reorder_level,0), counted in
    # SQL (a NULL on either side collapses to 0, matching the old Python predicate).
    low_stock_items = db.query(func.count(models.InventoryItem.id)).filter(
        func.coalesce(models.InventoryItem.current_stock, 0)
        <= func.coalesce(models.InventoryItem.reorder_level, 0)
    ).scalar() or 0

    total_cost = int(db.query(func.coalesce(func.sum(models.CostRecord.amount), 0)).scalar() or 0)

    return {
        "machine_count": machine_count,
        "running_machines": running_machines,
        "work_orders": work_orders,
        "production_plans": production_plans,
        "quality_rate": quality_rate,
        "low_stock_items": low_stock_items,
        "customer_orders": customer_orders,
        "dispatch_rate": dispatch_rate,
        "purchase_orders": purchase_orders,
        "total_cost": total_cost,
    }


@router.get("/analytics/industrial-gateway")
def get_industrial_gateway_analytics(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    devices = db.query(models.IndustrialDevice).all()
    # The last-500 window is a DISPLAY bound for latest_signals only — it must not
    # define the "signals" headline count. industrial_signals is a growing table
    # (industrial_adapters.tick_industrial appends every poll and only trims it
    # back to ~1,000 rows), so once it holds more than 500 rows the old
    # `len(signals)` reported a flat 500 — the .limit() cap leaking straight into
    # the displayed "Signals" KPI, understating the true count while the sibling
    # "devices"/"mappings" (fully hydrated) stayed true totals (an inconsistent
    # basis). Report the true total with a bounded SQL COUNT instead, exactly as
    # the sibling /analytics/iot-command endpoint already does for its telemetry
    # count. IndustrialSignal is in SCOPED_MODELS, so the do_orm_execute hook
    # (ADR-0002) tenant-scopes this COUNT exactly as it scopes the window scan.
    total_signals = db.query(func.count(models.IndustrialSignal.id)).scalar() or 0
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
        "signals": total_signals,
        "mappings": len(mappings),
        "enabled_mappings": len([m for m in mappings if m.enabled == "Yes"]),
        "latest_signals": latest[:30],
    }
