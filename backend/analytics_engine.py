from collections import defaultdict

from sqlalchemy.orm import Session

import models
# Re-exported so existing `from analytics_engine import parse_duration_to_minutes`
# call sites (recommendations_routes) keep working; single source of truth.
from duration import parse_duration_to_minutes


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


def build_management_summary(machines, downtime_logs, shifts, production_records):
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

    avg_oee = 0
    avg_availability = 0
    avg_performance = 0
    avg_quality = 0

    if production_records:
        oee_rows = [calculate_oee_from_record(record) for record in production_records]
        avg_oee = round(sum(row["oee"] for row in oee_rows) / len(oee_rows))
        avg_availability = round(sum(row["availability"] for row in oee_rows) / len(oee_rows))
        avg_performance = round(sum(row["performance"] for row in oee_rows) / len(oee_rows))
        avg_quality = round(sum(row["quality"] for row in oee_rows) / len(oee_rows))

    target_output = sum(shift.target_output for shift in shifts)
    actual_output = sum(shift.actual_output for shift in shifts)
    target_achievement = round((actual_output / target_output) * 100) if target_output else 0

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
        "estimated_loss_value": total_downtime * 8,
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
