from collections import defaultdict

# Was a local digit-concatenation parser that misread hour formats ("1 hr" -> 1
# minute), understating downtime in the risk score. Use the shared correct one.
import work_order_status
from duration import parse_duration_to_minutes


def _int(value):
    """Coalesce a possibly-NULL integer column to 0.

    The scorer reads five integer columns off its input rows —
    ``Machine.utilization``, ``WorkOrder.actual_quantity`` /
    ``WorkOrder.target_quantity`` and ``ProductionRecord.total_count`` /
    ``ProductionRecord.rejected_count``. A row written by raw SQL, a migration,
    or an update that clears a field can legitimately store NULL in any of them
    (the ``default=0`` columns because the ORM default only fills a value the
    *inserter* omitted; the ``nullable=False`` count columns because that
    constraint isn't retro-applied to pre-existing rows). The scorer then did
    ``None < 40`` / ``target - None`` / ``sum += None`` and raised ``TypeError``,
    500-ing the predictive-maintenance endpoint (and the maintenance agent that
    scores off the same rows). Treat a missing count as the column's own default
    of 0 — the same coalesce ``analytics_engine.pooled_oee`` already applies to
    ``total_count`` / ``good_count`` when it pools OEE from these very records.
    """
    return value if value is not None else 0


# Which work orders carry outstanding demand is work_order_status's question,
# answered once. This module used to keep its own answer --
# ACTIVE_WORK_ORDER_STATUSES = ("Running", "Delayed") -- and AMP writes neither
# word: the vocabulary is Planned / In Progress / Completed / On Hold, so the
# whitelist intersected the plant in NOTHING and the pressure factor below could
# only ever score zero. work_order_status names the ENDINGS instead and treats
# everything else as open, so a word nobody thought of defaults to the safe
# direction for a backlog. See test_work_order_pressure.py for the measurement.


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

def calculate_predictive_risk(machines, downtime_logs, production_records,
                              machine_events, work_orders, aggregates=None):
    """Score failure risk per machine.

    `aggregates` is the pre-reduced form of the three history lists, the same
    idea as build_management_summary's `production_sums` / `downtime_agg`. None
    of the three lists is ever read row by row below -- each is folded into
    per-machine counters, and every one of those counters is a GROUP BY. So a
    caller that can do the reduction in SQL should, and `ai.prediction
    .assess_from_db` does: at 200 machines with a month of history those lists
    were ~106,000 ORM objects on a three-second poll (1633 ms for
    /machine-health, of which only 76 ms was SQL).

    The rows entry point is unchanged and still used by callers that already
    hold the lists. test_predictive_risk_aggregates.py asserts the two paths
    score identically, including the awkward rows: a duration that does not
    parse, a blank reason, and "breakdown" in either case.
    """
    work_order_pressure = defaultdict(int)

    if aggregates is not None:
        downtime_by_machine = defaultdict(int, aggregates["downtime_minutes"])
        downtime_events_by_machine = defaultdict(int, aggregates["downtime_events"])
        breakdown_events_by_machine = defaultdict(int, aggregates["breakdown_events"])
        reject_by_machine = defaultdict(int, aggregates["rejects"])
        total_by_machine = defaultdict(int, aggregates["totals"])
        downtime_logs = production_records = machine_events = ()
    else:
        downtime_by_machine = defaultdict(int)
        downtime_events_by_machine = defaultdict(int)
        breakdown_events_by_machine = defaultdict(int)
        reject_by_machine = defaultdict(int)
        total_by_machine = defaultdict(int)

    for log in downtime_logs:
        downtime_by_machine[log.machine_id] += parse_duration_to_minutes(log.duration)
        downtime_events_by_machine[log.machine_id] += 1
        if str(log.reason).lower() == "breakdown":
            breakdown_events_by_machine[log.machine_id] += 1

    for record in production_records:
        # Coalesce NULL counts to 0 (see _int): a legacy / raw-SQL / migration row
        # can hold a NULL total_count or rejected_count, and `defaultdict(int) += None`
        # raised TypeError, 500-ing the endpoint. A missing count means no measured
        # production, so it contributes 0 (and total 0 leaves reject_rate at 0 via the
        # divide guard below) — matching how pooled_oee reads these same columns.
        reject_by_machine[record.machine_id] += _int(record.rejected_count)
        total_by_machine[record.machine_id] += _int(record.total_count)

    for event in machine_events:
        if event.new_status == "Breakdown":
            breakdown_events_by_machine[event.machine_id] += 1

    for work_order in work_orders:
        # A defensive re-filter (the DB loader already bounds this in SQL with
        # work_order_status.open_clause()): if a caller passes an unfiltered list,
        # only OPEN work orders count toward pressure. Same rule, same module, so
        # the SQL and the Python cannot drift into two answers -- pinned row for
        # row in test_work_order_pressure.py section 4.
        if not work_order_status.is_closed(work_order.status):
            # Guard BOTH operands: actual_quantity was already coalesced, but a NULL
            # target_quantity (same legacy/raw-SQL rows) made `None - int` raise
            # TypeError on the very same line. No known target = no known outstanding
            # demand, so max(0 - actual, 0) = 0 pressure — honest, not fabricated.
            work_order_pressure[work_order.machine_id] += max(_int(work_order.target_quantity) - _int(work_order.actual_quantity), 0)

    # WHICH MACHINES HAVE ANYTHING RECORDED. Taken BEFORE the per-machine reads
    # below: those counters are defaultdicts, and reading `[machine.id]` writes
    # a 0 that would then look like a recorded zero. A machine with no downtime
    # row, no production record and no breakdown transition in the window is
    # scored over nothing: every history rule reads 0, takes no points off, and
    # 100 comes out — an absence, not a clean bill. The row says which it is
    # (`has_recorded_input`), so the explanation, the twin and the fleet
    # average can tell the two apart. Status and utilisation are the machine
    # row itself and open-order load is now, not history, so none of those
    # count as recorded history.
    recorded_by_source = (
        ("downtime", set(downtime_by_machine) | set(downtime_events_by_machine)),
        ("production", set(total_by_machine) | set(reject_by_machine)),
        ("breakdowns", set(breakdown_events_by_machine)),
    )

    rows = []
    for machine in machines:
        score = 0
        reasons = []
        recorded_inputs = [name for name, ids in recorded_by_source if machine.id in ids]
        # EVERY CHECK THIS SCORE MADE, fired or not (ADR-0027). A machine health
        # score a plant cannot take apart is a number to be believed or ignored;
        # this records, for each rule, its points, the measured value it read and
        # the threshold it compared against. The score itself is untouched: the
        # if-chain below is the same chain in the same order, and
        # test_machine_health_explained.py asserts the fired points still sum to
        # the score and the reasons are still the fired rules' own words.
        components = []

        def check(key, label, points, fired, measured, unit, threshold, reason=None):
            components.append({"key": key, "label": label, "points": points if fired else 0,
                               "max_points": points, "fired": bool(fired), "measured": measured,
                               "unit": unit, "threshold": threshold, "reason": reason})
            return bool(fired)

        downtime_minutes = downtime_by_machine[machine.id]
        downtime_events = downtime_events_by_machine[machine.id]
        breakdown_events = breakdown_events_by_machine[machine.id]
        pressure = work_order_pressure[machine.id]

        utilization = _int(machine.utilization)

        reject_rate = 0
        if total_by_machine[machine.id]:
            reject_rate = round((reject_by_machine[machine.id] / total_by_machine[machine.id]) * 100, 1)

        if check("breakdown_now", "Currently in breakdown", 35, machine.status == "Breakdown",
                 machine.status, "", "status is Breakdown", "machine currently in breakdown"):
            score += 35
            reasons.append("machine currently in breakdown")
        if check("maintenance_now", "Currently in maintenance", 15, machine.status == "Maintenance",
                 machine.status, "", "status is Maintenance", "machine currently in maintenance"):
            score += 15
            reasons.append("machine currently in maintenance")
        if check("low_utilization", "Low utilisation", 20, utilization < 40, utilization, "%",
                 "utilisation below 40%", "low utilization below 40%"):
            score += 20
            reasons.append("low utilization below 40%")
        if check("high_utilization", "High utilisation", 12, utilization > 90, utilization, "%",
                 "utilisation above 90%", "high utilization above 90%"):
            score += 12
            reasons.append("high utilization above 90%")
        if check("downtime_high", "High accumulated downtime", 25, downtime_minutes >= 120, downtime_minutes,
                 "min", "120 minutes or more in the risk window", "high accumulated downtime"):
            score += 25
            reasons.append("high accumulated downtime")
        elif check("downtime_moderate", "Moderate accumulated downtime", 15, downtime_minutes >= 60,
                   downtime_minutes, "min", "60 minutes or more in the risk window",
                   "moderate accumulated downtime"):
            score += 15
            reasons.append("moderate accumulated downtime")
        if check("downtime_frequent", "Frequent stoppages", 15, downtime_events >= 5, downtime_events,
                 "events", "5 or more stoppages in the risk window", "frequent downtime events"):
            score += 15
            reasons.append("frequent downtime events")
        if check("breakdown_repeat", "Repeated breakdown transitions", 20, breakdown_events >= 3,
                 breakdown_events, "events", "3 or more transitions into Breakdown",
                 "repeated breakdown transitions"):
            score += 20
            reasons.append("repeated breakdown transitions")
        if check("reject_high", "High reject rate", 20, reject_rate >= 8, reject_rate, "%",
                 "8% or more of units rejected", "high reject rate"):
            score += 20
            reasons.append("high reject rate")
        elif check("reject_moderate", "Moderate reject rate", 10, reject_rate >= 5, reject_rate, "%",
                   "5% or more of units rejected", "moderate reject rate"):
            score += 10
            reasons.append("moderate reject rate")
        if check("work_order_load", "High open work-order load", 10, pressure >= 500, pressure, "units",
                 "500 or more outstanding units on open orders", "high active work-order load"):
            score += 10
            reasons.append("high active work-order load")

        raw_score = score
        score = min(score, 100)
        if not reasons:
            reasons.append("no major risk indicators detected")

        rows.append({
            "machine_id": machine.id,
            "machine_name": machine.name,
            "status": machine.status,
            "utilization": utilization,
            "risk_score": score,
            "risk_level": classify_risk(score),
            "downtime_minutes": downtime_minutes,
            "downtime_events": downtime_events,
            "breakdown_events": breakdown_events,
            "reject_rate": reject_rate,
            "work_order_pressure": pressure,
            "reasons": reasons,
            # What the history rules had to read: the sources with at least one
            # row for this machine in the risk window. Empty means the six
            # history rules read nothing, and a 100 here is an absence.
            "recorded_inputs": recorded_inputs,
            "has_recorded_input": bool(recorded_inputs),
            # The explanation travels WITH the score, so no surface can show one
            # without the other. `capped` says when the points were cut to 100.
            "components": components,
            "points_before_cap": raw_score,
            "capped": raw_score > 100,
            "recommendation": recommendation(score),
        })

    return sorted(rows, key=lambda item: item["risk_score"], reverse=True)
