"""THE canonical OEE contract. One definition, one window rule, one honesty rule.

Every user-facing OEE figure comes from here. The maths was never the problem —
``analytics_engine.pooled_oee`` has been correct for a while — the problem was
that each surface chose its own RECORD SET and its own idea of what "no data"
means, so the same factory answered differently depending on where you asked.

MEASURED BEFORE THIS MODULE EXISTED
-----------------------------------
One factory, one moment, asked four ways:

    surface                                       window                OEE
    ----------------------------------------------------------------------
    /oee-summary  (the dashboard)                 last 7 days           100%
    ai_copilot LLM context                        last 10 records        10%
    pooled over everything in the table           all time               17%
    pooled over the 7-day window                  last 7 days           100%

A customer asks the assistant "how is the plant doing?" and gets an answer 90
points from the dashboard's, because ``ai_copilot`` windowed by ``.limit(10)``
— a COUNT of rows, not a span of time.

And a machine falling off the network made the plant look better:

    plant OEE while the worst machine still reports : 67%
    plant OEE after its gateway goes silent         : 94%   (+27 points)

Its rows simply stop existing, so it leaves the denominator. The number improves
exactly when visibility is lost, which is the worst possible direction for a
metric to move.

============================================================================
THE MATHEMATICS
============================================================================

For a set of production records R, all sums taken over R:

    Availability  A = Σ runtime_minutes / Σ planned_minutes
    Performance   P = Σ (ideal_cycle_time_seconds × total_count)
                      ────────────────────────────────────────
                              Σ runtime_minutes × 60
    Quality       Q = Σ good_count / Σ total_count
    OEE             = A × P × Q

POOLED, not averaged. Each component is a ratio of SUMS, so a machine is
weighted by the time and volume it actually contributed. Averaging per-record
OEE (a mean of ratios) over-weights a tiny run: a machine that made ten parts
badly would count as much as one that made ten thousand well.

Every component is clamped to [0, 1] — symmetrically. Above 1 is a figure the
data cannot support (runtime beyond planned, good beyond total); below 0 is the
same violation in the other direction, reachable from a legacy row holding a
negative count. Percentages are rounded once, at the presentation boundary.

============================================================================
THE WINDOW
============================================================================

    window = [now - days, now)        start INCLUSIVE, end EXCLUSIVE

Stated once, here, because the boundary rule was previously each caller's own
choice and a record landing exactly on the cutoff could be counted or not
depending on which surface asked. Half-open is the standard that makes adjacent
windows tile without overlap: [d-14, d-7) and [d-7, d) share no record, so a
week-over-week comparison cannot double-count the boundary.

`days=None` means all time, and is only correct for an explicit "since
commissioning" view. It is never the default.

============================================================================
WHAT COUNTS AS WHAT
============================================================================

PLANNED TIME
    The sum of ``planned_minutes`` on records inside the window. A period with
    NO record contributes no planned time — we do not know a shift was planned
    unless something said so. This is deliberately conservative: inventing
    planned minutes for a silent machine would manufacture downtime.

UNPLANNED
    ``planned_minutes == 0``: no shift was scheduled. Such a record contributes
    nothing to availability's denominator, and a set consisting only of
    unplanned records has NO availability — not 0%. There was nothing to be
    efficient at. Reporting 0% for an unscheduled weekend is a fabricated loss.

NO DATA
    ``has_data`` is True only when the window contains something measurable:
    planned minutes > 0 OR total count > 0. It previously meant "at least one
    row exists", so an empty shift rendered as a measured 0% OEE. A row that
    records nothing is not evidence of poor performance.

OFFLINE
    A machine whose telemetry has stopped. It cannot be measured, so it
    contributes nothing to the numerator or the denominator — that part is
    unavoidable. What is NOT acceptable is letting it vanish silently, which is
    how a comms fault turned into a 27-point improvement. So:

MEASUREMENT COVERAGE
    Every plant-level result carries how much of the plant it actually measured:

        machines_expected   machines the tenant has
        machines_reporting  machines with at least one record in the window
        coverage_pct        reporting / expected

    A figure with coverage below 100% is a figure about PART of the plant, and
    every surface that shows the number must be able to say so. This does not
    invent the missing data — it refuses to present a partial measurement as a
    whole one.

============================================================================
KNOWN LIMITATIONS — stated, not silently worked around
============================================================================

These are properties of the schema, not of this module. They are written here
because a contract that omits what it cannot do is not a contract.

1. DOWNTIME DOES NOT RECONCILE WITH AVAILABILITY. Availability comes from
   ProductionRecord (planned − runtime); downtime comes from DowntimeLog. They
   are separate tables with no link. Measured: a shift losing 80 minutes by the
   production record carried 120 minutes of downtime logs — 83% availability
   against 75% implied. Nothing detects the contradiction.

2. OVERLAPPING DOWNTIME IS NOT REPRESENTABLE. DowntimeLog stores a duration and
   a created_at, not a start and an end. Two operators logging the same hour as
   a breakdown and a changeover produce 120 minutes of loss from 60 minutes of
   stoppage, and no query can tell. Fixing this needs an interval column, not a
   calculation.

3. REWORK IS INVISIBLE. A reworked unit is either counted good (hiding the
   first-pass failure) or scrap (hiding the recovery). First-pass yield cannot
   be derived from ``total_count`` and ``good_count`` alone.

4. DAY AND SHIFT BOUNDARIES ARE UTC. TenantConfig has no timezone. A plant on
   IST has its 06:00 shift split across two UTC "days" in every daily rollup,
   and a night shift crosses midnight mid-shift. Daily trends are therefore
   UTC-day trends, which is correct only for a UTC plant.
"""
from datetime import datetime, timedelta

from sqlalchemy import func

import models

# The default reporting window, in days. Seven because a factory week is the
# unit operations actually manage in, and because a shorter window makes the
# number swing on one bad shift.
DEFAULT_WINDOW_DAYS = 7

# The smallest gap the storage layer can distinguish. datetime carries
# microseconds and both PostgreSQL `timestamp` and SQLAlchemy's SQLite DATETIME
# string round-trip them, so one microsecond is genuinely the next representable
# instant rather than an arbitrary fudge.
_TICK = timedelta(microseconds=1)


def _now():
    """The current instant, as the default window uses it.

    A seam, and only a seam. It exists so the boundary rule can be tested by
    PINNING the clock: the alternative is a test that hopes a record and a query
    land in the same tick, which fails about a third of the time on Windows and
    essentially never on CI — non-evidence of exactly the kind that let the
    boundary bug survive in the first place.
    """
    return datetime.utcnow()


class OeeWindow:
    """A half-open time window [start, end). See the module docstring.

    Deliberately carries no `contains()` helper. The boundary rule is applied
    ONCE, in the SQL filter in `_sums`/`coverage`. A second Python
    implementation of the same rule is how two definitions drift apart — which
    is the defect this whole module exists to remove — and mutation testing
    confirmed it: breaking the unused helper failed nothing, because nothing
    called it.
    """

    __slots__ = ("start", "end", "days")

    def __init__(self, days=DEFAULT_WINDOW_DAYS, now=None):
        # THE DEFAULT END IS THE NEXT INSTANT, NOT THIS ONE.
        #
        # `end` is EXCLUSIVE, and a ProductionRecord's created_at defaults to
        # datetime.utcnow() exactly as this did. A record written in the same
        # clock tick as the query therefore carried EXACTLY the window's end and
        # was dropped from its own window — measured live:
        #
        #     stored created_at   2026-09-05 02:19:59.037681
        #     window end          2026-09-05 02:19:59.037681
        #     created_at < end    -> false, so has_data was False
        #
        # utcnow() has ~15.6 ms granularity on Windows, so the collision is
        # common there (3 failures in 8 runs of test_ai_copilot_context.py) and
        # rare on a microsecond-grained Linux CI box, which is how it survived.
        #
        # The fix is NOT to widen the filter to `<=`: half-open is what makes
        # [d-14, d-7) and [d-7, d) tile without sharing a record, and
        # test_oee_window_boundary.py section 3 pins that. Instead the default
        # end is the next representable instant, so everything that has ALREADY
        # happened is inside a window ending "now" while the comparison stays
        # strictly exclusive. An explicit `now=` — what a caller passes to build
        # adjacent windows — is used verbatim.
        self.end = now if now is not None else _now() + _TICK
        self.days = days
        self.start = None if days is None else self.end - timedelta(days=days)

    def label(self):
        return "all time" if self.days is None else f"last {self.days} days"

    def __repr__(self):
        return f"OeeWindow({self.label()}, start={self.start}, end={self.end})"


def is_measurable(planned, total) -> bool:
    """Whether a window contains anything OEE can be computed from.

    THE rule, in one place, because it was in five. "Measurable" means something
    was scheduled or something was made — NOT "a row exists", which is what
    every caller outside this module used:

        analytics_routes.py   record_count > 0              /analytics/summary
        analytics_routes.py   bool(production_by_machine)   /analytics/executive-oee
        analytics_engine.py   len(records) > 0              pooled_oee
        analytics_engine.py   record_count > 0              build_management_summary

    A row that recorded nothing satisfies all four and satisfies none of the
    arithmetic, so an unrun week rendered as a MEASURED 0% OEE — the fabricated
    loss the WHAT COUNTS AS WHAT section above exists to forbid. A factory that
    did not run is not a factory that ran badly.
    """
    return float(planned or 0) > 0 or float(total or 0) > 0


def _clamp(x):
    """[0, 1], symmetrically. See the module docstring for why both ends."""
    return max(0.0, min(x, 1.0))


def oee_from_sums(planned, runtime, total, good, ideal_seconds):
    """The contract, from the five sums. The one place the formula lives.

    Returns floats and explicit None for an undefined component, so a caller can
    tell "measured zero" from "not measurable". `as_percentages` is what turns
    that into the rounded ints a UI shows.
    """
    planned = float(planned or 0)
    runtime = float(runtime or 0)
    total = float(total or 0)
    good = float(good or 0)
    ideal_seconds = float(ideal_seconds or 0)

    # None, not 0.0, when the denominator is absent. Availability over zero
    # planned minutes is not zero — it is undefined, because nothing was
    # scheduled. This distinction is the whole point of the honesty rule and is
    # erased the moment it becomes a float.
    availability = _clamp(runtime / planned) if planned > 0 else None
    performance = _clamp(ideal_seconds / (runtime * 60)) if runtime > 0 else None
    quality = _clamp(good / total) if total > 0 else None

    # OEE is a product: it needs all three. A missing component means the
    # product is not computable, not that it is zero.
    if None in (availability, performance, quality):
        oee = None
    else:
        oee = availability * performance * quality

    return {
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee,
        # Measurable means something was scheduled or something was made.
        # Previously this was "a row exists", so an empty shift reported a
        # measured 0%.
        "has_data": is_measurable(planned, total),
        "planned_minutes": planned,
        "runtime_minutes": runtime,
        "total_count": total,
        "good_count": good,
    }


def as_percentages(result, missing=None):
    """Round the contract to the integer percentages a surface displays.

    An undefined component becomes ``missing`` (None by default) rather than 0,
    so a caller cannot accidentally render "not measurable" as "measured zero".
    Pass ``missing=0`` only where a legacy response shape demands a number, and
    carry ``has_data`` beside it so the zero is readable.
    """
    out = dict(result)
    for key in ("availability", "performance", "quality", "oee"):
        v = result.get(key)
        out[key] = missing if v is None else round(v * 100)
    return out


def _sums(db, tenant, window, machine_id=None):
    q = db.query(
        func.coalesce(func.sum(models.ProductionRecord.planned_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.runtime_minutes), 0),
        func.coalesce(func.sum(models.ProductionRecord.total_count), 0),
        func.coalesce(func.sum(models.ProductionRecord.good_count), 0),
        func.coalesce(func.sum(models.ProductionRecord.ideal_cycle_time_seconds
                               * models.ProductionRecord.total_count), 0),
    ).filter(models.ProductionRecord.tenant_code == tenant)
    if window.start is not None:
        q = q.filter(models.ProductionRecord.created_at >= window.start)
    q = q.filter(models.ProductionRecord.created_at < window.end)
    if machine_id is not None:
        q = q.filter(models.ProductionRecord.machine_id == machine_id)
    row = q.one()
    # int() on every value: PostgreSQL returns Decimal from SUM over an Integer
    # column, and mixing Decimal with float raises TypeError. SQLite returns
    # float, so a suite run only on SQLite never sees it.
    return tuple(int(v) for v in row)


def coverage(db, tenant, window):
    """How much of the plant this window actually measured.

    The answer to the survivorship problem: a machine that stops reporting
    leaves the pooled figure, and without this nothing says so.
    """
    expected = (db.query(func.count(models.Machine.id))
                .filter(models.Machine.tenant_code == tenant).scalar()) or 0
    q = (db.query(func.count(func.distinct(models.ProductionRecord.machine_id)))
         .filter(models.ProductionRecord.tenant_code == tenant))
    if window.start is not None:
        q = q.filter(models.ProductionRecord.created_at >= window.start)
    reporting = q.filter(models.ProductionRecord.created_at < window.end).scalar() or 0
    return {
        "machines_expected": int(expected),
        "machines_reporting": int(reporting),
        "coverage_pct": round(reporting / expected * 100) if expected else None,
        "complete": expected > 0 and reporting == expected,
    }


def plant_oee(db, tenant, window=None):
    """THE plant OEE. Every user-facing plant figure calls this.

    Reads are filtered by tenant EXPLICITLY rather than relying on the ADR-0002
    hook, because this is also called from scripts, exports and the AI context
    builder, and a headline number resolved through an ambient binding is the
    shape of the ingest defect in ADR-0011.
    """
    window = window or OeeWindow()
    result = oee_from_sums(*_sums(db, tenant, window))
    result["window"] = window.label()
    result["window_start"] = window.start.isoformat() if window.start else None
    result["window_end"] = window.end.isoformat()
    result["coverage"] = coverage(db, tenant, window)
    return result


def machine_oee(db, tenant, machine_id, window=None):
    """One machine's OEE over the same window, by the same rules."""
    window = window or OeeWindow()
    result = oee_from_sums(*_sums(db, tenant, window, machine_id=machine_id))
    result["window"] = window.label()
    result["machine_id"] = machine_id
    return result
