"""A machine's recent history, and the ONE rule for which part of it a prediction may see.

WHY THIS MODULE EXISTS
----------------------
The model is trained on synthetic histories that run for 400 days and served
on database histories that the loader bounds to ``LOOKBACK_DAYS``. If the two
paths computed features over different spans, "never broken" would mean "in
400 days" in training and "in 120 days" in production, and the model would be
scored on inputs it never learned. So BOTH paths call ``truncate(h, as_of)``
here, and every feature is computed from its output.

WINDOWS ARE HALF-OPEN
---------------------
A prediction at ``as_of`` may see ``[as_of - LOOKBACK_DAYS, as_of)``. An event
stamped exactly ``as_of`` belongs to the future. ``window_bounds`` is the same
[start, end) as ``oee_contract.OeeWindow(days, now=as_of)`` (pinned by
test_amp_ai_failure_risk_features.py; it is not imported here because
oee_contract imports the database layer).

The label window is ``[as_of, as_of + HORIZON_DAYS)``, which tiles against the
feature window: no instant belongs to neither. (The plan wrote ``(t, t + 7d]``;
with half-open feature windows that would leave the instant ``as_of`` in
neither window.)

DATE COLUMNS
------------
``MaintenanceTask.planned_date`` / ``completed_date`` are dates, not instants.
A date ``D`` is read as the instant ``D 00:00``. ``date_bounds(start, end)``
turns an instant window into the date range ``[lo, hi)`` holding exactly the
dates whose instant lies in ``[start, end)``; the SQL loader uses the same
function, so the database and in-memory paths select the same tasks.

A task is kept if its planned OR its completed date is in the window. A
completion dated at or after ``as_of`` has not happened yet at ``as_of``: the
truncated copy shows that task as open, with no completion date.

STATE AT WINDOW START
---------------------
The rule scorer and the "already broken?" check need the machine's status at
``as_of``. When no event falls inside the lookback, the status comes from the
last event in ``[start - STATE_LOOKBACK_DAYS, start)``. That span is bounded
because ``machine_events`` is kept for 180 days (retention.py): 120 days of
lookback leaves 60 days of older events that can still exist.

RECORD SHAPES (each list sorted by time)
----------------------------------------
    events       (ts, old_status, new_status, utilization)
    downtime     (ts, reason, duration_text)
    production   (ts, planned_min, runtime_min, ideal_cycle_s, total, good, rejected)
    inspections  (ts, inspected, failed)
    maintenance  (planned_date, completed_date | None, is_reactive, is_open)

Standard library only (test_amp_ai_failure_risk_purity.py).
"""
import bisect
from datetime import datetime, time, timedelta

__all__ = [
    "LOOKBACK_DAYS", "HORIZON_DAYS", "STATE_LOOKBACK_DAYS", "BREAKDOWN",
    "MachineHistory", "window_bounds", "date_instant", "date_bounds",
    "truncate", "status_at", "in_breakdown_at", "breakdown_in_horizon",
]

LOOKBACK_DAYS = 120
HORIZON_DAYS = 7
STATE_LOOKBACK_DAYS = 60
BREAKDOWN = "Breakdown"


def _ts(record):
    return record[0]


class MachineHistory:
    """Records for one machine. ``window_end`` is None for a full history, or the
    ``as_of`` it was truncated (or loaded) for; ``state_at_window_start`` is then
    authoritative for that window."""

    __slots__ = ("machine_id", "name", "state_at_window_start", "events", "downtime",
                 "production", "inspections", "maintenance", "window_end")

    def __init__(self, machine_id, name, *, state_at_window_start=None, events=(), downtime=(),
                 production=(), inspections=(), maintenance=(), window_end=None):
        self.machine_id = machine_id
        self.name = name
        self.state_at_window_start = state_at_window_start
        self.events = list(events)
        self.downtime = list(downtime)
        self.production = list(production)
        self.inspections = list(inspections)
        self.maintenance = list(maintenance)
        self.window_end = window_end

    def __repr__(self):
        return (f"MachineHistory({self.machine_id!r}, {self.name!r}, events={len(self.events)}, "
                f"downtime={len(self.downtime)}, production={len(self.production)}, "
                f"inspections={len(self.inspections)}, maintenance={len(self.maintenance)}, "
                f"window_end={self.window_end!r})")


def window_bounds(as_of, days):
    """[start, end) for the ``days`` before ``as_of`` - the same bounds as OeeWindow(days, now=as_of)."""
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    return as_of - timedelta(days=days), as_of


def date_instant(d):
    """The instant a date column is read as: 00:00 on that date."""
    return datetime.combine(d, time.min)


def date_bounds(start, end):
    """(lo, hi): the dates D with start <= D 00:00 < end are exactly lo <= D < hi."""
    lo = start.date() if start.time() == time.min else start.date() + timedelta(days=1)
    hi = end.date() if end.time() == time.min else end.date() + timedelta(days=1)
    return lo, hi


def _slice(records, start, end):
    i = bisect.bisect_left(records, start, key=_ts)
    j = bisect.bisect_left(records, end, lo=i, key=_ts)
    return records[i:j]


def truncate(h, as_of):
    """A NEW MachineHistory holding exactly what a prediction at ``as_of`` may see."""
    if not isinstance(h, MachineHistory):
        raise TypeError("truncate expects a MachineHistory")
    if h.window_end is not None and h.window_end != as_of:
        raise ValueError(f"history was truncated for {h.window_end}, cannot re-truncate for {as_of}")
    start, end = window_bounds(as_of, LOOKBACK_DAYS)

    before = _slice(h.events, start - timedelta(days=STATE_LOOKBACK_DAYS), start)
    if before:
        state = (before[-1][2], before[-1][3])
    elif h.window_end is not None:
        state = h.state_at_window_start
    else:
        state = None

    lo, hi = date_bounds(start, end)
    maintenance = []
    for planned, completed, reactive, is_open in h.maintenance:
        in_window = lo <= planned < hi or (completed is not None and lo <= completed < hi)
        if not in_window:
            continue
        if completed is not None and not completed < hi:
            # completed at or after as_of: at as_of the work was still outstanding
            maintenance.append((planned, None, reactive, True))
        else:
            maintenance.append((planned, completed, reactive, is_open))

    return MachineHistory(
        h.machine_id, h.name, state_at_window_start=state,
        events=_slice(h.events, start, end),
        downtime=_slice(h.downtime, start, end),
        production=_slice(h.production, start, end),
        inspections=_slice(h.inspections, start, end),
        maintenance=maintenance,
        window_end=as_of,
    )


def _visible(h, as_of):
    return h if h.window_end == as_of else truncate(h, as_of)


def status_at(h, as_of):
    """(status, utilization) just before ``as_of`` as the truncated view sees it, or None if unknown."""
    view = _visible(h, as_of)
    if view.events:
        last = view.events[-1]
        return last[2], last[3]
    return view.state_at_window_start


def in_breakdown_at(h, as_of):
    """True when the machine is already down at ``as_of``: it cannot start a NEW breakdown."""
    state = status_at(h, as_of)
    return state is not None and state[0] == BREAKDOWN


def breakdown_in_horizon(h, as_of, horizon_days=HORIZON_DAYS):
    """1 if a MachineEvent with new_status 'Breakdown' starts in [as_of, as_of + horizon), else 0.

    Needs the FULL history: a truncated one has, by construction, no future.
    """
    if h.window_end is not None:
        raise ValueError("labels need the full history, not a truncated view")
    for record in _slice(h.events, as_of, as_of + timedelta(days=horizon_days)):
        if record[2] == BREAKDOWN:
            return 1
    return 0
