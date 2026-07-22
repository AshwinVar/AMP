"""Order delivery outlook — will we deliver, and for whom (ADR-0007).

Answers the question an SME plant owner asks about the order book: "which
customer orders are on track, which are at risk, and which are already late?"
It classifies every customer order by its delivery state (delivered / on-track /
at-risk / late) from what's been dispatched against what's due, rolls that up
per customer (e.g. Bugatti vs Mercedes), and lists the specific orders that need
chasing. A read-model over customer_orders — auto-scoped to the tenant
(ADR-0002); it adds no storage.
"""
from datetime import datetime, timedelta

from sqlalchemy import or_

import models

name = "delivery"

AT_RISK_DAYS = 3   # due within this many days and not yet fulfilled -> at risk
TOP_N = 10
# A customer order counts as delivered once it's dispatched in full or marked done.
FULFILLED_STATUSES = {"delivered", "dispatched", "shipped", "completed", "closed"}


def _pct(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def _state(order, today) -> str:
    """A single order's delivery state. Fulfilled when dispatched in full (or the
    status says so); otherwise late if past due, at-risk if due soon, else on
    track."""
    ordered = order.order_quantity or 0
    dispatched = order.dispatched_quantity or 0
    if (order.status or "").strip().lower() in FULFILLED_STATUSES or (ordered > 0 and dispatched >= ordered):
        return "delivered"
    due = order.due_date
    if due is None:
        return "on_track"
    days = (due - today).days
    if days < 0:
        return "late"
    if days <= AT_RISK_DAYS:
        return "at_risk"
    return "on_track"


def build_delivery_summary(db, tenant: str) -> dict:
    """Delivery outlook across the order book: plant-wide state counts and unit
    fulfillment, a per-customer breakdown (worst first), and the specific
    at-risk/late orders to chase. customer_orders is auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    orders = db.query(models.CustomerOrder).all()

    totals = {"delivered": 0, "on_track": 0, "at_risk": 0, "late": 0}
    ordered_units = dispatched_units = units_at_risk = 0
    per_customer: dict = {}
    at_risk_orders = []

    for o in orders:
        state = _state(o, today)
        totals[state] += 1
        ordered = o.order_quantity or 0
        dispatched = o.dispatched_quantity or 0
        ordered_units += ordered
        dispatched_units += dispatched

        c = per_customer.setdefault(o.customer_name or "—", {
            "customer": o.customer_name or "—", "orders": 0,
            "delivered": 0, "on_track": 0, "at_risk": 0, "late": 0,
            "ordered": 0, "dispatched": 0,
        })
        c["orders"] += 1
        c[state] += 1
        c["ordered"] += ordered
        c["dispatched"] += dispatched

        if state in ("late", "at_risk"):
            units_at_risk += max(0, ordered - dispatched)   # undelivered units that may miss their date
            at_risk_orders.append({
                "order_no": o.order_no,
                "customer": o.customer_name,
                "product": o.product_name,
                "due_date": o.due_date.isoformat() if o.due_date else None,
                "order_quantity": ordered,
                "dispatched_quantity": dispatched,
                "state": state,
                "days_to_due": (o.due_date - today).days if o.due_date else None,
            })

    # Upcoming due load: not-yet-delivered orders due on each of the next 7 days.
    upcoming_days = [today + timedelta(days=i) for i in range(7)]
    due_set = set(upcoming_days)
    due_count = {d: 0 for d in upcoming_days}
    for o in orders:
        if o.due_date in due_set and _state(o, today) != "delivered":
            due_count[o.due_date] += 1
    upcoming = [{"date": d.isoformat(), "orders": due_count[d]} for d in upcoming_days]

    by_customer = [{**c, "fulfillment_rate": _pct(c["dispatched"], c["ordered"])} for c in per_customer.values()]
    # worst first: most late, then most at-risk, then lowest fulfillment
    by_customer.sort(key=lambda c: (c["late"], c["at_risk"], -c["fulfillment_rate"]), reverse=True)

    # chase list: late (most overdue) first, then at-risk (soonest due)
    at_risk_orders.sort(key=lambda o: (0 if o["state"] == "late" else 1,
                                       o["days_to_due"] if o["days_to_due"] is not None else 9999))

    return {
        "total": len(orders),
        "delivered": totals["delivered"],
        "on_track": totals["on_track"],
        "at_risk": totals["at_risk"],
        "late": totals["late"],
        # Share of the order book that's not late or at-risk — the headline "are we
        # keeping our promises?" number.
        "on_track_rate": _pct(totals["delivered"] + totals["on_track"], len(orders)),
        "fulfillment_rate": _pct(dispatched_units, ordered_units),
        "units_ordered": ordered_units,
        "units_dispatched": dispatched_units,
        "units_remaining": max(0, ordered_units - dispatched_units),
        # The volume actually in jeopardy — undelivered units on late/at-risk orders.
        "units_at_risk": units_at_risk,
        "by_customer": by_customer,
        "at_risk_orders": at_risk_orders[:TOP_N],
        "upcoming": upcoming,
    }


def build_customer_detail(db, tenant: str, customer: str) -> dict:
    """Drill-down for a single customer (by name, as keyed in the summary): its
    unit fulfillment and delivery reliability, the state mix across its orders,
    the due load for the next 7 days, the orders to chase (late first), and its
    recent orders. A read-model over customer_orders (auto-scoped, ADR-0002);
    adds no storage. Returns a zeroed shape when the customer has no orders."""
    today = datetime.utcnow().date()
    # The summary keys per-customer by name; filter to this customer in SQL rather
    # than scanning the whole order book in Python. "—" is the no-name bucket
    # (customer_name null or blank), matched with the same coalesce in SQL.
    if customer == "—":
        orders = (db.query(models.CustomerOrder)
                  .filter(or_(models.CustomerOrder.customer_name.is_(None),
                              models.CustomerOrder.customer_name == "",
                              models.CustomerOrder.customer_name == "—")).all())
    else:
        orders = (db.query(models.CustomerOrder)
                  .filter(models.CustomerOrder.customer_name == customer).all())

    totals = {"delivered": 0, "on_track": 0, "at_risk": 0, "late": 0}
    ordered_units = dispatched_units = overdue_units = 0
    chase = []
    for o in orders:
        state = _state(o, today)
        totals[state] += 1
        ordered = o.order_quantity or 0
        dispatched = o.dispatched_quantity or 0
        ordered_units += ordered
        dispatched_units += dispatched
        if state == "late":
            overdue_units += max(0, ordered - dispatched)   # units still owed on overdue orders
        if state in ("late", "at_risk"):
            due = o.due_date
            chase.append({
                "order_no": o.order_no,
                "product": o.product_name,
                "due_date": due.isoformat() if due else None,
                "order_quantity": ordered,
                "dispatched_quantity": dispatched,
                "state": state,
                "days_to_due": (due - today).days if due else None,
            })

    # chase list: late (most overdue) first, then at-risk (soonest due)
    chase.sort(key=lambda o: (0 if o["state"] == "late" else 1,
                              o["days_to_due"] if o["days_to_due"] is not None else 9999))

    # Upcoming due load for this customer: not-yet-delivered orders due on each
    # of the next 7 days.
    upcoming_days = [today + timedelta(days=i) for i in range(7)]
    due_set = set(upcoming_days)
    due_count = {d: 0 for d in upcoming_days}
    for o in orders:
        if o.due_date in due_set and _state(o, today) != "delivered":
            due_count[o.due_date] += 1
    upcoming = [{"date": d.isoformat(), "orders": due_count[d]} for d in upcoming_days]

    # Reliability: of the orders already due (delivered in full, or overdue and
    # still short), the share delivered in full — the "can I count on delivering
    # for them?" number. On-track / at-risk orders aren't due yet, so they're
    # held out.
    # NOTE ON SEMANTICS: a COMPLETION rate, not a punctuality one — "of the orders
    # that have reached their due date, how many are no longer outstanding?" An
    # order delivered three weeks late still counts as delivered. customer_orders
    # has no dispatch timestamp (only due_date), so a true on-time-delivery rate is
    # not computable — don't label this "on time".
    resolved = totals["delivered"] + totals["late"]

    # Recent orders (newest first) for context, with each order's current state.
    recent = sorted(orders, key=lambda o: (o.created_at or datetime.min, o.id), reverse=True)[:10]
    recent_orders = [{
        "order_no": o.order_no,
        "product": o.product_name,
        "order_quantity": o.order_quantity or 0,
        "dispatched_quantity": o.dispatched_quantity or 0,
        "due_date": o.due_date.isoformat() if o.due_date else None,
        "state": _state(o, today),
    } for o in recent]

    return {
        "customer": customer,
        "total": len(orders),
        "delivered": totals["delivered"],
        "on_track": totals["on_track"],
        "at_risk": totals["at_risk"],
        "late": totals["late"],
        "fulfillment_rate": _pct(dispatched_units, ordered_units),
        "reliability_rate": _pct(totals["delivered"], resolved),
        "ordered_units": ordered_units,
        "dispatched_units": dispatched_units,
        "overdue_units": overdue_units,
        "chase": chase[:TOP_N],
        "upcoming": upcoming,
        "recent": recent_orders,
    }
