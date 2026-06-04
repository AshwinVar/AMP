# Add these imports in backend/main.py if missing:
# from datetime import date

@app.get("/production-plans")
def get_production_plans(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return (
        db.query(models.ProductionPlan)
        .order_by(models.ProductionPlan.id.desc())
        .limit(200)
        .all()
    )


@app.post("/production-plans")
def create_production_plan(
    plan: schemas.ProductionPlanCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor"])),
):
    machine = db.query(models.Machine).filter(models.Machine.id == plan.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    work_order = db.query(models.WorkOrder).filter(models.WorkOrder.id == plan.work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    existing = db.query(models.ProductionPlan).filter(models.ProductionPlan.plan_no == plan.plan_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Plan number already exists")

    new_plan = models.ProductionPlan(**plan.model_dump())
    db.add(new_plan)
    db.commit()
    db.refresh(new_plan)

    return new_plan


@app.patch("/production-plans/{plan_id}")
def update_production_plan(
    plan_id: int,
    payload: schemas.ProductionPlanUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"])),
):
    plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan_id).first()

    if not plan:
        raise HTTPException(status_code=404, detail="Production plan not found")

    if payload.actual_quantity is not None:
        plan.actual_quantity = payload.actual_quantity
        if plan.actual_quantity >= plan.planned_quantity:
            plan.status = "Completed"

    if payload.status is not None:
        plan.status = payload.status

    db.commit()
    db.refresh(plan)

    return plan


@app.delete("/production-plans/{plan_id}")
def delete_production_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(["Admin"])),
):
    plan = db.query(models.ProductionPlan).filter(models.ProductionPlan.id == plan_id).first()

    if not plan:
        raise HTTPException(status_code=404, detail="Production plan not found")

    db.delete(plan)
    db.commit()

    return {"message": "Production plan deleted successfully"}


@app.get("/analytics/production-plans")
def get_production_plan_analytics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
