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
# The single definition of a purchase order's receipt state (ai.supply owns it) —
# the part drill-down reuses it so "received" can't mean two different things.
from ai.supply import _state as _po_state

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
# Inbound receipts — they don't move the burn rate, but the part drill-down shows
# them alongside consumption so the daily movement reads as a balance.
_INBOUND = {"in", "receive", "receipt", "return"}


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


MAX_FORECAST_DAYS = 3650   # 10 years — past this a dated stockout is meaningless


def _days_of_cover(stock: int, daily_burn: float):
    """Stock / burn, rounded — the one definition shared by the summary and the
    part drill-down. None when there's stock but nothing is consuming it."""
    if stock <= 0:
        return 0                                     # already empty
    if daily_burn > 0:
        return round(stock / daily_burn, 1)
    return None                                      # stock but no recent consumption


def _stockout_date(today, days_of_cover):
    """The projected stockout date, or None when there isn't a meaningful one.

    A barely-moving part with healthy stock computes centuries of cover (one unit
    issued in the window against thousands on hand). Adding that to a date raises
    OverflowError — a 500 on the endpoint — and the date would be meaningless
    anyway, so anything past the forecast horizon reads as 'no dated stockout'."""
    if days_of_cover is None or days_of_cover > MAX_FORECAST_DAYS:
        return None
    return today + timedelta(days=int(days_of_cover))


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
        days_of_cover = _days_of_cover(stock, daily_burn)
        state = _state(stock, days_of_cover)
        counts[state] += 1

        # Only surface items with a real runway risk (out / critical / watch) in
        # the reorder list; healthy and dormant items are just tallied.
        if state != "ok":
            projected = _stockout_date(today, days_of_cover)
            stockout_date = projected.isoformat() if projected else None
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


def _empty_part(item_code: str) -> dict:
    """Zeroed shape for an item that isn't in the master — the drawer renders a
    'not found' state rather than the caller having to special-case a 404."""
    return {
        "found": False, "item_code": item_code, "item_name": item_code,
        "category": None, "supplier": None, "unit": "", "location": None,
        "current_stock": 0, "reorder_level": 0,
        "window_days": WINDOW_DAYS, "critical_days": CRITICAL_DAYS,
        "daily_burn": 0.0, "days_of_cover": None, "stockout_date": None, "state": "ok",
        "consumed_units": 0, "received_units": 0, "daily": [],
        "inbound": [], "inbound_units": 0, "next_arrival": None,
        "cover_verdict": "not_at_risk", "days_uncovered": None,
        "recent": [],
    }


def build_part_runway(db, tenant: str, item_code: str) -> dict:
    """Drill-down for one stocked part: why it is running out, and whether what's
    on order lands in time.

    The summary ranks parts by how soon they run dry; this answers the follow-up
    the buyer actually acts on. It reconciles the same burn rate and days-of-cover
    as the summary row, shows the daily in/out movement behind it, then reads the
    open purchase orders for the part and compares the earliest arrival with the
    projected stockout — so the verdict is "covered", "arrives too late" (with the
    days uncovered) or "nothing on order". Composes inventory_items +
    inventory_transactions + purchase_orders + suppliers (auto-scoped, ADR-0002);
    it adds no storage. Returns found:false for an unknown item code."""
    today = datetime.utcnow().date()
    cutoff = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())

    item = (db.query(models.InventoryItem)
            .filter(models.InventoryItem.item_code == item_code).first())
    if item is None:
        return _empty_part(item_code)

    txns = (db.query(models.InventoryTransaction)
            .filter(models.InventoryTransaction.item_id == item.id)
            .filter(models.InventoryTransaction.created_at >= cutoff).all())

    # Daily in/out movement over the window — the shape behind the burn rate.
    days = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    out_by_day: dict = defaultdict(int)
    in_by_day: dict = defaultdict(int)
    consumed = received = 0
    for t in txns:
        qty = t.quantity or 0
        day = (t.created_at or datetime.utcnow()).date()
        if _is_outbound(t):
            consumed += qty
            out_by_day[day] += qty
        elif (t.transaction_type or "").strip().lower() in _INBOUND:
            received += qty
            in_by_day[day] += qty
    daily = [{"date": d.isoformat(), "out": out_by_day.get(d, 0), "in": in_by_day.get(d, 0)}
             for d in days]

    stock = item.current_stock or 0
    daily_burn = consumed / WINDOW_DAYS
    days_of_cover = _days_of_cover(stock, daily_burn)
    state = _state(stock, days_of_cover)
    stockout = _stockout_date(today, days_of_cover)

    # What's on order for this part, and does it land before we run dry? POs are
    # matched by item_id; ai.supply owns the receipt-state definition.
    supplier_names = {s.id: s.supplier_name for s in db.query(models.Supplier).all()}
    pos = (db.query(models.PurchaseOrder)
           .filter(models.PurchaseOrder.item_id == item.id).all())
    inbound = []
    inbound_units = 0
    for p in pos:
        po_state = _po_state(p, today)
        if po_state == "received":
            continue                                  # already in stock, not future cover
        outstanding = max(0, (p.order_quantity or 0) - (p.received_quantity or 0))
        inbound_units += outstanding
        due = p.expected_delivery_date
        inbound.append({
            "po_no": p.po_no,
            "supplier": supplier_names.get(p.supplier_id, "—"),
            "order_quantity": p.order_quantity or 0,
            "received_quantity": p.received_quantity or 0,
            "outstanding": outstanding,
            "unit": p.unit,
            "expected_delivery_date": due.isoformat() if due else None,
            "days_to_due": (due - today).days if due else None,
            "state": po_state,
            # A late PO is already overdue, so treat it as arriving today at best.
            "arrives_before_stockout": None if (due is None or stockout is None)
                                       else max(due, today) <= stockout,
        })
    # Soonest expected first; POs with no due date sort last.
    inbound.sort(key=lambda o: (o["days_to_due"] is None, o["days_to_due"] or 0))

    # The verdict the buyer acts on.
    dated = [o for o in inbound if o["expected_delivery_date"]]
    next_arrival = dated[0]["expected_delivery_date"] if dated else None
    days_uncovered = None
    if state == "ok":
        verdict = "not_at_risk"
    elif not inbound:
        verdict = "no_inbound"
    elif stockout is None or not dated:
        verdict = "covered"                           # something is on order and no dated stockout
    else:
        # An overdue PO can't arrive before today, so clamp it to today.
        arrival = max(datetime.fromisoformat(dated[0]["expected_delivery_date"]).date(), today)
        if arrival <= stockout:
            verdict = "covered"
        else:
            verdict = "late_cover"
            days_uncovered = (arrival - stockout).days

    recent = sorted(txns, key=lambda t: (t.created_at or datetime.min, t.id), reverse=True)[:10]
    movements = [{
        "id": t.id,
        "transaction_type": t.transaction_type,
        "quantity": t.quantity or 0,
        "direction": "out" if _is_outbound(t) else "in" if (t.transaction_type or "").strip().lower() in _INBOUND else "adjust",
        "reference": t.reference,
        "at": t.created_at.isoformat() if t.created_at else None,
    } for t in recent]

    return {
        "found": True,
        "item_code": item.item_code,
        "item_name": item.item_name,
        "category": item.category,
        "supplier": item.supplier,
        "unit": item.unit,
        "location": item.location,
        "current_stock": stock,
        "reorder_level": item.reorder_level or 0,
        "window_days": WINDOW_DAYS,
        "critical_days": CRITICAL_DAYS,
        "daily_burn": round(daily_burn, 2),
        "days_of_cover": days_of_cover,
        "stockout_date": stockout.isoformat() if stockout else None,
        "state": state,
        "consumed_units": consumed,
        "received_units": received,
        "daily": daily,
        "inbound": inbound[:TOP_N],
        "inbound_units": inbound_units,
        "next_arrival": next_arrival,
        "cover_verdict": verdict,
        "days_uncovered": days_uncovered,
        "recent": movements,
    }
