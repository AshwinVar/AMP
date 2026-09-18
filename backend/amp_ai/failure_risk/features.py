"""The ONE feature extraction for the failure-risk model (standard library + ``duration``).

Training (synthetic histories) and serving (database histories) both call
``extract(history, as_of)``, and ``extract`` first reduces the history to what a
prediction at ``as_of`` may see with ``history.truncate``. There is no second
implementation anywhere: the build, the tests and ``predict`` all come here.

WINDOWS
-------
Every window is half-open, ``[as_of - d, as_of)`` from ``window_bounds`` (the
same bounds as ``oee_contract.OeeWindow(days=d, now=as_of)``; pinned by
test_amp_ai_failure_risk_features.py). A record stamped exactly ``as_of`` is
the future; one stamped exactly ``as_of - d`` is inside the d-day window.

MISSING IS NONE
---------------
A rate with nothing to divide by is ``None``, never 0: no planned minutes means
availability was not measured, not that it was 0%. The model fills ``None``
with the TRAINING median stored in its artifact; nothing is ever estimated
from the data being scored.

FEATURES (all floats)
---------------------
breakdowns_7d, breakdowns_30d      MachineEvents with new_status "Breakdown"
days_since_breakdown               days since the last one in the lookback,
                                   ``LOOKBACK_DAYS`` if none
never_broken_in_lookback           1.0 if none in the lookback
downtime_min_7d, downtime_min_30d  DowntimeLog minutes (duration.parse_duration_to_minutes)
stoppages_7d                       DowntimeLog rows, any reason
stoppage_accel                     stoppages_7d / (stoppages_30d * 7/30 + 1)
availability_7d                    runtime / planned              (None if planned = 0)
performance_7d                     (sum total*ideal_cycle_s / 60) / runtime   (None if runtime = 0)
performance_drift                  performance_7d - performance_30d (None if either is)
reject_rate_7d                     rejected / total                (None if total = 0)
reject_rate_drift                  reject_rate_7d - reject_rate_30d (None if either is)
inspection_fail_rate_14d           failed / inspected              (None if nothing inspected)
runtime_h_since_maint              production runtime hours stamped at or after the last
                                   completed NON-reactive task in the lookback (read as
                                   00:00 of its completion date); over the whole lookback
                                   if none; capped at RUNTIME_H_CAP
no_maint_in_lookback               1.0 if there is no such task
days_since_maint                   days since that completion, ``LOOKBACK_DAYS`` if none
overdue_maint_open                 tasks open at as_of with planned_date < as_of.date()
                                   (the date comparison ai.maintenance.is_overdue makes)

Reactive (corrective) work is not maintenance here: it follows a failure, and
counting it would make "recently maintained" mean "recently broken".

NOT A FEATURE: utilisation
--------------------------
``MachineEvent.utilization`` (Integer, default 0) was checked against every
writer. 0 is used as a placeholder, not only as a reading: mqtt_service creates
an unknown machine with utilization=0 and clamps a payload with no
utilisation field to 0 (``payload.get("utilization", 0)``); the CSV import
writes 0 for an unparseable value; the IoT and industrial routes copy the
machine's column, which is that default until a reading arrives. A mean of
those values would mix "idle" with "unknown", so ``util_mean_7d`` is dropped.
The current ``Machine.status`` / ``Machine.utilization`` are not features either.
"""
import bisect
from datetime import timedelta

from duration import parse_duration_to_minutes

from .history import BREAKDOWN, LOOKBACK_DAYS, date_instant, truncate, window_bounds

__all__ = ["FEATURE_NAMES", "RUNTIME_H_CAP", "extract", "window_bounds"]

FEATURE_NAMES = (
    "breakdowns_7d", "breakdowns_30d", "days_since_breakdown", "never_broken_in_lookback",
    "downtime_min_7d", "downtime_min_30d", "stoppages_7d", "stoppage_accel",
    "availability_7d", "performance_7d", "performance_drift", "reject_rate_7d", "reject_rate_drift",
    "inspection_fail_rate_14d", "runtime_h_since_maint", "no_maint_in_lookback", "days_since_maint",
    "overdue_maint_open",
)
RUNTIME_H_CAP = 2000.0
_DAY_SECONDS = 86400.0


def _ts(record):
    return record[0]


def _since(records, start):
    """Records (already < as_of) stamped at or after ``start``."""
    return records[bisect.bisect_left(records, start, key=_ts):]


def _ratio(numerator, denominator):
    return numerator / denominator if denominator > 0 else None


def _difference(a, b):
    return None if a is None or b is None else a - b


def _production_rates(rows):
    planned = runtime = rejected = total = 0
    ideal_seconds = 0
    for _, planned_min, runtime_min, ideal_cycle_s, count, _good, rejects in rows:
        planned += planned_min
        runtime += runtime_min
        ideal_seconds += count * ideal_cycle_s
        total += count
        rejected += rejects
    return (_ratio(float(runtime), planned),
            _ratio(ideal_seconds / 60.0, runtime),
            _ratio(float(rejected), total))


def extract(history, as_of) -> dict:
    """{feature name: float | None} for a prediction at ``as_of``. See the module docstring."""
    view = history if history.window_end == as_of else truncate(history, as_of)
    start_7, _ = window_bounds(as_of, 7)
    start_14, _ = window_bounds(as_of, 14)
    start_30, _ = window_bounds(as_of, 30)

    events_30 = _since(view.events, start_30)
    breakdowns_30 = [e for e in events_30 if e[2] == BREAKDOWN]
    breakdowns_7 = [e for e in breakdowns_30 if e[0] >= start_7]
    last_breakdown = None
    for e in reversed(view.events):
        if e[2] == BREAKDOWN:
            last_breakdown = e[0]
            break

    downtime_30 = _since(view.downtime, start_30)
    downtime_7 = _since(downtime_30, start_7)
    stoppages_7, stoppages_30 = len(downtime_7), len(downtime_30)

    production_30 = _since(view.production, start_30)
    production_7 = _since(production_30, start_7)
    availability_7, performance_7, reject_7 = _production_rates(production_7)
    _, performance_30, reject_30 = _production_rates(production_30)

    inspected = failed = 0
    for _, n_inspected, n_failed in _since(view.inspections, start_14):
        inspected += n_inspected
        failed += n_failed

    last_maintenance = None
    overdue = 0
    today = as_of.date()
    for planned, completed, reactive, is_open in view.maintenance:
        if completed is not None and not reactive and (last_maintenance is None or completed > last_maintenance):
            last_maintenance = completed
        if is_open and planned < today:
            overdue += 1
    if last_maintenance is None:
        runtime_rows = view.production
        days_since_maint = float(LOOKBACK_DAYS)
    else:
        since = date_instant(last_maintenance)
        runtime_rows = _since(view.production, since)
        days_since_maint = min((as_of - since) / timedelta(days=1), float(LOOKBACK_DAYS))
    runtime_hours = min(sum(r[2] for r in runtime_rows) / 60.0, RUNTIME_H_CAP)

    return {
        "breakdowns_7d": float(len(breakdowns_7)),
        "breakdowns_30d": float(len(breakdowns_30)),
        "days_since_breakdown": (float(LOOKBACK_DAYS) if last_breakdown is None
                                 else min((as_of - last_breakdown).total_seconds() / _DAY_SECONDS,
                                          float(LOOKBACK_DAYS))),
        "never_broken_in_lookback": 1.0 if last_breakdown is None else 0.0,
        "downtime_min_7d": float(sum(parse_duration_to_minutes(r[2]) for r in downtime_7)),
        "downtime_min_30d": float(sum(parse_duration_to_minutes(r[2]) for r in downtime_30)),
        "stoppages_7d": float(stoppages_7),
        "stoppage_accel": stoppages_7 / (stoppages_30 * 7.0 / 30.0 + 1.0),
        "availability_7d": availability_7,
        "performance_7d": performance_7,
        "performance_drift": _difference(performance_7, performance_30),
        "reject_rate_7d": reject_7,
        "reject_rate_drift": _difference(reject_7, reject_30),
        "inspection_fail_rate_14d": _ratio(float(failed), inspected),
        "runtime_h_since_maint": float(runtime_hours),
        "no_maint_in_lookback": 1.0 if last_maintenance is None else 0.0,
        "days_since_maint": float(days_since_maint),
        "overdue_maint_open": float(overdue),
    }
