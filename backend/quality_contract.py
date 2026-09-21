"""THE plant quality figure — one window, one definition.

WHY THIS MODULE EXISTS
----------------------
"Fail rate" was published on THREE bases at once, none of them stated on the
screen that showed it:

  * `ai/quality.build_quality_summary` — the last seven CALENDAR dates
    (`created_at >= midnight(today-6)`, no upper bound), feeding the Quality
    intel card, the snapshot, the morning briefing's alert, the command centre,
    the copilot and the assistant;
  * `GET /analytics/quality` — EVERY inspection ever recorded, feeding the
    Quality view's "Pass Rate" and "Fail Rate" tiles;
  * `GET /analytics/factory-command-center` — every inspection ever recorded,
    feeding the digital twin's "Quality Fail" tile.

Meanwhile `ai/twin._machine_quality` was moved onto the canonical rolling
window by #590, so the machine cockpit's fail rate and the plant's already
disagreed by construction. A plant that ran badly last quarter and cleanly this
week read 11% on the twin, 11% on the Quality view and 3% on the intel card —
three true numbers, one label, and nothing on screen saying which week each
one meant.

THE RULE
--------
One window: `oee_contract.OeeWindow`, the same half-open `[start, end)` the
plant OEE, the cost trend, the shift attainment and the machine cockpit are
all on. Every plant-level quality figure comes from `plant_quality` and carries
the window it was measured over, so a screen can name it.

AND A RATE OVER NOTHING IS NOT ZERO
-----------------------------------
`round(part / whole * 100) if whole else 0` was in five places. Zero is the
BEST value on a fail-rate scale, so a plant that inspected nothing published a
perfect quality record — the same defect the shift attainment had (#698) and
the health score had (#697). `rate()` returns None instead, `measured` says
which it was, and the screens print a dash.

Tenant filtering is EXPLICIT here rather than left to the ADR-0002 hook,
because this is also read from the AI context builder and from scripts, and a
headline number resolved through an ambient binding is the shape of the ingest
defect in ADR-0011. `rows_in` — the row-level breakdowns — stays on the hook,
as `shift_contract.rows_in` does.
"""
from sqlalchemy import func

import models
import oee_contract


def rate(part, whole):
    """`part` as a whole-number percentage of `whole`, or None when there is no
    denominator.

    None is the point, and it is not a nicety: every caller that used to write
    `... if whole else 0` was publishing 0% — the best possible fail rate — for
    a plant that had inspected nothing at all."""
    return round(part / whole * 100) if whole else None


def rate1(part, whole):
    """The same rate to one decimal, or None. A trend has to resolve movement
    that an integer percentage would round away."""
    return round(part / whole * 100, 1) if whole else None


def _sums(db, tenant, window):
    """The window's pooled inspection totals in ONE aggregate SELECT.

    passed / failed / rework / scrap_quantity are `Column(Integer, default=0)`
    WITHOUT nullable=False, so a row written by raw SQL, a migration, or an
    update that cleared the field can hold a real NULL. SUM ignores NULLs and
    returns NULL for an empty group, so each is coalesced to the column's own
    default of 0 (inspected_quantity IS nullable=False, so its SUM is exact)."""
    QI = models.QualityInspection
    q = db.query(
        func.count(QI.id),
        func.coalesce(func.sum(QI.inspected_quantity), 0),
        func.coalesce(func.sum(QI.passed_quantity), 0),
        func.coalesce(func.sum(QI.failed_quantity), 0),
        func.coalesce(func.sum(QI.rework_quantity), 0),
        func.coalesce(func.sum(QI.scrap_quantity), 0),
    ).filter(QI.tenant_code == tenant)
    if window.start is not None:
        q = q.filter(QI.created_at >= window.start)
    q = q.filter(QI.created_at < window.end)
    # int() so a DB that returns Decimal for SUM (PostgreSQL) matches the plain
    # ints the payload types expect, and so the rates divide cleanly.
    return tuple(int(v or 0) for v in q.one())


def plant_quality(db, tenant, window=None):
    """THE plant quality figure. Every user-facing plant rate calls this.

    Numerator and denominator read the same rows over the same window, so the
    totals and the rates beside them reconcile."""
    window = window or oee_contract.OeeWindow()
    inspections, inspected, passed, failed, rework, scrap = _sums(db, tenant, window)
    return {
        "inspections": inspections,
        "inspected": inspected,
        "passed": passed,
        "failed": failed,
        "rework": rework,
        "scrap": scrap,
        "first_pass_yield": rate(passed, inspected),
        "fail_rate": rate(failed, inspected),
        # Not "were there rows" — were there UNITS. Ten inspections of nothing
        # still divide by zero.
        "measured": inspected > 0,
        "window": window.label(),
        "days": window.days,
        "window_start": window.start.isoformat() if window.start else None,
        "window_end": window.end.isoformat(),
    }


def rows_in(db, window, machine_id=None):
    """The window's inspections, oldest first, bounded in SQL at BOTH ends.

    quality_inspections grows with every inspection, so this is never an
    unbounded read; the half-open upper bound also keeps a future-dated row out
    of a window it did not happen in (the defect #590 found on the machine
    cockpit). Tenant scope comes from the ADR-0002 hook — these rows feed
    breakdowns rendered inside a request, not a headline figure."""
    q = db.query(models.QualityInspection)
    if machine_id is not None:
        q = q.filter(models.QualityInspection.machine_id == machine_id)
    if window.start is not None:
        q = q.filter(models.QualityInspection.created_at >= window.start)
    return (q.filter(models.QualityInspection.created_at < window.end)
             .order_by(models.QualityInspection.id).all())
