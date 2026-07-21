"""AI-recommendation routes — the copilot's suggestion queue.

List AI recommendations, update one (accept / dismiss), and (re)generate the set
from current shop-floor state. Plain CRUD over models.AIRecommendation plus a
rules pass that reads machines / downtime / inventory / plans / quality; the one
shared helper is parse_duration_to_minutes (analytics_engine). Tenant scoping is
the ORM chokepoint (ADR-0002). Peeled out of main.py per ADR-0009.

Named recommendations_routes (not ai_routes) to avoid confusion with the `ai`
read-model package.
"""
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


router = APIRouter(tags=["AI Recommendations"])


@router.get("/ai/recommendations", response_model=List[schemas.AIRecommendationResponse])
def get_ai_recommendations(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    return db.query(models.AIRecommendation).order_by(models.AIRecommendation.id.desc()).limit(300).all()


@router.patch("/ai/recommendations/{recommendation_id}", response_model=schemas.AIRecommendationResponse)
def update_ai_recommendation(recommendation_id: int, payload: schemas.AIRecommendationUpdate, db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):
    row = db.query(models.AIRecommendation).filter(models.AIRecommendation.id == recommendation_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="AI recommendation not found")
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    db.refresh(row)
    return row


@router.post("/ai/generate-recommendations")
def generate_ai_recommendations(db: Session = Depends(_get_db), current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    inventory_items = db.query(models.InventoryItem).all()
    production_plans = db.query(models.ProductionPlan).all()
    quality_rows = db.query(models.QualityInspection).all()
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
            add_rec("Predictive Maintenance", "High", f"Maintenance risk detected on {machine.name}", f"{machine.name} has {downtime_minutes} minutes downtime or breakdown state. Schedule inspection.", machine.id, 86)

        if machine.utilization < 45:
            add_rec("Utilization Optimization", "Medium", f"Low utilization on {machine.name}", f"{machine.name} utilization is {machine.utilization}%. Rebalance schedule.", machine.id, 74)

    for item in inventory_items:
        if item.current_stock <= item.reorder_level:
            add_rec("Inventory Forecast", "High" if item.current_stock == 0 else "Medium", f"Inventory replenishment recommended for {item.item_code}", f"{item.item_name} is at {item.current_stock} {item.unit}; reorder level is {item.reorder_level}.", None, 82)

    for plan in production_plans:
        if plan.status == "Behind":
            add_rec("Production Delay Prediction", "High", f"Delay risk on plan {plan.plan_no}", f"Plan {plan.plan_no} is behind schedule. Review capacity/materials.", plan.machine_id, 80)

    inspected = sum(row.inspected_quantity for row in quality_rows)
    failed = sum(row.failed_quantity for row in quality_rows)
    fail_rate = round((failed / inspected) * 100) if inspected else 0
    if fail_rate >= 10:
        add_rec("Quality Prediction", "High", "Quality failure trend detected", f"Current fail rate is {fail_rate}%. Trigger root-cause analysis.", None, 84)

    db.commit()
    return {"created": created}
