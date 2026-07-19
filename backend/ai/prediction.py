"""Prediction — predictive-maintenance risk scoring (ADR-0003).

Wraps the existing rule-based ``predictive_engine`` behind the AI platform.
Callers depend on the platform, not the engine, so the scorer can become an ML
model later without touching them. Rule-first today; ML/LLM slot in behind this
same surface.

History inputs are windowed to the last ``RISK_WINDOW_DAYS``: the engine scores
absolute thresholds ("120 downtime minutes", "5 downtime events"), and against
LIFETIME accumulation every long-lived machine eventually crosses them and
stays risky forever — a bad week two months ago never washed out. Risk should
describe the machine's RECENT condition, so downtime, production and breakdown
history are bounded (SQL-side, on indexed created_at), while current state
(status, utilization) and open work-order pressure are point-in-time and stay
unwindowed.
"""
from datetime import datetime, timedelta

import models
from predictive_engine import calculate_predictive_risk

name = "prediction"

RISK_WINDOW_DAYS = 30


def assess_risk(machines, downtime_logs, production_records, machine_events, work_orders):
    """Score failure risk for the given machines. Delegates to the rule engine."""
    return calculate_predictive_risk(
        machines, downtime_logs, production_records, machine_events, work_orders
    )


def assess_from_db(db):
    """Pull the inputs from the DB (history bounded to the risk window) and
    score them.

    Tenant scoping is applied automatically at the query layer (ADR-0002), so in
    a request/subscriber context this returns only the caller's machines.
    """
    cutoff = datetime.utcnow() - timedelta(days=RISK_WINDOW_DAYS)
    return assess_risk(
        db.query(models.Machine).all(),
        db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= cutoff).all(),
        db.query(models.ProductionRecord).filter(models.ProductionRecord.created_at >= cutoff).all(),
        db.query(models.MachineEvent).filter(models.MachineEvent.created_at >= cutoff).all(),
        db.query(models.WorkOrder).all(),
    )


def risk_for_machine(db, machine_id):
    """The risk row for one machine, or ``None``. Used by event subscribers."""
    for row in assess_from_db(db):
        if row["machine_id"] == machine_id:
            return row
    return None
