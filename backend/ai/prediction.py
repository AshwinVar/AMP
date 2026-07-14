"""Prediction — predictive-maintenance risk scoring (ADR-0003).

Wraps the existing rule-based ``predictive_engine`` behind the AI platform.
Behaviour is unchanged; callers now depend on the platform, not the engine, so
the scorer can become an ML model later without touching them. Rule-first today;
ML/LLM slot in behind this same surface.
"""
import models
from predictive_engine import calculate_predictive_risk

name = "prediction"


def assess_risk(machines, downtime_logs, production_records, machine_events, work_orders):
    """Score failure risk for the given machines. Delegates to the rule engine."""
    return calculate_predictive_risk(
        machines, downtime_logs, production_records, machine_events, work_orders
    )


def assess_from_db(db):
    """Pull the inputs from the DB and score them.

    Tenant scoping is applied automatically at the query layer (ADR-0002), so in
    a request/subscriber context this returns only the caller's machines.
    """
    return assess_risk(
        db.query(models.Machine).all(),
        db.query(models.DowntimeLog).all(),
        db.query(models.ProductionRecord).all(),
        db.query(models.MachineEvent).all(),
        db.query(models.WorkOrder).all(),
    )


def risk_for_machine(db, machine_id):
    """The risk row for one machine, or ``None``. Used by event subscribers."""
    for row in assess_from_db(db):
        if row["machine_id"] == machine_id:
            return row
    return None
