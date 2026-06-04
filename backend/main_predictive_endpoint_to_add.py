from predictive_engine import calculate_predictive_risk

@app.get("/analytics/predictive-maintenance")
def get_predictive_maintenance(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    production_records = db.query(models.ProductionRecord).all()
    machine_events = db.query(models.MachineEvent).all()
    work_orders = db.query(models.WorkOrder).all()

    return calculate_predictive_risk(
        machines,
        downtime_logs,
        production_records,
        machine_events,
        work_orders,
    )
