"""Canonical work-order status vocabulary — what "still open" means, once.

Modelled on machine_status.py, and for the same reason: the same question was
being answered in four places, and they disagreed.

    analytics_routes:1020   IN ("Running", "Planned")     the command centre
    analytics_routes:462    Running + In Progress folded  /analytics/work-orders
    ai/flow.py:70           NOT IN _CLOSED                the WIP board
    predictive_engine:33    ("Running", "Delayed")        the risk factor

`factory_simulator.py:279` writes the book as

    ["Planned", "In Progress", "In Progress", "In Progress", "Completed", "On Hold"]

so on a seeded plant HALF the orders are "In Progress" and none are "Running".
The command centre's "Work Orders" KPI counted the Planned sixth; the predictive
risk factor matched nothing at all.

A COMPLEMENT, NOT A WHITELIST
------------------------------
This lists the states that mean the work is FINISHED, and treats everything else
as open. That is deliberate and is the lesson of the whole family of defects
this came from: every one of them was a whitelist that missed a word —
"In Progress" here and in the operator terminal (#568), "Partially Received" in
purchasing (#569), "Corrective" in the CMMS card (#576). A whitelist has to
enumerate every word anyone will ever write; a closed-set complement only has to
enumerate the endings, and a word nobody thought of defaults to OPEN, which is
the safe direction for a backlog.

The set is taken verbatim from `ai/flow.py`, which already had it right and now
imports from here rather than keeping its own copy. Matched lowercased and
trimmed, so a migration or an API client writing "  COMPLETED  " cannot open a
hole, and a NULL reads as open (an unfinished row with no status is still work).
"""
from sqlalchemy import func

import models

# The states that mean the work is finished. Everything else is open.
#
# "Cancelled" is here with "Completed": a withdrawn order is off the board, not
# outstanding — the same call ai/supply.py makes for a cancelled PO (#566) and
# ai/maintenance.py for a rejected task (#562).
CLOSED_STATUSES = ("completed", "complete", "done", "closed", "cancelled", "canceled")


def is_closed(status) -> bool:
    """True when the work order is finished or withdrawn.

    Lowercased and trimmed, so vocabulary drift and stray whitespace cannot
    reopen a closed order or close an open one. A NULL is NOT closed."""
    return (status or "").strip().lower() in CLOSED_STATUSES


def open_clause():
    """SQL for "this work order is still open" — the complement of
    CLOSED_STATUSES.

    COALESCE+LOWER+TRIM because `status` is Column(String, default="Planned")
    WITHOUT nullable=False: a raw-SQL or migrated row can hold a real NULL, and
    SQL's `status NOT IN (...)` is NULL — not TRUE — for it, which silently drops
    the row from a count. An unfinished row with no status is still open.

    test_work_order_active.py pins this against is_closed() row-for-row, so the
    SQL and the Python cannot drift into two rules."""
    return func.lower(func.trim(func.coalesce(models.WorkOrder.status, ""))).notin_(
        CLOSED_STATUSES)
