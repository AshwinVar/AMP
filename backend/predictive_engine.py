from collections import defaultdict

# Was a local digit-concatenation parser that misread hour formats ("1 hr" -> 1
# minute), understating downtime in the risk score. Use the shared correct one.
from duration import parse_duration_to_minutes


def classify_risk(score: int):
    if score >= 75:
        return "Critical"
    if score >= 55:
        return "High"
    if score >= 35:
        return "Medium"
    return "Low"

def recommendation(score: int):
    if score >= 75:
        return "Immediate maintenance inspection recommended before next production run."
    if score >= 55:
        return "Schedule preventive maintenance and monitor closely."
    if score >= 35:
        return "Monitor condition and review recent downtime history."
    return "Machine condition appears stable."

def calculate_predictive_risk(machines, downtime_logs, production_records, machine_events, work_orders):
    downtime_by_machine = defaultdict(int)
    downtime_events_by_machine = defaultdict(int)
    breakdown_events_by_machine = defaultdict(int)
    reject_by_machine = defaultdict(int)
    total_by_machine = defaultdict(int)
    work_order_pressure = defaultdict(int)

    for log in downtime_logs:
        downtime_by_machine[log.machine_id] += parse_duration_to_minutes(log.duration)
        downtime_events_by_machine[log.machine_id] += 1
        if str(log.reason).lower() == "breakdown":
            breakdown_events_by_machine[log.machine_id] += 1

    for record in production_records:
        reject_by_machine[record.machine_id] += record.rejected_count
        total_by_machine[record.machine_id] += record.total_count

    for event in machine_events:
        if event.new_status == "Breakdown":
            breakdown_events_by_machine[event.machine_id] += 1

    for work_order in work_orders:
        if work_order.status in ["Running", "Delayed"]:
            work_order_pressure[work_order.machine_id] += max(work_order.target_quantity - work_order.actual_quantity, 0)

    rows = []
    for machine in machines:
        score = 0
        reasons = []
        downtime_minutes = downtime_by_machine[machine.id]
        downtime_events = downtime_events_by_machine[machine.id]
        breakdown_events = breakdown_events_by_machine[machine.id]
        pressure = work_order_pressure[machine.id]

        reject_rate = 0
        if total_by_machine[machine.id]:
            reject_rate = round((reject_by_machine[machine.id] / total_by_machine[machine.id]) * 100, 1)

        if machine.status == "Breakdown":
            score += 35
            reasons.append("machine currently in breakdown")
        if machine.status == "Maintenance":
            score += 15
            reasons.append("machine currently in maintenance")
        if machine.utilization < 40:
            score += 20
            reasons.append("low utilization below 40%")
        if machine.utilization > 90:
            score += 12
            reasons.append("high utilization above 90%")
        if downtime_minutes >= 120:
            score += 25
            reasons.append("high accumulated downtime")
        elif downtime_minutes >= 60:
            score += 15
            reasons.append("moderate accumulated downtime")
        if downtime_events >= 5:
            score += 15
            reasons.append("frequent downtime events")
        if breakdown_events >= 3:
            score += 20
            reasons.append("repeated breakdown transitions")
        if reject_rate >= 8:
            score += 20
            reasons.append("high reject rate")
        elif reject_rate >= 5:
            score += 10
            reasons.append("moderate reject rate")
        if pressure >= 500:
            score += 10
            reasons.append("high active work-order load")

        score = min(score, 100)
        if not reasons:
            reasons.append("no major risk indicators detected")

        rows.append({
            "machine_id": machine.id,
            "machine_name": machine.name,
            "status": machine.status,
            "utilization": machine.utilization,
            "risk_score": score,
            "risk_level": classify_risk(score),
            "downtime_minutes": downtime_minutes,
            "downtime_events": downtime_events,
            "breakdown_events": breakdown_events,
            "reject_rate": reject_rate,
            "work_order_pressure": pressure,
            "reasons": reasons,
            "recommendation": recommendation(score),
        })

    return sorted(rows, key=lambda item: item["risk_score"], reverse=True)
