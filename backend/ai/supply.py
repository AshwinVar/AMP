"""Inbound supply outlook — will my materials arrive, and from whom (ADR-0007).

The delivery read-model looks outward at customer orders; this one looks inward
at the purchase orders that feed the line. It answers the SME plant owner's
supply-side question: "which inbound POs are received, on-track, at-risk or
overdue, and which suppliers keep me waiting?" It classifies every purchase
order by its receipt state from what's been received against what's due, rolls
that up per supplier (worst first), and lists the specific POs to chase — the
overdue inbound that quietly becomes tomorrow's stockout. A read-model over
purchase_orders + suppliers — auto-scoped to the tenant (ADR-0002); it adds no
storage.
"""
from datetime import datetime, timedelta

import models

name = "supply"

AT_RISK_DAYS = 3   # due within this many days and not yet received -> at risk
TOP_N = 10
# A PO counts as received once it's fully received or the status says so.
RECEIVED_STATUSES = {"received", "closed", "completed", "complete", "delivered"}
# A status that itself declares the PO late, regardless of the expected date.
LATE_STATUSES = {"overdue"}


def _pct(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def _state(po, today) -> str:
    """A single PO's receipt state. Received when the full quantity is in (or the
    status says so); otherwise late if past its expected date (or flagged
    overdue), at-risk if due soon, else on track."""
    ordered = po.order_quantity or 0
    received = po.received_quantity or 0
    status = (po.status or "").strip().lower()
    if status in RECEIVED_STATUSES or (ordered > 0 and received >= ordered):
        return "received"
    if status in LATE_STATUSES:
        return "late"
    due = po.expected_delivery_date
    if due is None:
        return "on_track"
    days = (due - today).days
    if days < 0:
        return "late"
    if days <= AT_RISK_DAYS:
        return "at_risk"
    return "on_track"


def build_supply_summary(db, tenant: str) -> dict:
    """Inbound supply outlook across the PO book: plant-wide state counts and
    unit receipt rate, a per-supplier breakdown (worst first), and the specific
    at-risk/late POs to chase. purchase_orders and suppliers are auto-scoped
    (ADR-0002)."""
    today = datetime.utcnow().date()
    pos = db.query(models.PurchaseOrder).all()
    supplier_names = {s.id: s.supplier_name for s in db.query(models.Supplier).all()}

    totals = {"received": 0, "on_track": 0, "at_risk": 0, "late": 0}
    ordered_units = received_units = 0
    per_supplier: dict = {}
    chase = []

    for p in pos:
        state = _state(p, today)
        totals[state] += 1
        ordered = p.order_quantity or 0
        received = p.received_quantity or 0
        ordered_units += ordered
        received_units += received

        name_ = supplier_names.get(p.supplier_id, "—")
        s = per_supplier.setdefault(name_, {
            "supplier": name_, "pos": 0,
            "received": 0, "on_track": 0, "at_risk": 0, "late": 0,
            "ordered": 0, "received_units": 0,
        })
        s["pos"] += 1
        s[state] += 1
        s["ordered"] += ordered
        s["received_units"] += received

        if state in ("late", "at_risk"):
            due = p.expected_delivery_date
            chase.append({
                "po_no": p.po_no,
                "supplier": name_,
                "item_name": p.item_name,
                "expected_delivery_date": due.isoformat() if due else None,
                "order_quantity": ordered,
                "received_quantity": received,
                "unit": p.unit,
                "state": state,
                "days_to_due": (due - today).days if due else None,
            })

    # Upcoming inbound load: not-yet-received POs expected on each of the next 7 days.
    upcoming_days = [today + timedelta(days=i) for i in range(7)]
    due_set = set(upcoming_days)
    due_count = {d: 0 for d in upcoming_days}
    for p in pos:
        if p.expected_delivery_date in due_set and _state(p, today) != "received":
            due_count[p.expected_delivery_date] += 1
    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]

    by_supplier = [{**s, "receipt_rate": _pct(s["received_units"], s["ordered"])} for s in per_supplier.values()]
    # worst first: most late, then most at-risk, then lowest receipt rate
    by_supplier.sort(key=lambda s: (s["late"], s["at_risk"], -s["receipt_rate"]), reverse=True)

    # chase list: late (most overdue) first, then at-risk (soonest due)
    chase.sort(key=lambda o: (0 if o["state"] == "late" else 1,
                              o["days_to_due"] if o["days_to_due"] is not None else 9999))

    return {
        "total": len(pos),
        "received": totals["received"],
        "on_track": totals["on_track"],
        "at_risk": totals["at_risk"],
        "late": totals["late"],
        "receipt_rate": _pct(received_units, ordered_units),
        "by_supplier": by_supplier,
        "chase": chase[:TOP_N],
        "upcoming": upcoming,
    }


def build_supplier_detail(db, tenant: str, supplier: str) -> dict:
    """Drill-down for a single supplier (by name, as keyed in the summary): its
    unit receipt rate and delivery reliability, the state mix across its POs, an
    inbound timeline for the next 7 days, the POs to chase (late first), and its
    recent POs. Composes purchase_orders + suppliers (auto-scoped, ADR-0002);
    adds no storage. Returns a zeroed shape when the supplier has no POs."""
    today = datetime.utcnow().date()
    suppliers = db.query(models.Supplier).all()
    name_by_id = {s.id: s.supplier_name for s in suppliers}
    # A PO belongs to this supplier when its supplier row's name matches (the
    # summary keys per-supplier by name); "—" catches POs whose row is missing.
    pos = [p for p in db.query(models.PurchaseOrder).all()
           if name_by_id.get(p.supplier_id, "—") == supplier]
    meta = next((s for s in suppliers if s.supplier_name == supplier), None)

    totals = {"received": 0, "on_track": 0, "at_risk": 0, "late": 0}
    ordered_units = received_units = overdue_units = 0
    chase = []
    for p in pos:
        state = _state(p, today)
        totals[state] += 1
        ordered = p.order_quantity or 0
        received = p.received_quantity or 0
        ordered_units += ordered
        received_units += received
        if state == "late":
            overdue_units += max(0, ordered - received)   # units still owed on overdue POs
        if state in ("late", "at_risk"):
            due = p.expected_delivery_date
            chase.append({
                "po_no": p.po_no,
                "item_name": p.item_name,
                "expected_delivery_date": due.isoformat() if due else None,
                "order_quantity": ordered,
                "received_quantity": received,
                "unit": p.unit,
                "state": state,
                "days_to_due": (due - today).days if due else None,
            })

    # chase list: late (most overdue) first, then at-risk (soonest due)
    chase.sort(key=lambda o: (0 if o["state"] == "late" else 1,
                              o["days_to_due"] if o["days_to_due"] is not None else 9999))

    # Upcoming inbound load for this supplier: not-yet-received POs due on each
    # of the next 7 days.
    upcoming_days = [today + timedelta(days=i) for i in range(7)]
    due_set = set(upcoming_days)
    due_count = {d: 0 for d in upcoming_days}
    for p in pos:
        if p.expected_delivery_date in due_set and _state(p, today) != "received":
            due_count[p.expected_delivery_date] += 1
    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]

    # Reliability: of the POs already due (delivered in full, or overdue and
    # still short), the share delivered in full — the "can I count on them?"
    # number. On-track / at-risk POs aren't due yet, so they're held out.
    resolved = totals["received"] + totals["late"]

    # Recent POs (newest first) for context, with each PO's current state.
    recent = sorted(pos, key=lambda p: (p.created_at or datetime.min, p.id), reverse=True)[:10]
    recent_pos = [{
        "po_no": p.po_no,
        "item_name": p.item_name,
        "order_quantity": p.order_quantity or 0,
        "received_quantity": p.received_quantity or 0,
        "unit": p.unit,
        "expected_delivery_date": p.expected_delivery_date.isoformat() if p.expected_delivery_date else None,
        "state": _state(p, today),
    } for p in recent]

    return {
        "supplier": supplier,
        "category": meta.category if meta else None,
        "supplier_status": meta.status if meta else None,
        "total": len(pos),
        "received": totals["received"],
        "on_track": totals["on_track"],
        "at_risk": totals["at_risk"],
        "late": totals["late"],
        "receipt_rate": _pct(received_units, ordered_units),
        "reliability_rate": _pct(totals["received"], resolved),
        "ordered_units": ordered_units,
        "received_units": received_units,
        "overdue_units": overdue_units,
        "chase": chase[:TOP_N],
        "upcoming": upcoming,
        "recent": recent_pos,
    }
