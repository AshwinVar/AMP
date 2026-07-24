"""Inventory record accuracy — do my stock records match the shelf? (ADR-0007).

Every other inventory read-model trusts ``current_stock`` — ``ai.inventory`` and
``ai.coverage`` decide what to reorder from it, ``ai.stock_health`` decides what's
dead or overstocked from it. This one asks the prior question: *is that number
right?* It reframes the cycle-count log (``cycle_counts`` + ``cycle_count_items``,
where a counter records the physical count against the book quantity) into the
inventory-record-accuracy (IRA) picture a stores manager actually manages by:

  * ACCURACY  — of the items counted in the window, the share whose book quantity
                matched the physical count exactly (IRA%).
  * COVERAGE  — of the whole item master, the share that has been cycle-counted at
                all in the window (a count programme that never touches 90% of the
                SKUs can't vouch for them).
  * VARIANCE  — the net (signed) and absolute unit discrepancy the counts found,
                and the specific items whose records are furthest off — where a
                phantom stock figure is about to drive a bad reorder decision.

Each item is judged on its LATEST count in the window (its current record state);
an item counted twice is scored once, on the most recent count.

Honesty notes (ADR-0007):
  * Variance is recomputed as ``physical_qty - book_qty`` from the count line — the
    derivable source of truth — never read from the denormalised ``variance``
    column, which could drift (rule 1/2).
  * ``accuracy_rate`` is None (not a misleading 0% or 100%) when nothing has been
    counted in the window — no count, no accuracy to report (rule 2).
  * Everything is in UNITS and PERCENT. cycle_count_items carries no unit cost, so
    this NEVER puts a currency figure on a discrepancy — it would be invented
    (rule 2).
  * The per-direction counts (exact / over / under) sum to ``counted_items`` and
    the absolute variance sums to its per-item parts (asserted in the tests, rule 3).

Tenant scoping (ADR-0002): ``CycleCount`` / ``CycleCountItem`` are NOT in
``tenancy.SCOPED_MODELS`` (they carry no ``tenant_code``), so they are scoped
EXPLICITLY here — the count lines are filtered to the tenant's own inventory items
(``item_id`` of an auto-scoped ``InventoryItem``; ``item_id`` is a global unique
PK, so a line for another tenant's item can't match). The scan is bounded in SQL to
the window on ``created_at`` and to those item ids (both indexed via ``main.py``), so
the ever-growing count history isn't re-read on every poll (rule 4). It adds no
storage.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "stock_accuracy"

TOP_N = 10
# Cycle counting is a periodic, quarterly-cadence discipline — a quarter is the
# honest window over which "have we counted it, and was it right?" means something.
WINDOW_DAYS = 90
# IRA health bands (% of counted items that matched the book exactly). 95% is the
# common stores target; below 80% the records can't be trusted for planning.
ACCURACY_TARGET = 95
ACCURACY_FLOOR = 80
# Below this many counted items the accuracy rate swings on a single miss, so we
# report it but don't hang a verdict on it.
MIN_COUNTED = 5


def _variance(line) -> int:
    """The discrepancy a count found, recomputed from the line as physical - book —
    the derivable source of truth, not the stored ``variance`` column (rule 1)."""
    return (line.physical_qty or 0) - (line.book_qty or 0)


def build_stock_accuracy(db, tenant: str) -> dict:
    """Inventory record accuracy over the last 90 days of cycle counts: the IRA rate
    (share of counted items that matched the book exactly), the count-programme
    coverage (share of the item master counted at all), the net and absolute unit
    variance found, and the items whose records are furthest off. Scoped explicitly
    to the tenant's own inventory items (ADR-0002); the scan is bounded in SQL to the
    window and those items (rule 4). Empty-safe: zeros / None, no divide-by-zero, no
    invented currency (the schema has no unit cost)."""
    today = datetime.utcnow().date()
    cutoff = datetime.combine(today - timedelta(days=WINDOW_DAYS - 1), datetime.min.time())

    # The tenant's item master (auto-scoped, ADR-0002) — both the coverage
    # denominator and the id set that scopes the un-scoped count lines to this tenant.
    items = db.query(models.InventoryItem).all()
    item_by_id = {i.id: i for i in items}
    total_items = len(items)

    if not item_by_id:
        return _empty(total_items)

    # Count lines inside the window AND belonging to this tenant's items, bounded in
    # SQL (created_at + item_id are indexed). item_id is a global unique PK, so an
    # ".in_(this tenant's ids)" filter is leak-proof for the un-scoped table (rule 5).
    lines = (db.query(models.CycleCountItem)
             .filter(models.CycleCountItem.created_at >= cutoff,
                     models.CycleCountItem.item_id.in_(list(item_by_id.keys())))
             .all())

    # One current record-state per item: its LATEST count in the window (max
    # created_at, id as the deterministic tiebreak). An item counted twice is
    # scored once, on the most recent count.
    latest: dict = {}
    for ln in lines:
        if ln.item_id is None:
            continue
        key = (ln.created_at or datetime.min, ln.id or 0)
        cur = latest.get(ln.item_id)
        if cur is None or key > cur[0]:
            latest[ln.item_id] = (key, ln)

    # Count-no / status per referenced count header, for the worst-item rows.
    count_ids = {ln.count_id for _, ln in latest.values() if ln.count_id is not None}
    counts = ({c.id: c for c in db.query(models.CycleCount)
               .filter(models.CycleCount.id.in_(list(count_ids))).all()}
              if count_ids else {})

    counted_items = len(latest)
    exact = over = under = 0
    net_variance = abs_variance = 0
    rows = []
    for item_id, (_, ln) in latest.items():
        var = _variance(ln)
        net_variance += var
        abs_variance += abs(var)
        if var == 0:
            exact += 1
        elif var > 0:
            over += 1           # more on the shelf than the book said
        else:
            under += 1          # less on the shelf than the book said (the costlier miss)

        if var != 0:            # only the inaccurate items are worth surfacing
            it = item_by_id.get(item_id)
            hdr = counts.get(ln.count_id)
            rows.append({
                "item_code": it.item_code if it else f"#{item_id}",
                "item_name": it.item_name if it else f"#{item_id}",
                "category": it.category if it else None,
                "unit": it.unit if it else "",
                "book_qty": ln.book_qty or 0,
                "physical_qty": ln.physical_qty or 0,
                "variance": var,
                "abs_variance": abs(var),
                "direction": "over" if var > 0 else "under",
                "count_no": hdr.count_no if hdr else None,
                "count_status": hdr.status if hdr else None,
                "counted_at": ln.created_at.isoformat() if ln.created_at else None,
            })

    # Worst records first: biggest absolute discrepancy, then item code for stability.
    rows.sort(key=lambda r: (-r["abs_variance"], r["item_code"]))

    # IRA% — share of COUNTED items that matched the book exactly. None when nothing
    # was counted (no count, no accuracy — never a misleading 0/100%).
    accuracy_rate = round(exact / counted_items * 100) if counted_items else None
    # Programme coverage — share of the whole item master counted at all in the window.
    count_coverage = round(counted_items / total_items * 100) if total_items else 0
    counts_in_window = len(count_ids)
    worst = rows[0] if rows else None

    verdict, tone = _verdict(counted_items, accuracy_rate, count_coverage,
                             under, abs_variance)

    return {
        "window_days": WINDOW_DAYS,
        "target": ACCURACY_TARGET,
        "total_items": total_items,
        "counted_items": counted_items,
        # Coverage of the count programme (how much of the master it actually touched).
        "count_coverage": count_coverage,
        "counts_in_window": counts_in_window,
        # Accuracy split — exact / over / under sum to counted_items (rule 3).
        "accurate_items": exact,
        "inaccurate_items": counted_items - exact,
        "over_items": over,
        "under_items": under,
        "accuracy_rate": accuracy_rate,          # IRA%; None when nothing counted
        # Variance in units — net (signed) and absolute (magnitude). No currency: the
        # schema carries no unit cost, so pricing the discrepancy would be invented.
        "net_variance_units": net_variance,
        "abs_variance_units": abs_variance,
        "worst": worst,
        "items": rows[:TOP_N],                   # capped page; inaccurate_items is the true count
        "verdict": verdict,
        "tone": tone,
    }


def _verdict(counted, accuracy_rate, coverage, under_items, abs_variance):
    """A short, honest read on the count programme — accuracy first, then whether the
    programme has counted enough of the master to be believed."""
    if counted == 0:
        return "No cycle counts in the window — inventory accuracy is unmeasured.", "warn"
    if counted < MIN_COUNTED:
        return (f"Only {counted} item{'s' if counted != 1 else ''} counted in {WINDOW_DAYS} days — "
                f"too few to judge record accuracy."), "warn"
    if accuracy_rate >= ACCURACY_TARGET:
        tail = (f", but only {coverage}% of the item master has been counted"
                if coverage < 50 else "")
        return (f"{accuracy_rate}% of counted records matched the shelf{tail}."), \
               ("warn" if tail else "good")
    if accuracy_rate >= ACCURACY_FLOOR:
        return (f"{accuracy_rate}% record accuracy — below the {ACCURACY_TARGET}% target; "
                f"{abs_variance:,} units of discrepancy found."), "warn"
    return (f"Inventory records can't be trusted — {accuracy_rate}% accuracy, "
            f"{under_items} item{'s' if under_items != 1 else ''} short of book, "
            f"{abs_variance:,} units of discrepancy."), "bad"


def _empty(total_items: int) -> dict:
    """Zeroed shape for a tenant with no inventory (and therefore no counts)."""
    return {
        "window_days": WINDOW_DAYS,
        "target": ACCURACY_TARGET,
        "total_items": total_items,
        "counted_items": 0,
        "count_coverage": 0,
        "counts_in_window": 0,
        "accurate_items": 0,
        "inaccurate_items": 0,
        "over_items": 0,
        "under_items": 0,
        "accuracy_rate": None,
        "net_variance_units": 0,
        "abs_variance_units": 0,
        "worst": None,
        "items": [],
        "verdict": "No inventory on record.", "tone": "warn",
    }
