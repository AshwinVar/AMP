"""Supplier performance — who actually delivers (ADR-0007).

Purchasing shows the PO pipeline and the supplier directory, but nothing answers
the buyer's real question: *which suppliers deliver on time and in full, and which
are quietly starving the plant?* This read-model reframes purchase_orders (+ the
GRN receipts and the supplier directory) into a per-supplier scorecard over the
last 90 days:

  * Fill: of the units ordered, how many arrived (pooled per supplier; a PO's
    over-receipt is capped at its ordered quantity so over-delivery can't mask a
    short order elsewhere).
  * Overdue supply: open POs (not cancelled, not fully received) already past
    their expected date — the plant is waiting on these NOW.
  * On-time delivery: a PO is timed only when a GRN references it
    (purchase_order_ref -> po_no); receipt date = the FIRST such GRN's date.
    On-time = first receipt on or before expected_delivery_date. POs fully
    received but never referenced by a GRN can't be timed and are reported as
    ``untimed_deliveries`` — never guessed, never scored 0 days late (ADR-0007).

A read-model over purchase_orders / suppliers / goods_receipt_notes — all
auto-scoped to the tenant (ADR-0002; GRNs fail-safe per #245, so a legacy
unassigned GRN simply can't time a PO until backfilled — honest, not wrong).
Bounded in SQL: POs created in the window OR still open; GRNs in the window.
Adds no storage. Empty-safe: zeros / None, no divide-by-zero.

The severity / per-supplier parts reconcile: each supplier row's orders sum to
``orders_considered``, and overdue rows are a subset of open ones (rule 3,
asserted in the tests).
"""
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import or_

import models

name = "supplier_performance"

WINDOW_DAYS = 90       # procurement is a quarterly discipline
TOP_N = 8
OTD_TARGET = 90        # % on-time deliveries we call healthy
CANCELLED = ("cancelled", "canceled")
UNKNOWN_SUPPLIER = "Unknown supplier"


def _is_cancelled(status) -> bool:
    return (status or "").strip().lower() in CANCELLED


def _fulfilled(po) -> bool:
    return (po.received_quantity or 0) >= (po.order_quantity or 0) > 0


def build_supplier_performance(db, tenant: str) -> dict:
    """Per-supplier delivery scorecard over the last 90 days: fill rate (pooled,
    over-receipt capped), open + overdue supply, and a GRN-timed on-time rate with
    average days late — worst supplier first. Empty-safe; untimed deliveries are
    reported, never scored."""
    today = datetime.utcnow().date()
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

    # Bounded in SQL: POs raised in the window OR still awaiting stock (any age) —
    # an old open PO is still supply the plant is waiting on, so it stays in view.
    pos = (db.query(models.PurchaseOrder).filter(or_(
        models.PurchaseOrder.created_at >= cutoff,
        models.PurchaseOrder.received_quantity < models.PurchaseOrder.order_quantity,
    )).all())
    pos = [p for p in pos if not _is_cancelled(p.status)]

    supplier_names = {s.id: s.supplier_name for s in db.query(models.Supplier).all()}

    # First receipt date per PO number, from the GRNs that reference one. GRNs are
    # tenant-scoped (fail-safe): an unassigned legacy GRN is hidden, so it simply
    # can't time a PO until backfilled — the PO reads "untimed", never mis-timed.
    first_receipt: dict = {}
    grns = (db.query(models.GoodsReceiptNote)
            .filter(models.GoodsReceiptNote.created_at >= cutoff).all())
    for g in grns:
        ref = (g.purchase_order_ref or "").strip()
        if ref and g.created_at:
            d = g.created_at.date()
            if ref not in first_receipt or d < first_receipt[ref]:
                first_receipt[ref] = d

    agg: dict = defaultdict(lambda: {
        "orders": 0, "ordered_units": 0, "filled_units": 0,
        "open": 0, "overdue": 0, "fulfilled": 0,
        "on_time": 0, "late": 0, "days_late": [],
    })
    overdue_rows = []
    for p in pos:
        a = agg[p.supplier_id]
        ordered = p.order_quantity or 0
        received = p.received_quantity or 0
        a["orders"] += 1
        a["ordered_units"] += ordered
        # Over-receipt is capped per PO so one generous delivery can't mask a
        # short order elsewhere in the same supplier's pool.
        a["filled_units"] += min(received, ordered)

        if _fulfilled(p):
            a["fulfilled"] += 1
            receipt = first_receipt.get((p.po_no or "").strip())
            if receipt and p.expected_delivery_date:
                if receipt <= p.expected_delivery_date:
                    a["on_time"] += 1
                else:
                    a["late"] += 1
                    a["days_late"].append((receipt - p.expected_delivery_date).days)
            # else: fulfilled but no GRN reference -> untimed (counted below)
        else:
            a["open"] += 1
            if p.expected_delivery_date and p.expected_delivery_date < today:
                a["overdue"] += 1
                overdue_rows.append({
                    "po_no": p.po_no,
                    "supplier": supplier_names.get(p.supplier_id, UNKNOWN_SUPPLIER),
                    "item_name": p.item_name,
                    "ordered": ordered,
                    "received": received,
                    "unit": p.unit,
                    "expected": p.expected_delivery_date.isoformat(),
                    "days_overdue": (today - p.expected_delivery_date).days,
                })

    rows = []
    for sid, a in agg.items():
        timed = a["on_time"] + a["late"]
        otd = round(a["on_time"] / timed * 100) if timed else None
        fill = round(a["filled_units"] / a["ordered_units"] * 100) if a["ordered_units"] else None
        rows.append({
            "supplier_id": sid,
            "supplier": supplier_names.get(sid, UNKNOWN_SUPPLIER),
            "orders": a["orders"],
            "ordered_units": a["ordered_units"],
            "filled_units": a["filled_units"],
            "fill_rate": fill,
            "open": a["open"],
            "overdue": a["overdue"],
            "fulfilled": a["fulfilled"],
            "timed_deliveries": timed,
            "untimed_deliveries": a["fulfilled"] - timed,
            "on_time": a["on_time"],
            "late": a["late"],
            "otd_rate": otd,                     # None when nothing could be timed
            "avg_days_late": (round(sum(a["days_late"]) / len(a["days_late"]), 1)
                              if a["days_late"] else 0.0),
        })
    # Worst first: most overdue supply, then lowest on-time rate (unmeasured sorts
    # after a real bad number), then lowest fill.
    rows.sort(key=lambda r: (-r["overdue"],
                             r["otd_rate"] if r["otd_rate"] is not None else 101,
                             r["fill_rate"] if r["fill_rate"] is not None else 101))

    overdue_rows.sort(key=lambda r: -r["days_overdue"])
    orders_considered = sum(r["orders"] for r in rows)
    total_overdue = sum(r["overdue"] for r in rows)
    timed_total = sum(r["timed_deliveries"] for r in rows)
    on_time_total = sum(r["on_time"] for r in rows)
    plant_otd = round(on_time_total / timed_total * 100) if timed_total else None
    worst = rows[0] if rows else None

    if not pos:
        verdict, tone = "No purchase orders on record in the window.", "warn"
    elif total_overdue:
        w = (f" — worst: {worst['supplier']} ({worst['overdue']})"
             if worst and worst["overdue"] else "")
        verdict = (f"{total_overdue} PO{'s' if total_overdue != 1 else ''} overdue "
                   f"across {sum(1 for r in rows if r['overdue'])} supplier"
                   f"{'s' if sum(1 for r in rows if r['overdue']) != 1 else ''}{w}.")
        tone = "bad"
    elif plant_otd is not None and plant_otd < OTD_TARGET:
        verdict = f"On-time delivery at {plant_otd}% — below the {OTD_TARGET}% target."
        tone = "warn"
    elif plant_otd is not None:
        verdict = f"Suppliers delivering — {plant_otd}% on time, nothing overdue."
        tone = "good"
    else:
        verdict, tone = "Nothing overdue; no receipts could be timed yet.", "good"

    return {
        "days": WINDOW_DAYS,
        "otd_target": OTD_TARGET,
        "orders_considered": orders_considered,
        "suppliers": len(rows),
        "overdue": total_overdue,
        "timed_deliveries": timed_total,
        "plant_otd": plant_otd,                  # None when nothing timed
        "by_supplier": rows[:TOP_N],
        "overdue_pos": overdue_rows[:TOP_N],
        "worst": worst,
        "verdict": verdict,
        "tone": tone,
    }
