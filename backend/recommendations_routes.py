"""AI-recommendation routes — the copilot's suggestion queue.

List AI recommendations, update one (accept / dismiss), and (re)generate the set
from current shop-floor state. Plain CRUD over models.AIRecommendation plus a
rules pass that reads machines / downtime / inventory / plans / quality; the one
shared helper is parse_duration_to_minutes (analytics_engine). Tenant scoping is
the ORM chokepoint (ADR-0002). Peeled out of main.py per ADR-0009.

Named recommendations_routes (not ai_routes) to avoid confusion with the `ai`
read-model package.
"""
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from analytics_engine import parse_duration_to_minutes
from auth import get_current_user, require_roles
from database import SessionLocal


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/ai", tags=["AI Recommendations"])

# Recommendations describe the shop floor's RECENT condition, so the two
# history-based rules — accumulated downtime and the quality fail rate — are
# windowed to the last WINDOW_DAYS and bounded SQL-side on the indexed
# created_at. Two bugs otherwise: (1) an unbounded scan of the growing
# downtime_logs / quality_inspections tables, and (2) LIFETIME accumulation
# labelled as the machine's current state — every long-lived machine eventually
# crosses the 120-minute downtime threshold and a fail rate labelled "current"
# would really be an all-time average that a single bad week months ago keeps
# elevated. Current point-in-time state (machine status/utilization, stock
# levels, a plan's Behind flag) is not history and stays unwindowed.
RECOMMENDATION_WINDOW_DAYS = 30


@router.get("/recommendations", response_model=List[schemas.AIRecommendationResponse])
def get_ai_recommendations(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.AIRecommendation).order_by(models.AIRecommendation.id.desc()).limit(300).all()


@router.patch("/recommendations/{recommendation_id}", response_model=schemas.AIRecommendationResponse)
def update_ai_recommendation(recommendation_id: int, payload: schemas.AIRecommendationUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.AIRecommendation).filter(models.AIRecommendation.id == recommendation_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="AI recommendation not found")
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    db.refresh(row)
    return row


@router.post("/generate-recommendations")
def generate_ai_recommendations(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    cutoff = datetime.utcnow() - timedelta(days=RECOMMENDATION_WINDOW_DAYS)
    machines = db.query(models.Machine).all()
    downtime_logs = (
        db.query(models.DowntimeLog)
        .filter(models.DowntimeLog.created_at >= cutoff)
        .all()
    )
    inventory_items = db.query(models.InventoryItem).all()
    production_plans = db.query(models.ProductionPlan).all()
    quality_rows = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.created_at >= cutoff)
        .all()
    )
    created = 0

    def add_rec(kind, severity, title, message, machine_id=None, confidence=78):
        nonlocal created
        existing = db.query(models.AIRecommendation).filter(models.AIRecommendation.title == title, models.AIRecommendation.status != "Closed").first()
        if existing:
            return
        db.add(models.AIRecommendation(
            recommendation_type=kind,
            severity=severity,
            title=title,
            message=message,
            related_machine_id=machine_id,
            confidence=confidence,
            status="Open",
        ))
        created += 1

    for machine in machines:
        machine_downtime = [log for log in downtime_logs if log.machine_id == machine.id]
        downtime_minutes = sum(parse_duration_to_minutes(log.duration) for log in machine_downtime)

        if machine.status == "Breakdown" or downtime_minutes > 120:
            add_rec("Predictive Maintenance", "High", f"Maintenance risk detected on {machine.name}", f"{machine.name} has {downtime_minutes} minutes downtime in the last {RECOMMENDATION_WINDOW_DAYS} days or is in breakdown state. Schedule inspection.", machine.id, 86)

        # Machine.utilization is Column(Integer, default=0) WITHOUT nullable=False —
        # a row written by raw SQL / a migration / an update that clears the field
        # can be NULL, and `None < 45` raised TypeError, 500-ing this endpoint. A
        # NULL means "no reading", which is NOT a measured 0%: skip the utilization
        # rule rather than fabricate a "low utilization" one from the column default
        # (ADR-0010 — a default must never leak into a displayed value), matching
        # build_smart_alerts / generate_alerts.
        if machine.utilization is not None and machine.utilization < 45:
            add_rec("Utilization Optimization", "Medium", f"Low utilization on {machine.name}", f"{machine.name} utilization is {machine.utilization}%. Rebalance schedule.", machine.id, 74)

    for item in inventory_items:
        # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
        # nullable=False, so either can be NULL and `None <= None` raised TypeError.
        # A NULL level can't say whether the item is low, so exclude it — matching
        # generate_low_stock_escalations, whose SQL `current_stock <= reorder_level`
        # filter yields NULL (excluded) when either side is NULL.
        if item.current_stock is not None and item.reorder_level is not None and item.current_stock <= item.reorder_level:
            add_rec("Inventory Forecast", "High" if item.current_stock == 0 else "Medium", f"Inventory replenishment recommended for {item.item_code}", f"{item.item_name} is at {item.current_stock} {item.unit}; reorder level is {item.reorder_level}.", None, 82)

    for plan in production_plans:
        if plan.status == "Behind":
            add_rec("Production Delay Prediction", "High", f"Delay risk on plan {plan.plan_no}", f"Plan {plan.plan_no} is behind schedule. Review capacity/materials.", plan.machine_id, 80)

    # inspected_quantity is nullable=False, but failed_quantity is
    # Column(Integer, default=0) WITHOUT nullable=False — a NULL (raw write /
    # migration) made `sum(... + None)` raise TypeError. NULL means "no failures
    # recorded", i.e. 0, so coalesce; numerator and denominator stay on the same
    # windowed rows so the rate reconciles.
    inspected = sum((row.inspected_quantity or 0) for row in quality_rows)
    failed = sum((row.failed_quantity or 0) for row in quality_rows)
    fail_rate = round((failed / inspected) * 100) if inspected else 0
    if fail_rate >= 10:
        add_rec("Quality Prediction", "High", "Quality failure trend detected", f"Fail rate over the last {RECOMMENDATION_WINDOW_DAYS} days is {fail_rate}%. Trigger root-cause analysis.", None, 84)

    db.commit()
    return {"created": created}
