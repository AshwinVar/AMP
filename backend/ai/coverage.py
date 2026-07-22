"""Inventory days-of-cover — the forward-looking stockout forecast (ADR-0007).

Answers the question the reorder snapshot can't: "at the rate we're actually
burning it, when do we run out?" For every stocked item it measures the recent
consumption rate (outbound transactions over a 14-day window), turns current
stock into days of cover, and dates the projected stockout — so the worst items
surface *before* they cross a static reorder line, ranked by how soon they run
dry. A read-model over inventory_items + inventory_transactions — auto-scoped to
the tenant by the query layer (ADR-0002); it adds no storage.

Distinct from ai.inventory (which flags items already at/below their reorder
level): this one is rate-based and predictive, so it catches a fast-moving item
that's still above its reorder line but days from empty.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "coverage"

TOP_N = 10
WINDOW_DAYS = 14      # consumption look-back used to estimate the daily burn
CRITICAL_DAYS = 7     # runs dry within a week (or already empty) -> critical
WATCH_DAYS = 21       # runs dry within three weeks -> worth watching

# Outbound transaction types count as consumption. Two seed vocabularies exist in
# the codebase (routes issue "Issue"; the simulator issues "OUT"), so match both,
# case-insensitively. Inbound ("IN"/"Receive"/"Return") and stock corrections
# ("Adjust"/"Adjustment") are not consumption and don't move the burn rate.
_OUTBOUND = {"out", "issue"}


def _is_outbound(txn) -> bool:
    return (txn.transaction_type or "").strip().lower() in _OUTBOUND


def _state(stock: int, days_of_cover):
    """Classify an item by how soon it runs dry."""
    if stock <= 0:
        return "out"
    if days_of_cover is None:
        return "ok"                       # has stock, no recent burn -> not a runway risk
    if days_of_cover <= CRITICAL_DAYS:
        return "critical"
    if days_of_cover <= WATCH_DAYS:
        return "watch"
    return "ok"


_STATE_RANK = {"out": 0, "critical": 1, "watch": 2, "ok": 3}


def build_coverage_summary(db, tenant: str) -> dict:
    """Days-of-cover across the item master: how many items are already out or
    projected to run dry within a week, plus the specific items to reorder first
    (soonest stockout first). inventory_items + inventory_transactions are
    auto-scoped (ADR-0002); the transaction scan is bounded to the window."""
    today = datetime.utcnow().date()
    cutoff = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())

    items = db.query(models.InventoryItem).all()

    # Recent outbound quantity per item, bounded in SQL (the table grows forever).
    burned: dict = defaultdict(int)
    txns = (db.query(models.InventoryTransaction)
            .filter(models.InventoryTransaction.created_at >= cutoff).all())
    for t in txns:
        if t.item_id is not None and _is_outbound(t):
            burned[t.item_id] += t.quantity or 0

    rows = []
    counts = {"out": 0, "critical": 0, "watch": 0, "ok": 0}
    for i in items:
        stock = i.current_stock or 0
        daily_burn = burned.get(i.id, 0) / WINDOW_DAYS      # units/day over the window
        if stock <= 0:
            days_of_cover = 0                                # already empty
        elif daily_burn > 0:
            days_of_cover = round(stock / daily_burn, 1)
        else:
            days_of_cover = None                             # stock but no recent consumption
        state = _state(stock, days_of_cover)
        counts[state] += 1

        # Only surface items with a real runway risk (out / critical / watch) in
        # the reorder list; healthy and dormant items are just tallied.
        if state != "ok":
            stockout_date = None
            if days_of_cover is not None:
                stockout_date = (today + timedelta(days=int(days_of_cover))).isoformat()
            rows.append({
                "item_code": i.item_code,
                "item_name": i.item_name,
                "current_stock": stock,
                "unit": i.unit,
                "supplier": i.supplier,
                "daily_burn": round(daily_burn, 2),
                "days_of_cover": days_of_cover,
                "stockout_date": stockout_date,
                "state": state,
            })

    # Soonest to run dry first: out, then by days-of-cover ascending, then bigger burn.
    rows.sort(key=lambda r: (_STATE_RANK[r["state"]],
                             r["days_of_cover"] if r["days_of_cover"] is not None else 1e9,
                             -r["daily_burn"]))

    return {
        "window_days": WINDOW_DAYS,
        "critical_days": CRITICAL_DAYS,
        "total_items": len(items),
        "out_of_stock": counts["out"],
        "critical": counts["critical"],
        "watch": counts["watch"],
        # The headline "reorder now" number: already out or projected dry within a week.
        "running_out": counts["out"] + counts["critical"],
        "items": rows[:TOP_N],
    }
