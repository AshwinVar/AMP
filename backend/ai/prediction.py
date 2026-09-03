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

from sqlalchemy import func

import models
from duration import parse_duration_to_minutes
from predictive_engine import ACTIVE_WORK_ORDER_STATUSES, calculate_predictive_risk

name = "prediction"

RISK_WINDOW_DAYS = 30


def assess_risk(machines, downtime_logs, production_records, machine_events,
                work_orders, aggregates=None):
    """Score failure risk for the given machines. Delegates to the rule engine."""
    return calculate_predictive_risk(
        machines, downtime_logs, production_records, machine_events, work_orders,
        aggregates=aggregates,
    )


def _history_aggregates(db, cutoff):
    """The three windowed histories, reduced to per-machine counters in SQL.

    Every figure here is what calculate_predictive_risk's loops produced from
    the rows, computed the same way:

      * duration is a STRING ("15 min"), so it cannot be SUMmed -- but it CAN be
        grouped. Each DISTINCT (machine, reason, duration) is parsed once and
        multiplied by its count, which is arithmetically identical to parsing
        every row. Same technique as analytics_engine.downtime_aggregates.
      * a breakdown is counted from BOTH sources, exactly as before: a downtime
        row whose RAW reason lowercases to "breakdown", and a machine event
        whose new_status is exactly "Breakdown".
      * SUM over no rows is NULL, so the counts are coalesced to 0 -- the same
        reading _int() gives a missing count.
    """
    downtime_minutes, downtime_events, breakdown_events = {}, {}, {}
    for machine_id, reason, duration, count in (
            db.query(models.DowntimeLog.machine_id, models.DowntimeLog.reason,
                     models.DowntimeLog.duration, func.count())
              .filter(models.DowntimeLog.created_at >= cutoff)
              .group_by(models.DowntimeLog.machine_id, models.DowntimeLog.reason,
                        models.DowntimeLog.duration).all()):
        downtime_minutes[machine_id] = (downtime_minutes.get(machine_id, 0)
                                        + parse_duration_to_minutes(duration) * count)
        downtime_events[machine_id] = downtime_events.get(machine_id, 0) + count
        if str(reason).lower() == "breakdown":
            breakdown_events[machine_id] = breakdown_events.get(machine_id, 0) + count

    for machine_id, count in (
            db.query(models.MachineEvent.machine_id, func.count())
              .filter(models.MachineEvent.created_at >= cutoff,
                      models.MachineEvent.new_status == "Breakdown")
              .group_by(models.MachineEvent.machine_id).all()):
        breakdown_events[machine_id] = breakdown_events.get(machine_id, 0) + count

    rejects, totals = {}, {}
    for machine_id, rejected, total in (
            db.query(models.ProductionRecord.machine_id,
                     func.coalesce(func.sum(models.ProductionRecord.rejected_count), 0),
                     func.coalesce(func.sum(models.ProductionRecord.total_count), 0))
              .filter(models.ProductionRecord.created_at >= cutoff)
              .group_by(models.ProductionRecord.machine_id).all()):
        rejects[machine_id] = int(rejected or 0)
        totals[machine_id] = int(total or 0)

    return {"downtime_minutes": downtime_minutes,
            "downtime_events": downtime_events,
            "breakdown_events": breakdown_events,
            "rejects": rejects, "totals": totals}


def assess_from_db(db):
    """Pull the inputs from the DB (history bounded to the risk window) and
    score them.

    Tenant scoping is applied automatically at the query layer (ADR-0002), so in
    a request/subscriber context this returns only the caller's machines.
    """
    cutoff = datetime.utcnow() - timedelta(days=RISK_WINDOW_DAYS)
    return assess_risk(
        db.query(models.Machine).all(),
        # The three windowed histories are REDUCED IN SQL rather than hydrated.
        # A 30-day window is not a bound on a table that gains thousands of rows
        # a day: measured at 200 machines it held 63,036 downtime rows and
        # 43,198 machine events, ~106,000 ORM objects built on every three-second
        # poll. /machine-health took 1633 ms, of which 76 ms was SQL. See
        # _history_aggregates and test_predictive_risk_aggregates.py.
        (), (), (),
        # Work-order load is point-in-time (unwindowed by date), but only ACTIVE
        # orders carry outstanding demand — the engine sums pressure over exactly
        # ACTIVE_WORK_ORDER_STATUSES. Bound that in SQL rather than hydrating the
        # whole (growing) work_orders table and filtering in Python (rule-4): the
        # Completed/Planned rows are never used, so this returns identical risk
        # while reading only the handful of active orders. Filtered on the indexed
        # work_orders.status column (main.py _ensure_index). WorkOrder is in
        # SCOPED_MODELS, so this stays tenant-scoped by the ORM hook (ADR-0002)
        # exactly as the old .all() scan did.
        db.query(models.WorkOrder)
        .filter(models.WorkOrder.status.in_(ACTIVE_WORK_ORDER_STATUSES))
        .all(),
        aggregates=_history_aggregates(db, cutoff),
    )


def risk_for_machine(db, machine_id):
    """The risk row for one machine, or ``None``. Used by event subscribers."""
    for row in assess_from_db(db):
        if row["machine_id"] == machine_id:
            return row
    return None
