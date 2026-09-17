"""Statement periods and covered hours for a downtime attribution contract (ADR-0020).

ANCHORED, NOT ROLLING
---------------------
A statement period is [local midnight on the 1st, local midnight on the 1st
`period_months` later), in the contract's timezone, converted to naive UTC.
Periods start at the contract's `starts_at` and tile it with no gap and no
overlap. `ends_at` and any `termination_effective_at` fall on a boundary, so
every period is whole and no fee is ever prorated.

This module deliberately does NOT use oee_contract.OeeWindow. That window is
ROLLING, [now - days, now); a statement period is ANCHORED to the calendar.
Evaluating one against the other is the class of defect behind a 17-finding
audit, so test_contract_periods fails if this module imports or names it.

The period grid (timezone, period_months) is fixed for a contract's life: an
amendment that changed it would move every boundary under statements that
already exist.

COVERED HOURS
-------------
`covered_intervals` expands the terms' coverage for one period: 24x7 is the
whole period; weekly windows are laid on each LOCAL date (0=Monday), excluded
local dates are dropped, each window edge is converted to UTC, overlapping or
touching intervals merge, and the result is clipped to the period and to the
active range the caller passes (the term version's effective range).

LOCAL TIME THAT DOES NOT EXIST, OR EXISTS TWICE
-----------------------------------------------
`local_to_utc` is the one conversion. A local time inside a spring-forward gap
(Europe/London 01:30 on the last Sunday of March) moves FORWARD to the first
instant that exists, the transition itself (02:00 BST). A local time that
happens twice (fall-back) takes its FIRST occurrence (fold=0). A window lying
wholly inside a gap therefore covers nothing that day.

All results are naive UTC with whole seconds; every interval is half-open.

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_periods.py
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

# A contract term is at most 120 months (contract_terms.MAX_TERM_MONTHS); the
# grid walk refuses to run away past this many boundaries on a bad input.
_MAX_BOUNDARIES = 1200


class PeriodError(ValueError):
    """An instant is not where the period grid requires it to be."""


@dataclass(frozen=True, order=True)
class Period:
    start: datetime      # naive UTC, inclusive
    end: datetime        # naive UTC, exclusive


def _utc_to_local(instant, zone):
    return instant.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


def local_to_utc(local, zone):
    """A naive local wall time in `zone` as naive UTC. The one conversion.

    Non-existent (gap) times move forward to the transition instant; ambiguous
    (fold) times take their first occurrence."""
    if not isinstance(local, datetime) or local.tzinfo is not None:
        raise PeriodError(f"local_to_utc needs a naive local datetime, not {local!r}")
    early = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc).replace(tzinfo=None)
    if _utc_to_local(early, zone) == local:
        return early
    # Inside a gap. fold=0 used the offset before the transition (an instant
    # after it); fold=1 the offset after (an instant before it). The transition
    # is the first whole second whose local time is >= `local`.
    late = local.replace(tzinfo=zone, fold=1).astimezone(timezone.utc).replace(tzinfo=None)
    lo, hi = min(early, late), max(early, late)
    if not _utc_to_local(lo, zone) < local <= _utc_to_local(hi, zone):
        raise PeriodError(f"cannot place local time {local} in {zone}")
    while hi - lo > timedelta(seconds=1):
        mid = lo + timedelta(seconds=(hi - lo) // timedelta(seconds=1) // 2)
        if _utc_to_local(mid, zone) >= local:
            hi = mid
        else:
            lo = mid
    return hi


def _month_start(zone, year, month):
    """Local midnight on the 1st of (year, month), as UTC. Month may overflow."""
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return local_to_utc(datetime(year, month, 1), zone)


def month_start_utc(terms, year, month):
    """Local midnight on the 1st of (year, month) in the terms' timezone, as UTC.

    How a contract's `starts_at` is made from a chosen month. `month` may run
    past 12 (it carries into the year)."""
    if type(year) is not int or type(month) is not int:
        raise PeriodError("month_start_utc needs integer year and month")
    return _month_start(terms.zone, year, month)


def _naive_utc(instant, name):
    if not isinstance(instant, datetime) or instant.tzinfo is not None:
        raise PeriodError(f"{name} must be a naive UTC datetime, not {instant!r}")
    if instant.microsecond:
        raise PeriodError(f"{name} {instant} has a fractional second")
    return instant


def is_month_start(terms, instant):
    """Is `instant` local midnight on the 1st of a month in the terms' timezone?"""
    _naive_utc(instant, "instant")
    zone = terms.zone
    local = _utc_to_local(instant, zone)
    return _month_start(zone, local.year, local.month) == instant


def _boundaries(terms, starts_at, until):
    """Grid boundaries from starts_at while the previous one is < until."""
    _naive_utc(starts_at, "starts_at")
    if not is_month_start(terms, starts_at):
        raise PeriodError(
            f"starts_at {starts_at} is not local midnight on the 1st of a month in "
            f"{terms.timezone}")
    zone = terms.zone
    local = _utc_to_local(starts_at, zone)
    out = [starts_at]
    k = 0
    while out[-1] < until:
        k += 1
        if k > _MAX_BOUNDARIES:
            raise PeriodError(f"{until} is further than {_MAX_BOUNDARIES} periods away")
        out.append(_month_start(zone, local.year, local.month + k * terms.period_months))
    return out


def periods(terms, starts_at, effective_end):
    """The whole periods [start, end) tiling [starts_at, effective_end).

    Raises PeriodError when starts_at is not a month start or effective_end is
    not on the period grid."""
    _naive_utc(effective_end, "effective_end")
    if effective_end <= starts_at:
        _boundaries(terms, starts_at, starts_at)       # still validates starts_at
        return []
    grid = _boundaries(terms, starts_at, effective_end)
    if grid[-1] != effective_end:
        raise PeriodError(
            f"effective end {effective_end} is not a period boundary (the nearest "
            f"after it is {grid[-1]})")
    return [Period(a, b) for a, b in zip(grid, grid[1:])]


def is_boundary(terms, starts_at, instant):
    """Is `instant` a boundary of the contract's period grid (at or after starts_at)?"""
    _naive_utc(instant, "instant")
    if instant < starts_at:
        return False
    return _boundaries(terms, starts_at, instant)[-1] == instant


def boundary_at_or_after(terms, starts_at, instant):
    """The first grid boundary >= instant (starts_at when instant precedes it).

    For a termination: effective at the first boundary at or after now + notice."""
    if not isinstance(instant, datetime) or instant.tzinfo is not None:
        raise PeriodError(f"instant must be a naive UTC datetime, not {instant!r}")
    return _boundaries(terms, starts_at, instant)[-1]


def contract_effective_end(contract):
    """min(ends_at, termination_effective_at). Derived at read time; no sweeper."""
    end = contract.ends_at
    if contract.termination_effective_at is not None:
        end = min(end, contract.termination_effective_at)
    return end


def period_starting(contract, terms, start):
    """The whole period of this contract that starts at `start`, or None."""
    if not isinstance(start, datetime) or start.tzinfo is not None or start.microsecond:
        return None
    for p in periods(terms, contract.starts_at, contract_effective_end(contract)):
        if p.start == start:
            return p
    return None


def _merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def covered_intervals(period, terms, active_start, active_end):
    """Covered UTC intervals of `period`, clipped to [active_start, active_end).

    Sorted, disjoint, non-touching, half-open (start, end) tuples."""
    lo = max(period.start, active_start)
    hi = min(period.end, active_end)
    if lo >= hi:
        return []
    coverage = terms.coverage
    if coverage.mode == "24x7":
        return [(lo, hi)]
    zone = terms.zone
    excluded = set(coverage.excluded_dates)
    first = _utc_to_local(period.start, zone).date() - timedelta(days=1)
    last = _utc_to_local(period.end, zone).date()
    raw = []
    day = first
    while day <= last:
        if day not in excluded:
            midnight = datetime.combine(day, time())
            for w in coverage.windows:
                if day.weekday() in w.days:
                    s = local_to_utc(midnight + timedelta(minutes=w.start_minute), zone)
                    e = local_to_utc(midnight + timedelta(minutes=w.end_minute), zone)
                    if e > s:
                        raw.append((s, e))
        day += timedelta(days=1)
    clipped = [(max(s, lo), min(e, hi)) for s, e in _merge(raw)]
    return [(s, e) for s, e in clipped if s < e]
