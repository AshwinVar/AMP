"""THE plant shift attainment: one window, one formula, one function.

WHAT THIS FIXES
---------------
A shift's attainment (actual output as a % of target) had ONE formula since
test_no_target_no_shift_efficiency.py — `analytics_engine.shift_attainment`,
None without a target — and THREE windows:

    ai/shift.build_shift_summary       the last 7 days (its own rolling cutoff)
    /analytics/summary                 every shift ever recorded
    /analytics/management              every shift ever recorded (three lines
                                       under a production sum that IS windowed)
    /analytics/executive-oee           the most recent 50 rows — a count, not a
                                       span, the same shape oee_contract names

So a plant that ran badly last quarter and well this week read 96% on the
shift card, 88% on the Executive OEE page and 71% on the management dashboard,
for the same shifts on the same day, and the comment beside the lifetime sum
said it was "the same basis as the shift read-model". The standard had been
applied to the FORMULA and not to the SPAN — exactly what
test_oee_rollups_one_window.py records for the plant OEE.

THE RULE
--------
Every plant-wide attainment is `pooled_attainment(db, tenant, window)`: the
canonical `oee_contract.OeeWindow` (half-open, the same seven days the OEE
figures pool), a ratio of sums (a no-target shift's output joins the numerator
only), and `None` when no shift in the window had a target — with `measured`,
`window` and `days` beside the number so a surface can say what it shows.

Legacy response keys that promised an integer (`avg_shift_efficiency`,
`target_achievement`, `production_achievement`) keep their 0 and carry a
`*_measured` flag beside it, as `/analytics/summary` already did.
"""
from sqlalchemy import func

import models
import oee_contract
# The one formula (None without a target), shared with every per-shift figure.
from analytics_engine import shift_attainment


def _sums(db, tenant, window):
    """Total target, total actual and the row count inside `window`, in SQL.

    Filtered by tenant EXPLICITLY, like oee_contract._sums: this is read from
    scripts and exports as well as requests, and a headline number resolved
    through the ambient tenant binding is the shape of the ADR-0011 defect.
    """
    q = db.query(
        func.coalesce(func.sum(models.ShiftData.target_output), 0),
        func.coalesce(func.sum(models.ShiftData.actual_output), 0),
        func.count(models.ShiftData.id),
    ).filter(models.ShiftData.tenant_code == tenant)
    if window.start is not None:
        q = q.filter(models.ShiftData.created_at >= window.start)
    q = q.filter(models.ShiftData.created_at < window.end)
    target, actual, entries = q.one()
    # int() on every value: PostgreSQL returns Decimal from SUM over an Integer
    # column; SQLite returns int. A suite run only on SQLite never sees it.
    return int(target), int(actual), int(entries)


def pooled_attainment(db, tenant, window=None):
    """THE plant attainment over `window` (the canonical week when omitted).

    Returns the figure with its basis:

        attainment   round(actual / target * 100), or None when no shift in
                     the window had a target (there is nothing to attain)
        target       total planned output in the window
        actual       total output in the window (a no-target shift's output
                     counts here and nowhere else)
        entries      how many shift rows the window holds
        measured     target > 0 — the same integer-plus-flag convention
                     /analytics/summary already carried
        window       "last 7 days"; days / window_start / window_end beside it
    """
    window = window or oee_contract.OeeWindow()
    target, actual, entries = _sums(db, tenant, window)
    return {
        "attainment": shift_attainment(actual, target),
        "target": target,
        "actual": actual,
        "entries": entries,
        "measured": target > 0,
        "window": window.label(),
        "days": window.days,
        "window_start": window.start.isoformat() if window.start else None,
        "window_end": window.end.isoformat(),
    }


def rows_in(db, window):
    """The shift rows inside `window`, oldest first — for the per-shift
    breakdowns that sit under a pooled headline (the Executive OEE chart, the
    shift card), so a breakdown always sums to the headline above it. Bounded
    by the window, not by a row count; the ADR-0002 hook scopes the tenant."""
    q = db.query(models.ShiftData)
    if window.start is not None:
        q = q.filter(models.ShiftData.created_at >= window.start)
    return (q.filter(models.ShiftData.created_at < window.end)
             .order_by(models.ShiftData.id).all())
