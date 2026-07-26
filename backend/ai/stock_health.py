"""Stock health — the capital-tied-up side of the inventory story (ADR-0007).

Every existing inventory read-model looks at the *shortage* end: ``ai.inventory``
flags items at/below their reorder level, and ``ai.coverage`` dates the projected
stockout for the items running dry. Neither ever surfaces the opposite failure —
stock that is *sitting still*. ``ai.coverage`` explicitly classifies an item with
stock but no recent consumption as ``ok`` and drops it from its list, so a store
full of parts nobody is issuing is invisible on every board.

This read-model asks that question: *what am I holding that isn't moving, or that
I hold far more of than I use?* Over a long look-back window it splits the item
master into:

  * DEAD        — has stock, but zero outbound movement in the window: not being
                  consumed at all (obsolescence / dead-capital risk).
  * OVERSTOCKED — moving, but the current burn rate gives far more days of cover
                  than the overstock threshold: more on hand than the line needs.
  * HEALTHY     — has stock and is turning over within a sensible cover band.
  * EMPTY       — no stock on hand (nothing to hold; still counted so the parts
                  reconcile to the whole).

Honesty notes (ADR-0007):
  * Everything is in UNITS and DAYS. inventory_items carries no unit cost, so this
    NEVER puts a currency figure on the holding — it would be invented, and an
    invented number is worse than no number (rule 2).
  * DEAD means "no outbound movement in the last WINDOW_DAYS", not "never moved" —
    the honest, bounded definition. Proving a true zero-lifetime would need an
    unbounded per-item scan of a table that only grows (rule 4), and a part quiet
    for two months is already the signal.
  * ``excess_units`` for an overstocked item is the stock beyond OVERSTOCK_DAYS of
    cover at its own measured burn — a number derived from the data, floored at 0,
    never a rendering default (rule 2).
  * The per-state counts sum to ``total_items`` and the flagged-unit totals sum to
    their per-item parts (asserted in the tests, rule 3).

A pure read-model over inventory_items + inventory_transactions, auto-scoped to
the tenant by the query layer (ADR-0002); the transaction scan is bounded to the
window in SQL (created_at is indexed) and it adds no storage. The one definition
of "outbound" and "days of cover" is imported from ``ai.coverage`` so the two
inventory read-models can never disagree on what a burn rate means (rule 1).
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models
# One definition of "outbound consumption" and "days of cover", shared with the
# days-of-cover read-model so a burn rate means the same thing on both boards.
from ai.coverage import _days_of_cover, _is_outbound

name = "stock_health"

TOP_N = 10
# A longer look-back than the days-of-cover card (14 days): "nothing has moved"
# only means something over a span, and slow-but-real parts issue monthly.
WINDOW_DAYS = 60
# More than this many days of cover at the current burn is more than the line
# needs on hand — the item is overstocked. Six months is a conservative SME line.
OVERSTOCK_DAYS = 180

# State ranking for the worst-first list: dead capital leads, then overstock,
# then the healthy/empty remainder (which never reaches the displayed list).
_STATE_RANK = {"dead": 0, "overstocked": 1, "healthy": 2, "empty": 3}


def _excess_units(stock: int, daily_burn: float) -> int:
    """Units held beyond OVERSTOCK_DAYS of cover at the measured burn — the
    actionable "how much is over target" number. Floored at 0 (never negative),
    and 0 when the burn is high enough that OVERSTOCK_DAYS of cover already
    exceeds the stock."""
    target = daily_burn * OVERSTOCK_DAYS
    return max(0, round(stock - target))


def _classify(stock: int, daily_burn: float, days_of_cover):
    """The holding state for one item. ``days_of_cover`` is the shared coverage
    figure (None when there is stock but no burn — i.e. dead)."""
    if stock <= 0:
        return "empty"                                   # nothing on hand to hold
    if daily_burn <= 0:
        return "dead"                                    # stock, but nothing moving it
    if days_of_cover is not None and days_of_cover > OVERSTOCK_DAYS:
        return "overstocked"                             # moving, but far more than needed
    return "healthy"


def build_stock_health(db, tenant: str) -> dict:
    """Dead-stock and overstock across the item master: how many items are sitting
    with no movement or held far beyond need, the units tied up in each, and the
    specific items to review first (most units flagged first). inventory_items and
    inventory_transactions are auto-scoped (ADR-0002); the transaction scan is
    bounded to the window in SQL. Empty-safe: zeros, no divide-by-zero, no currency
    invented where the schema has no cost."""
    today = datetime.utcnow().date()
    cutoff = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())

    items = db.query(models.InventoryItem).all()

    # Recent outbound quantity per item, bounded in SQL (the table grows forever).
    # Same window bound and same _is_outbound definition the coverage card uses.
    burned: dict = defaultdict(int)
    txns = (db.query(models.InventoryTransaction)
            .filter(models.InventoryTransaction.created_at >= cutoff).all())
    for t in txns:
        if t.item_id is not None and _is_outbound(t):
            burned[t.item_id] += t.quantity or 0

    counts = {"dead": 0, "overstocked": 0, "healthy": 0, "empty": 0}
    dead_units = 0
    excess_units = 0
    rows = []
    for i in items:
        stock = i.current_stock or 0
        daily_burn = burned.get(i.id, 0) / WINDOW_DAYS          # units/day over the window
        days_of_cover = _days_of_cover(stock, daily_burn)       # shared definition (rule 1)
        state = _classify(stock, daily_burn, days_of_cover)
        counts[state] += 1

        if state == "dead":
            # The whole holding is idle capital while nothing is consuming it.
            flagged = stock
            dead_units += stock
        elif state == "overstocked":
            flagged = _excess_units(stock, daily_burn)
            excess_units += flagged
        else:
            continue                                            # healthy / empty aren't a holding risk

        rows.append({
            "item_code": i.item_code,
            "item_name": i.item_name,
            "category": i.category,
            "supplier": i.supplier,
            "unit": i.unit,
            "current_stock": stock,
            "reorder_level": i.reorder_level or 0,
            "daily_burn": round(daily_burn, 2),
            "days_of_cover": days_of_cover,          # None for dead (no burn to divide by)
            "state": state,
            "flagged_units": flagged,                # dead: full stock; overstocked: units over target
        })

    # Worst first: dead capital, then overstock; within each, the most flagged units.
    rows.sort(key=lambda r: (_STATE_RANK[r["state"]], -r["flagged_units"]))

    flagged_items = counts["dead"] + counts["overstocked"]
    flagged_units = dead_units + excess_units
    worst = rows[0] if rows else None

    if not items:
        verdict, tone = "No inventory on record.", "warn"
    elif counts["dead"]:
        verdict = (f"{counts['dead']} item{'s' if counts['dead'] != 1 else ''} "
                   f"sitting idle ({dead_units:,} units, no movement in {WINDOW_DAYS} days)"
                   + (f", {counts['overstocked']} overstocked" if counts["overstocked"] else "") + ".")
        tone = "bad"
    elif counts["overstocked"]:
        verdict = (f"{counts['overstocked']} item{'s' if counts['overstocked'] != 1 else ''} "
                   f"overstocked — {excess_units:,} units held beyond {OVERSTOCK_DAYS} days of cover.")
        tone = "warn"
    else:
        verdict, tone = "Stock is turning over — nothing dead or overstocked.", "good"

    return {
        "window_days": WINDOW_DAYS,
        "overstock_days": OVERSTOCK_DAYS,
        "total_items": len(items),
        # Per-state counts (sum to total_items — asserted in the tests, rule 3).
        "dead": counts["dead"],
        "overstocked": counts["overstocked"],
        "healthy": counts["healthy"],
        "empty": counts["empty"],
        # Units tied up (each sums to its per-item parts — asserted in the tests).
        "dead_units": dead_units,
        "excess_units": excess_units,
        "flagged_items": flagged_items,
        "flagged_units": flagged_units,
        "worst": worst,
        "items": rows[:TOP_N],
        "verdict": verdict,
        "tone": tone,
    }
