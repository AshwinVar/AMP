from collections import defaultdict

from sqlalchemy.orm import Session

import models
# Re-exported so existing `from analytics_engine import parse_duration_to_minutes`
# call sites (recommendations_routes) keep working; single source of truth.
from duration import parse_duration_to_minutes

# World-class OEE benchmarks — the single source of truth (ADR-0010). The plant
# target and the three component targets: 0.90 x 0.95 x ~0.99 ~= 0.85 OEE.
WORLD_CLASS_OEE = 85
WORLD_CLASS_COMPONENTS = {"availability": 90, "performance": 95, "quality": 99}


def biggest_lever(components: dict):
    """The one OEE component to focus on: the one furthest below its OWN
    world-class target, so closing its gap buys the most. `components` maps
    availability/performance/quality -> current %. Returns the component key, or
    None if every component is already at/above target.

    This is the single definition of 'the component to focus on', shared by the
    OEE summary's 'biggest drag' and the recovery read-model's 'biggest lever' so
    the dashboard never names two different levers on the same page. Note it is
    NOT the lowest raw component — Availability at 90% is AT target (no gap),
    while Performance at 91% is 4 points short of its 95% target."""
    gaps = {c: WORLD_CLASS_COMPONENTS[c] - components.get(c, 0) for c in WORLD_CLASS_COMPONENTS}
    key = max(gaps, key=gaps.get)
    return key if gaps[key] > 0 else None


def calculate_oee_from_record(record):
    availability = record.runtime_minutes / record.planned_minutes if record.planned_minutes else 0
    runtime_seconds = record.runtime_minutes * 60

    performance = (
        (record.ideal_cycle_time_seconds * record.total_count) / runtime_seconds
        if runtime_seconds else 0
    )

    quality = record.good_count / record.total_count if record.total_count else 0
    performance = min(performance, 1)

    return {
        "availability": round(availability * 100),
        "performance": round(performance * 100),
        "quality": round(quality * 100),
        "oee": round(availability * performance * quality * 100),
    }


def pooled_oee(records) -> dict:
    """Aggregate OEE across many records by POOLING — sum the inputs, then compute
    each component once (ratio of sums, so a machine is weighted by its volume/
    time). This is the sound way to combine OEE and the single method every
    surface uses: averaging per-record OEE (mean of ratios) over- or under-weights
    small runs and can disagree page-to-page. Each component is clamped to [0, 1].

    (calculate_oee_from_record above stays the per-record view — used for alert
    thresholds on a single machine's latest run, where pooling makes no sense.)
    """
    planned = sum(r.planned_minutes or 0 for r in records)
    runtime = sum(r.runtime_minutes or 0 for r in records)
    total = sum(r.total_count or 0 for r in records)
    good = sum(r.good_count or 0 for r in records)
    ideal_s = sum((r.ideal_cycle_time_seconds or 0) * (r.total_count or 0) for r in records)
    a = min(runtime / planned, 1.0) if planned else 0.0
    p = min(ideal_s / (runtime * 60), 1.0) if runtime else 0.0
    q = min(good / total, 1.0) if total else 0.0
    return {
        "oee": round(a * p * q * 100),
        "availability": round(a * 100),
        "performance": round(p * 100),
        "quality": round(q * 100),
        "has_data": len(records) > 0,
    }


def build_shift_kpis(shifts):
    rows = []

    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        rows.append({
            "shift_name": shift.shift_name,
            "target_output": shift.target_output,
            "actual_output": shift.actual_output,
            "efficiency": efficiency,
            "gap": shift.target_output - shift.actual_output,
        })

    return rows


def build_oee_trends(records):
    rows = []

    for index, record in enumerate(records):
        oee = calculate_oee_from_record(record)

        rows.append({
            "record": index + 1,
            "machine_id": record.machine_id,
            "machine_name": record.machine.name if record.machine else f"Machine {record.machine_id}",
            "availability": oee["availability"],
            "performance": oee["performance"],
            "quality": oee["quality"],
            "oee": oee["oee"],
            "good_count": record.good_count,
            "rejected_count": record.rejected_count,
            "total_count": record.total_count,
        })

    return rows


def build_management_summary(machines, downtime_logs, shifts, production_records, unit_value_gbp=None):
    reason_minutes = defaultdict(int)
    machine_minutes = defaultdict(int)

    for log in downtime_logs:
        minutes = parse_duration_to_minutes(log.duration)
        reason_minutes[log.reason] += minutes
        machine_minutes[log.machine_id] += minutes

    total_downtime = sum(machine_minutes.values())

    top_loss_reason = max(reason_minutes.items(), key=lambda x: x[1])[0] if reason_minutes else "No data"

    worst_machine_id = None
    worst_machine_downtime = 0

    if machine_minutes:
        worst_machine_id, worst_machine_downtime = max(machine_minutes.items(), key=lambda x: x[1])

    worst_machine = "No data"

    for machine in machines:
        if machine.id == worst_machine_id:
            worst_machine = machine.name
            break

    # Plant OEE is pooled across the window's records (ratio of sums), consistent
    # with the Executive-OEE card and every other surface.
    pooled = pooled_oee(production_records)
    avg_oee = pooled["oee"]
    avg_availability = pooled["availability"]
    avg_performance = pooled["performance"]
    avg_quality = pooled["quality"]

    target_output = sum(shift.target_output for shift in shifts)
    actual_output = sum(shift.actual_output for shift in shifts)
    target_achievement = round((actual_output / target_output) * 100) if target_output else 0

    # Value the downtime as lost OUTPUT: at the observed run-rate (good units per
    # minute of run time), the downtime would have produced this many good units.
    good = sum(r.good_count or 0 for r in production_records)
    runtime = sum(r.runtime_minutes or 0 for r in production_records)
    estimated_loss_units = round(total_downtime * (good / runtime)) if runtime else 0
    if unit_value_gbp:
        # Money = lost units x the tenant's configured £/good-unit.
        estimated_loss_value = round(estimated_loss_units * unit_value_gbp)
    else:
        # No rate configured — fall back to the legacy £8/min downtime proxy.
        estimated_loss_value = total_downtime * 8

    return {
        "avg_oee": avg_oee,
        "avg_availability": avg_availability,
        "avg_performance": avg_performance,
        "avg_quality": avg_quality,
        "total_downtime_minutes": total_downtime,
        "top_loss_reason": top_loss_reason,
        "worst_machine": worst_machine,
        "worst_machine_downtime": worst_machine_downtime,
        "target_output": target_output,
        "actual_output": actual_output,
        "target_achievement": target_achievement,
        "estimated_loss_units": estimated_loss_units,
        "unit_value_gbp": unit_value_gbp,
        "estimated_loss_value": estimated_loss_value,
        "breakdown_count": len([machine for machine in machines if machine.status == "Breakdown"]),
        "machine_count": len(machines),
    }


def build_smart_alerts(machines, production_records, downtime_logs):
    alerts = []
    seen = set()

    def add_alert(alert_type, severity, machine_name, message):
        key = f"{machine_name}:{alert_type}"
        if key in seen:
            return
        seen.add(key)
        alerts.append({
            "type": alert_type,
            "severity": severity,
            "machine": machine_name,
            "message": message,
        })

    for machine in machines:
        if machine.status == "Breakdown":
            add_alert("Breakdown", "Critical", machine.name, f"{machine.name} is currently in breakdown.")
        if machine.utilization < 40:
            add_alert("Low Utilization", "High", machine.name, f"{machine.name} utilization is critically low at {machine.utilization}%.")
        elif machine.utilization < 50:
            add_alert("Low Utilization", "Medium", machine.name, f"{machine.name} utilization is below 50%.")

    latest_by_machine = {}
    for record in sorted(production_records, key=lambda item: item.id, reverse=True):
        if record.machine_id not in latest_by_machine:
            latest_by_machine[record.machine_id] = record

    for record in latest_by_machine.values():
        machine_name = record.machine.name if record.machine else f"Machine {record.machine_id}"
        oee = calculate_oee_from_record(record)

        if oee["oee"] < 50:
            add_alert("OEE Degradation", "Critical", machine_name, f"{machine_name} OEE is critically low at {oee['oee']}%.")
        elif oee["oee"] < 60:
            add_alert("Low OEE", "High", machine_name, f"{machine_name} OEE is below target at {oee['oee']}%.")

        reject_rate = (record.rejected_count / record.total_count) * 100 if record.total_count else 0

        if reject_rate > 8:
            add_alert("Quality Escalation", "High", machine_name, f"{machine_name} reject rate is above 8%.")
        elif reject_rate > 5:
            add_alert("Quality Loss", "Medium", machine_name, f"{machine_name} reject rate is above 5%.")

    downtime_by_machine = defaultdict(int)
    for log in downtime_logs[-50:]:
        downtime_by_machine[log.machine_id] += parse_duration_to_minutes(log.duration)

    for machine_id, minutes in downtime_by_machine.items():
        if minutes > 60:
            machine_name = f"Machine {machine_id}"
            for machine in machines:
                if machine.id == machine_id:
                    machine_name = machine.name
                    break
            add_alert("Downtime Escalation", "Critical", machine_name, f"{machine_name} has accumulated more than 60 minutes of downtime recently.")

    return alerts


def calculate_fallback_oee(utilization: int):
    return round((utilization / 100) * 0.9 * 0.95 * 100)


def generate_alerts(db: Session):
    machines = db.query(models.Machine).all()
    production_records = (
        db.query(models.ProductionRecord)
        .order_by(models.ProductionRecord.id.desc())
        .limit(50)
        .all()
    )

    dynamic_alerts = []
    seen = set()

    def add_alert(alert_type: str, severity: str, machine_name: str, message: str):
        key = f"{machine_name}:{alert_type}"
        if key in seen:
            return
        seen.add(key)
        dynamic_alerts.append(
            {
                "type": alert_type,
                "severity": severity,
                "machine": machine_name,
                "message": message,
            }
        )

    for machine in machines:
        if machine.status == "Breakdown":
            add_alert("Breakdown", "High", machine.name, f"{machine.name} is currently in breakdown")

        if machine.utilization < 50:
            add_alert("Low Utilization", "Medium", machine.name, f"{machine.name} utilization is below 50%")

    latest_by_machine = {}

    for record in production_records:
        if record.machine_id not in latest_by_machine:
            latest_by_machine[record.machine_id] = record

    for record in latest_by_machine.values():
        oee = calculate_oee_from_record(record)
        machine_name = record.machine.name if record.machine else f"Machine {record.machine_id}"

        if oee["oee"] < 60:
            add_alert("Low OEE", "High", machine_name, f"{machine_name} OEE is below target at {oee['oee']}%")

        if record.rejected_count > 0 and record.total_count:
            reject_rate = (record.rejected_count / record.total_count) * 100

            if reject_rate > 5:
                add_alert("Quality Loss", "Medium", machine_name, f"{machine_name} reject rate is above 5%")

    return dynamic_alerts
