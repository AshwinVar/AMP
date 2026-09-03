from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
# Re-exported so existing `from analytics_engine import parse_duration_to_minutes`
# call sites (recommendations_routes) keep working; single source of truth.
from duration import parse_duration_to_minutes

# World-class OEE benchmarks — the single source of truth (ADR-0010). The plant
# target and the three component targets: 0.90 x 0.95 x ~0.99 ~= 0.85 OEE.
WORLD_CLASS_OEE = 85
WORLD_CLASS_COMPONENTS = {"availability": 90, "performance": 95, "quality": 99}


def downtime_aggregates(db: Session):
    """Downtime totals WITHOUT hydrating the downtime log into Python.

    THE PROBLEM THIS SOLVES
    `/analytics/executive-oee` and `/analytics/factory-command-center` are both
    on the dashboard's 3-second poll and both did an unfiltered, unlimited
    `db.query(models.DowntimeLog).all()`. Measured on PostgreSQL 18.3 with 200
    machines, as pure handler time (no HTTP, no queueing):

            downtime rows      executive-oee      factory-command-center
                     ~200             6.9 ms                    16.8 ms
                   75,000           822.0 ms                   859.6 ms

    `downtime_logs` does not grow with the size of the factory; it grows with
    how long the factory has been running. 75,000 rows is one year of a
    200-machine plant at one event per machine per day, so this is not a
    hypothetical scale. Every performance harness in this repo missed it because
    all of them seed downtime rows PER MACHINE (see
    test_downtime_scan_bounded.py, which deliberately does not).

    WHY GROUP BY, WHEN AN EARLIER PASS SAID IT COULDN'T BE DONE
    The comment at analytics_routes.py:950 said this scan "genuinely can't move
    into SQL", and its reason was right: `DowntimeLog.duration` is a STRING
    ("15 min"), so no portable SQL can SUM it. But SQL can still GROUP BY it.
    Python then parses each DISTINCT duration once and multiplies by that
    group's COUNT. Since parsing is a pure function of the string, the result is
    arithmetically IDENTICAL to summing row by row -- not an approximation and
    not a sample, so no displayed figure changes. Measured: 75,000 rows collapse
    to 2,200 groups, and 807.6 ms becomes 15.2 ms.

    A `.limit()` was not an option: it would silently drop downtime and corrupt
    availability. A date window was not either: these endpoints report LIFETIME
    totals and their production figures are lifetime too, so narrowing one side
    would make the pair inconsistent.

    Returns five aggregates because the three call sites need different ones --
    `analytics_summary` tallies reasons by EVENT COUNT while `executive-oee`
    tallies them by MINUTES, and conflating the two would change both payloads.
    """
    rows = (
        db.query(
            models.DowntimeLog.machine_id,
            models.DowntimeLog.reason,
            models.DowntimeLog.duration,
            func.count(),
        )
        .group_by(
            models.DowntimeLog.machine_id,
            models.DowntimeLog.reason,
            models.DowntimeLog.duration,
        )
        .all()
    )
    minutes_by_machine, minutes_by_reason, events_by_reason = {}, {}, {}
    total_minutes = 0
    total_events = 0
    for machine_id, reason, duration, count in rows:
        # parse ONCE per distinct string, then multiply -- exactly equal to
        # parsing each of the `count` identical rows separately.
        minutes = parse_duration_to_minutes(duration) * count
        label = normalize_downtime_reason(reason)
        total_minutes += minutes
        total_events += count
        minutes_by_machine[machine_id] = minutes_by_machine.get(machine_id, 0) + minutes
        minutes_by_reason[label] = minutes_by_reason.get(label, 0) + minutes
        events_by_reason[label] = events_by_reason.get(label, 0) + count
    return {
        "minutes_by_machine": minutes_by_machine,
        "minutes_by_reason": minutes_by_reason,
        "events_by_reason": events_by_reason,
        "total_minutes": total_minutes,
        "total_events": total_events,
    }


def normalize_downtime_reason(reason) -> str:
    """Coalesce a downtime log's ``reason`` to a stable, non-empty label.

    ``DowntimeLog.reason`` is ``Column(String, nullable=False)`` WITHOUT a
    default, and (as everywhere in this codebase) that constraint is not
    retro-applied to a row written by raw SQL / a migration / a legacy path — so
    such a row can hold a genuine NULL, and an edge/import path can write an
    empty string. Used RAW as a grouping key and as a displayed "top loss
    reason" / Pareto label, a NULL surfaced in the payload as a literal
    ``null`` / ``None`` (top_loss_reason, the downtime Pareto, the reason
    tallies) — a stored non-value leaking straight into a displayed label
    (ADR-0010: a default/guard must never leak into a displayed value).

    Coalesce to "Unknown" — the SAME label the canonical downtime read-model
    already uses (``ai.downtime._norm_reason``: ``(d.reason or "Unknown").strip()
    or "Unknown"``) — so the older engine path and the read-model name an
    unlabelled stop identically (rule-1: one convention, not two). A whitespace-
    only reason folds to "Unknown" as well, and a real reason is returned
    unchanged.
    """
    return (reason or "Unknown").strip() or "Unknown"


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


# A week-over-week OEE move smaller than this (points) reads as "flat" — one
# dead-band so a small move can't be "flat" on one exec-home surface and "down"
# on another.
OEE_TREND_DEAD_BAND = 2


def oee_direction(current, prior, dead_band=OEE_TREND_DEAD_BAND):
    """Week-over-week OEE direction: 'up' / 'down' / 'flat', or None with no prior
    week. The single definition of "which way is OEE going week on week", shared by
    the recovery card's trend badge and the scorecard's OEE delta, so the same
    weekly pair can't be labelled 'flat' on one and 'down' (red) on another."""
    if prior is None:
        return None
    d = current - prior
    return "up" if d >= dead_band else "down" if d <= -dead_band else "flat"


def calculate_oee_from_record(record):
    # Coalesce every NULL count/minute column to 0 BEFORE any arithmetic. These
    # five columns are nullable=False, but that constraint is not retro-applied to
    # rows written by raw SQL / a migration / a legacy insert — pooled_oee already
    # reads them as `... or 0` for exactly this reason. This per-record view did
    # NOT, so a single such row raised TypeError (`None / int`, `int * None`) and
    # 500-ed every surface it backs: /oee/summary, /oee/trends, /reports/oee.csv,
    # and the smart-alert / generate-alert paths (which call this first). That is
    # the same "a NULL count 500'd the whole endpoint" class already fixed for the
    # predictive scorer (#428) and the roster/list endpoints. A missing count means
    # no measured production, so it contributes 0 — which also reconciles this
    # drill-down with the pooled headline (rule-3): pooled_oee returns all-zero
    # components on the identical row, and now so does this.
    planned_minutes = record.planned_minutes or 0
    runtime_minutes = record.runtime_minutes or 0
    total_count = record.total_count or 0
    good_count = record.good_count or 0
    ideal_cycle_time_seconds = record.ideal_cycle_time_seconds or 0

    availability = runtime_minutes / planned_minutes if planned_minutes else 0
    runtime_seconds = runtime_minutes * 60

    performance = (
        (ideal_cycle_time_seconds * total_count) / runtime_seconds
        if runtime_seconds else 0
    )

    quality = good_count / total_count if total_count else 0

    # Clamp EVERY component to [0, 1], matching pooled_oee (which caps a/p/q the
    # same way). Only performance was capped here, so the per-record view could
    # print availability > 100% (a machine that ran past its planned minutes,
    # runtime > planned) or quality > 100% (good_count > total_count from a
    # data-entry slip), and its OEE could top 100% — disagreeing with the pooled
    # headline and violating the honesty rule (a metric can't exceed the bound
    # the data supports). Reconciles the per-record view with the pooled one.
    #
    # The clamp is SYMMETRIC — floor at 0 as well as cap at 1. The ingest paths
    # reject negative counts/minutes (machines_routes.create_production_record,
    # mqtt_service._non_negative_int), but a legacy / raw-SQL / migration row can
    # still hold a negative good_count or runtime, and good/total (or runtime/
    # planned) then goes NEGATIVE — printing e.g. quality -20% and dragging OEE
    # below zero. A percentage the data can't support in the other direction is
    # the same honesty violation as one over 100%; 0 is the floor a physical
    # quantity supports. max(0, min(x, 1)) is a strict no-op on every valid record.
    availability = max(0, min(availability, 1))
    performance = max(0, min(performance, 1))
    quality = max(0, min(quality, 1))

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
    return pooled_oee_from_sums(
        planned=sum(r.planned_minutes or 0 for r in records),
        runtime=sum(r.runtime_minutes or 0 for r in records),
        total=sum(r.total_count or 0 for r in records),
        good=sum(r.good_count or 0 for r in records),
        ideal_seconds=sum((r.ideal_cycle_time_seconds or 0) * (r.total_count or 0) for r in records),
        has_data=len(records) > 0,
    )


def pooled_oee_from_sums(planned, runtime, total, good, ideal_seconds, has_data) -> dict:
    """The same pooling, for callers that already have the sums.

    A caller that aggregates in SQL (GROUP BY rather than hydrating every row
    into Python) has the five totals and no record list. Rather than let it
    re-derive the formula — which is how two surfaces end up disagreeing — it
    calls this, and ``pooled_oee`` above delegates here too. One definition.
    """
    # Symmetric clamp to [0, 1]: cap at 1.0 (a component can't exceed 100%) AND
    # floor at 0.0. A negative count/minute on a legacy or raw-SQL row (the ingest
    # guards can't retro-fix history) makes good/total or runtime/planned negative,
    # which pulled pooled OEE below zero — the same honesty violation as > 100%,
    # and it disagreed with calculate_oee_from_record above (which now floors too).
    # max(0.0, ...) is a strict no-op on every well-formed sum.
    a = max(0.0, min(runtime / planned, 1.0)) if planned else 0.0
    p = max(0.0, min(ideal_seconds / (runtime * 60), 1.0)) if runtime else 0.0
    q = max(0.0, min(good / total, 1.0)) if total else 0.0
    return {
        "oee": round(a * p * q * 100),
        "availability": round(a * 100),
        "performance": round(p * 100),
        "quality": round(q * 100),
        "has_data": has_data,
    }


def build_shift_kpis(shifts):
    rows = []

    for shift in shifts:
        # Coalesce NULL target/actual to 0 BEFORE any arithmetic. Both columns are
        # Column(Integer, nullable=False) WITHOUT a default, but that constraint is
        # not retro-applied to rows written by raw SQL / a migration / a legacy
        # insert — the same reason the count columns are read as `... or 0`
        # everywhere (calculate_oee_from_record, the predictive scorer #428). Left
        # as None, `None / int`, `int - None` and (in the callers below) `sum(...)`
        # raised TypeError and 500-ed every hydrated-row shift surface
        # (/analytics/shift-kpis, /analytics/executive-oee, the intelligence-summary
        # export) — while the SQL rollups (/analytics/summary, /analytics/management)
        # returned a number, because their COALESCE(SUM(..),0) already drops a NULL
        # row (SQL SUM skips NULLs, contributing 0). Coalescing per-row to 0 here
        # RECONCILES the two: a NULL row contributes 0 to both paths (rule-3).
        target = shift.target_output or 0
        actual = shift.actual_output or 0
        efficiency = round((actual / target) * 100) if target else 0
        rows.append({
            "shift_name": shift.shift_name,
            "target_output": target,
            "actual_output": actual,
            "efficiency": efficiency,
            "gap": target - actual,
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


def build_management_summary(machines, downtime_logs, shifts, production_records,
                             unit_value_gbp=None, production_sums=None, shift_sums=None,
                             downtime_agg=None):
    # `downtime_agg` is the same idea as `production_sums` and `shift_sums` above:
    # let the caller do the tally in SQL and hand in the result, instead of
    # hydrating a growing table just to add it up in Python. It is the entry
    # point /analytics/management uses so it stops scanning downtime_logs, which
    # grows with how long the factory has run rather than with its size — the
    # defect #531 fixed on the two polled endpoints and could not fix here,
    # because this caller passes the row LIST rather than consuming it locally.
    #
    # An EQUIVALENCE, not an approximation: downtime_aggregates groups by
    # (machine, reason, duration) and parses each distinct duration once, so the
    # totals are identical to the loop below. test_management_dashboard_sql.py
    # asserts the two entry points produce the same dict.
    if downtime_agg is not None:
        reason_minutes = defaultdict(int, downtime_agg["minutes_by_reason"])
        machine_minutes = defaultdict(int, downtime_agg["minutes_by_machine"])
        total_downtime = downtime_agg["total_minutes"]
    else:
        reason_minutes = defaultdict(int)
        machine_minutes = defaultdict(int)

        for log in downtime_logs:
            minutes = parse_duration_to_minutes(log.duration)
            # Coalesce a NULL/empty reason to "Unknown" (see
            # normalize_downtime_reason) so top_loss_reason is never a literal
            # `null` label — the same convention the canonical downtime
            # read-model applies.
            reason_minutes[normalize_downtime_reason(log.reason)] += minutes
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

    # Plant OEE is pooled across the records (ratio of sums), consistent with the
    # Executive-OEE card and every other surface. ``good``/``runtime`` (the loss
    # valuation below) are the same sums pooled_oee already needs, so they come out
    # of the same place.
    #
    # A caller that aggregated the (growing) production_records table in SQL — rather
    # than hydrating every row into Python just to sum it (rule-4) — passes the five
    # pooled sums plus the record COUNT as ``production_sums``; the pooled result and
    # the loss units are then byte-for-byte identical to iterating the full list
    # (pooled_oee delegates to the very same pooled_oee_from_sums), which is exactly
    # what the /analytics/summary rollup already does (#411/#419). The list path is
    # kept for callers that already hold the rows (the text-report export). Downtime
    # stays a row scan either way — its durations are free text, and only Python's
    # parse_duration_to_minutes can total them.
    if production_sums is not None:
        planned_s, runtime_s, total_s, good_s, ideal_s, record_count = production_sums
        pooled = pooled_oee_from_sums(
            planned=planned_s, runtime=runtime_s, total=total_s,
            good=good_s, ideal_seconds=ideal_s, has_data=record_count > 0,
        )
        good = good_s
        runtime = runtime_s
    else:
        pooled = pooled_oee(production_records)
        good = sum(r.good_count or 0 for r in production_records)
        runtime = sum(r.runtime_minutes or 0 for r in production_records)
    avg_oee = pooled["oee"]
    avg_availability = pooled["availability"]
    avg_performance = pooled["performance"]
    avg_quality = pooled["quality"]

    # Shift attainment is pooled (total actual / total target). Same rule-4 option as
    # above: a caller that summed shift_data in SQL passes ``shift_sums``; the list
    # path sums the rows it was handed. target_output/actual_output are nullable=False,
    # so the SQL COALESCE(SUM(..),0) and the Python sum agree byte-for-byte.
    if shift_sums is not None:
        target_output, actual_output = shift_sums
    else:
        # Coalesce per-row NULLs to 0 (see build_shift_kpis): a NULL target/actual on
        # a raw-SQL/legacy row made `sum(...)` raise TypeError here, 500-ing the
        # intelligence-summary export. SQL's COALESCE(SUM(..),0) path already skips a
        # NULL row (contributing 0), so `or 0` per row keeps the list path byte-for-byte
        # identical to the SQL rollup (rule-3).
        target_output = sum((shift.target_output or 0) for shift in shifts)
        actual_output = sum((shift.actual_output or 0) for shift in shifts)
    target_achievement = round((actual_output / target_output) * 100) if target_output else 0

    # Value the downtime as lost OUTPUT: at the observed run-rate (good units per
    # minute of run time), the downtime would have produced this many good units.
    estimated_loss_units = round(total_downtime * (good / runtime)) if runtime else 0
    if unit_value_gbp is not None:
        # Money = lost units x the tenant's configured £/good-unit. A configured
        # rate of 0 is a real £0 margin, so it yields £0 — NOT the legacy proxy
        # below (`is not None`, not truthiness): fabricating a downtime-loss £ for
        # a tenant whose rate is explicitly zero is the exact thing ADR-0010 bans.
        estimated_loss_value = round(estimated_loss_units * unit_value_gbp)
    else:
        # No rate configured (None) — fall back to the legacy £8/min downtime proxy.
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
        # utilization is Column(Integer, default=0) WITHOUT nullable=False — a row
        # written by raw SQL / a migration / an update that clears the field can be
        # NULL, and `None < 40` raised TypeError, 500-ing every caller (/alerts/smart,
        # the intelligence-summary export). A NULL means "no utilization recorded",
        # which is NOT the same as a measured 0%: coercing it to 0 would fabricate a
        # "critically low at 0%" alert from the column default (ADR-0010 — a default
        # must never leak into a displayed value). So skip the utilization alert when
        # there's no reading; the status-based alerts above still fire.
        if machine.utilization is not None and machine.utilization < 40:
            add_alert("Low Utilization", "High", machine.name, f"{machine.name} utilization is critically low at {machine.utilization}%.")
        elif machine.utilization is not None and machine.utilization < 50:
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

        # Coalesce a NULL rejected_count to 0 (same legacy/raw-SQL rows as above):
        # total_count already guards the divide, but `None / int` still raised
        # TypeError when only rejected_count was NULL, 500-ing /alerts/smart and the
        # intelligence-summary export. No recorded rejects means a 0 reject rate.
        reject_rate = ((record.rejected_count or 0) / record.total_count) * 100 if record.total_count else 0

        if reject_rate > 8:
            add_alert("Quality Escalation", "High", machine_name, f"{machine_name} reject rate is above 8%.")
        elif reject_rate > 5:
            add_alert("Quality Loss", "Medium", machine_name, f"{machine_name} reject rate is above 5%.")

    # "Recently" = the 50 most-recent logs by id, selected HERE rather than
    # trusting the caller's ordering. /alerts/smart passes downtime newest-first
    # (id desc, limit 100) and the report export oldest-first (.all()), so a
    # positional [-50:] slice read OPPOSITE windows: on the newest-first caller it
    # summed the OLDEST 50 and missed the very downtime this alert exists to flag.
    # Sorting by id here makes "recent" mean recent for both callers.
    recent_downtime = sorted(
        downtime_logs, key=lambda log: getattr(log, "id", 0) or 0, reverse=True
    )[:50]
    downtime_by_machine = defaultdict(int)
    for log in recent_downtime:
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
    # Floor at 0 for the same reason the pooled/per-record clamps do: the summary
    # fallback (analytics_routes) passes a machine's raw stored utilization, and a
    # legacy / raw-SQL negative reading would otherwise estimate a negative OEE.
    return max(0, round((utilization / 100) * 0.9 * 0.95 * 100))


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

        # NULL utilization = no reading (see build_smart_alerts): skip rather than
        # crash on `None < 50` or fabricate a 0% alert from the column default.
        if machine.utilization is not None and machine.utilization < 50:
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

        # `record.rejected_count > 0` raised TypeError on a NULL rejected_count
        # (a legacy/raw-SQL row) before either operand was even divided; coalesce
        # it so a missing reject count reads as 0 (no reject alert) rather than
        # 500-ing /alerts (generate_alerts backs the dynamic alert feed).
        if (record.rejected_count or 0) > 0 and record.total_count:
            reject_rate = (record.rejected_count / record.total_count) * 100

            if reject_rate > 5:
                add_alert("Quality Loss", "Medium", machine_name, f"{machine_name} reject rate is above 5%")

    return dynamic_alerts
