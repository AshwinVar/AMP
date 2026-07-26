"""Orders & procurement route registration test (ADR-0009).

The order-to-procurement CRUD (customer orders, suppliers, purchase orders,
their analytics, CSV export, escalation generation) lives in
orders_routes.register(app), peeled out of main.py. Guards registration +
ownership. (The CSV export helper is exercised by test_orders_export.py.)

Run:  python backend/test_orders_routes.py     (exit 0 = pass)
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
import models
import orders_routes
import schemas
import tenancy as T
from database import Base

EXPECTED = {
    "/customer-orders", "/customer-orders/export", "/customer-orders/{order_id}",
    "/analytics/customer-orders", "/customer-orders/generate-late-order-escalations",
    "/suppliers", "/suppliers/{supplier_id}",
    "/purchase-orders", "/purchase-orders/{po_id}", "/analytics/purchasing",
    "/purchase-orders/generate-overdue-escalations",
}


def test_procurement_paths_owned_by_orders_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"procurement paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"orders_routes"}}
    assert not wrong, f"procurement paths not owned solely by orders_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} procurement paths owned by orders_routes")


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _order(no):
    return schemas.CustomerOrderCreate(order_no=no, customer_name="C", product_name="P",
                                       order_quantity=10, dispatched_quantity=0,
                                       due_date=datetime.utcnow().date())  # UTC, matches the read-models


def _create_as(db, tenant, payload):
    tok = T.set_current_tenant(tenant)
    try:
        return orders_routes.create_customer_order(payload, db=db, current_user={"tenant": tenant})
    finally:
        T.reset_current_tenant(tok)


def test_duplicate_order_no_across_tenants_is_409_not_500():
    # order_no is globally unique, but the duplicate pre-check is tenant-scoped
    # (CustomerOrder is in SCOPED_MODELS). Tenant B reusing Tenant A's number
    # passes the check, then trips the DB constraint on commit — which must be a
    # clean 409 with a rolled-back session, not an unhandled IntegrityError -> 500.
    db = _iso_session()
    _create_as(db, "TA", _order("ORD-1"))
    try:
        _create_as(db, "TB", _order("ORD-1"))
        assert False, "cross-tenant duplicate order_no should raise, not succeed"
    except HTTPException as e:
        assert e.status_code == 409, e.status_code    # not 500, not a raw IntegrityError

    # the session was rolled back (not poisoned): a fresh insert still works
    order = _create_as(db, "TB", _order("ORD-2"))
    assert order.order_no == "ORD-2"
    tok = T.set_current_tenant("TB")
    assert {o.order_no for o in db.query(models.CustomerOrder).all()} == {"ORD-2"}  # scoped to TB
    T.reset_current_tenant(tok)
    print("PASS duplicate order_no across tenants -> 409, session survives (no 500, no poison)")


def test_same_tenant_duplicate_order_no_is_400():
    # Within one tenant the scoped pre-check catches it first -> the existing 400.
    db = _iso_session()
    _create_as(db, "TA", _order("ORD-9"))
    try:
        _create_as(db, "TA", _order("ORD-9"))
        assert False, "same-tenant duplicate should raise"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    print("PASS same-tenant duplicate order_no -> 400 (caught by the scoped pre-check)")


def _counting_session():
    """Isolated session whose engine counts every SQL statement it executes, so a
    test can assert the purchasing analytics no longer runs one query per PO."""
    from sqlalchemy import event

    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        counter["n"] += 1

    return sessionmaker(bind=engine)(), counter


def _supplier(code, name):
    return schemas.SupplierCreate(supplier_code=code, supplier_name=name)


def _po(no, supplier_id, order_qty, received_qty):
    from datetime import date, timedelta

    return schemas.PurchaseOrderCreate(
        po_no=no, supplier_id=supplier_id, item_name="Widget",
        order_quantity=order_qty, received_quantity=received_qty, unit="ea",
        expected_delivery_date=date.today() + timedelta(days=30),  # future -> never overdue
    )


def test_purchasing_analytics_supplier_pending_reconciled_and_no_n_plus_1():
    # The supplier-pending breakdown must resolve names from the suppliers already
    # loaded, not a SELECT per PO. This pins BOTH: (a) the numbers are unchanged
    # (independently-derived totals per supplier, incl. the "Supplier {id}"
    # fallback for a dangling supplier_id), and (b) the query count does NOT scale
    # with the number of purchase orders (the N+1 that this fixes).
    db, counter = _counting_session()
    tok = T.set_current_tenant("TA")
    try:
        s1 = orders_routes.create_supplier(_supplier("S1", "Acme"), db=db, current_user={"tenant": "TA"})
        s2 = orders_routes.create_supplier(_supplier("S2", "Globex"), db=db, current_user={"tenant": "TA"})

        # Acme: 100-30=70, 50-50=0, 10-10=0  -> 70 pending
        orders_routes.create_purchase_order(_po("PO-1", s1.id, 100, 30), db=db, current_user={"tenant": "TA"})
        orders_routes.create_purchase_order(_po("PO-2", s1.id, 50, 50), db=db, current_user={"tenant": "TA"})
        orders_routes.create_purchase_order(_po("PO-6", s1.id, 10, 10), db=db, current_user={"tenant": "TA"})
        # Globex: 40-0=40, 20-5=15  -> 55 pending
        orders_routes.create_purchase_order(_po("PO-3", s2.id, 40, 0), db=db, current_user={"tenant": "TA"})
        orders_routes.create_purchase_order(_po("PO-4", s2.id, 20, 5), db=db, current_user={"tenant": "TA"})

        # A PO whose supplier row does not exist (deleted supplier / bad ref):
        # inserted directly to bypass the create-time existence check, so the
        # fallback-label branch is exercised. 10-0=10 pending under "Supplier 9999".
        db.add(models.PurchaseOrder(
            po_no="PO-5", supplier_id=9999, item_name="Widget", order_quantity=10,
            received_quantity=0, unit="ea",
            expected_delivery_date=_po("x", 1, 1, 0).expected_delivery_date, status="Open",
        ))
        db.commit()

        counter["n"] = 0  # count only the analytics call
        result = orders_routes.get_purchasing_analytics(db=db, current_user={"tenant": "TA"})
        queries = counter["n"]
    finally:
        T.reset_current_tenant(tok)

    # Independently-derived expectations.
    assert result["supplier_pending"] == {"Acme": 70, "Globex": 55, "Supplier 9999": 10}, result["supplier_pending"]
    assert result["purchase_orders"] == 6, result["purchase_orders"]
    # ordered = 100+50+10+40+20+10 = 230 ; received = 30+50+10+0+5+0 = 95 -> round(95/230*100)=41
    assert result["ordered_qty"] == 230 and result["received_qty"] == 95, result
    assert result["receipt_rate"] == 41, result["receipt_rate"]

    # 6 purchase orders, but the analytics must not run ~6 supplier lookups. The
    # bounded query count (a small constant) is what the N+1 removal guarantees;
    # the old per-PO SELECT made this grow past the PO count.
    assert queries <= 3, f"purchasing analytics ran {queries} queries for 6 POs (N+1 regressed?)"
    print(f"PASS purchasing supplier_pending reconciled + bounded ({queries} queries for 6 POs)")


def test_purchasing_analytics_empty_book():
    # No suppliers, no POs: no crash, honest zeros, empty breakdown.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        result = orders_routes.get_purchasing_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)
    assert result["purchase_orders"] == 0
    assert result["receipt_rate"] == 0  # ordered_qty == 0 -> no divide-by-zero
    assert result["supplier_pending"] == {}
    print("PASS purchasing analytics on an empty order book -> honest zeros, no crash")


def test_purchasing_analytics_survives_null_received_quantity():
    # received_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # legacy row (raw SQL / migration / cleared update) can hold a true NULL. The
    # ORM default only fills an *omitted* value, so we force NULL with a raw UPDATE
    # (create-then-null), the same "slips in via raw SQL" path the fix targets.
    # Pre-fix the summary did sum(int + None) and max(int - None) and 500'd; NULL
    # must now read as the column's own default of 0.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        s1 = orders_routes.create_supplier(_supplier("S1", "Acme"), db=db, current_user={"tenant": "TA"})
        # ordered 100, received 30 (clean); ordered 40, received -> NULL.
        po_a = orders_routes.create_purchase_order(_po("PO-A", s1.id, 100, 30), db=db, current_user={"tenant": "TA"})
        po_b = orders_routes.create_purchase_order(_po("PO-B", s1.id, 40, 0), db=db, current_user={"tenant": "TA"})
        db.execute(text("UPDATE purchase_orders SET received_quantity = NULL WHERE id = :i"), {"i": po_b.id})
        db.commit()
        db.expire_all()  # force the analytics query to reload the true NULL from the DB

        result = orders_routes.get_purchasing_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)

    # Independently-derived: ordered = 100+40 = 140; received = 30 + 0(NULL->0) = 30.
    assert result["ordered_qty"] == 140, result["ordered_qty"]
    assert result["received_qty"] == 30, result["received_qty"]
    # receipt_rate = round(30/140*100) = round(21.43) = 21 (NOT a crash, NOT dropping the NULL PO).
    assert result["receipt_rate"] == 21, result["receipt_rate"]
    # supplier_pending under Acme: (100-30) + (40 - NULL->0) = 70 + 40 = 110.
    assert result["supplier_pending"] == {"Acme": 110}, result["supplier_pending"]
    print("PASS purchasing analytics: NULL received_quantity -> 0, totals reconcile (140/30/21%, pending 110)")


def _co(no, order_qty, dispatched_qty):
    from datetime import date, timedelta

    return schemas.CustomerOrderCreate(
        order_no=no, customer_name="Acme", product_name="Widget",
        order_quantity=order_qty, dispatched_quantity=dispatched_qty,
        due_date=date.today() + timedelta(days=30),  # future -> never late
    )


def test_customer_order_analytics_survives_null_dispatched_quantity():
    # dispatched_quantity is Column(Integer, default=0) WITHOUT nullable=False —
    # same NULL exposure as received_quantity. Force a true NULL with a raw UPDATE
    # and assert the dispatch summary treats it as 0 rather than 500'ing.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        co_a = orders_routes.create_customer_order(_co("CO-A", 100, 40), db=db, current_user={"tenant": "TA"})
        co_b = orders_routes.create_customer_order(_co("CO-B", 50, 0), db=db, current_user={"tenant": "TA"})
        db.execute(text("UPDATE customer_orders SET dispatched_quantity = NULL WHERE id = :i"), {"i": co_b.id})
        db.commit()
        db.expire_all()

        result = orders_routes.get_customer_order_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)

    # Independently-derived: order = 100+50 = 150; dispatched = 40 + 0(NULL->0) = 40.
    assert result["total_order_qty"] == 150, result["total_order_qty"]
    assert result["total_dispatched_qty"] == 40, result["total_dispatched_qty"]
    # dispatch_rate = round(40/150*100) = round(26.67) = 27.
    assert result["dispatch_rate"] == 27, result["dispatch_rate"]
    assert result["total_orders"] == 2, result["total_orders"]
    print("PASS customer-order analytics: NULL dispatched_quantity -> 0, totals reconcile (150/40/27%)")


def _co_full(no, customer, qty, dispatched, status, priority, due_offset_days):
    from datetime import timedelta

    return schemas.CustomerOrderCreate(
        order_no=no, customer_name=customer, product_name="Widget",
        order_quantity=qty, dispatched_quantity=dispatched,
        status=status, priority=priority,
        due_date=datetime.utcnow().date() + timedelta(days=due_offset_days),
    )


def test_customer_order_analytics_buckets_and_reconciles():
    # A mixed book with past-due / future / dispatched / cancelled orders across
    # three customers and three priorities. Every expected number below is derived
    # by hand from the fixture, not read back from the endpoint.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        # no, customer, qty, dispatched, status, priority, due_offset.
        # NOTE: create_customer_order auto-derives status from dispatched vs order
        # qty (dispatched>=qty -> Dispatched, dispatched>0 -> Partial), so the
        # status-controlled rows below carry dispatched=0.
        orders_routes.create_customer_order(_co_full("CO-1", "Acme", 100, 0, "Pending", "High", -5), db=db, current_user={"tenant": "TA"})
        orders_routes.create_customer_order(_co_full("CO-2", "Acme", 40, 40, "Pending", "Medium", -10), db=db, current_user={"tenant": "TA"})  # -> Dispatched
        orders_routes.create_customer_order(_co_full("CO-3", "Beta", 30, 12, "Pending", "Low", 5), db=db, current_user={"tenant": "TA"})  # -> Partial
        orders_routes.create_customer_order(_co_full("CO-4", "Beta", 20, 0, "Cancelled", "Medium", -3), db=db, current_user={"tenant": "TA"})
        orders_routes.create_customer_order(_co_full("CO-5", "Gamma", 50, 0, "Pending", "High", -1), db=db, current_user={"tenant": "TA"})

        result = orders_routes.get_customer_order_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)

    assert result["total_orders"] == 5, result["total_orders"]
    assert result["pending"] == 2, result["pending"]      # CO-1, CO-5
    assert result["partial"] == 1, result["partial"]      # CO-3 (12/30 dispatched)
    assert result["dispatched"] == 1, result["dispatched"]  # CO-2 (40/40 dispatched)
    assert result["cancelled"] == 1, result["cancelled"]  # CO-4
    # late = past due AND not Dispatched/Cancelled -> CO-1, CO-5. CO-2/CO-4 are
    # past due but excluded by status; CO-3 is future.
    assert result["late"] == 2, result["late"]
    # order qty = 100+40+30+20+50 = 240; dispatched = 0+40+12+0+0 = 52.
    assert result["total_order_qty"] == 240, result["total_order_qty"]
    assert result["total_dispatched_qty"] == 52, result["total_dispatched_qty"]
    # dispatch_rate = round(52/240*100) = round(21.67) = 22.
    assert result["dispatch_rate"] == 22, result["dispatch_rate"]
    assert result["priority_counts"] == {"High": 2, "Medium": 2, "Low": 1}, result["priority_counts"]
    assert result["customer_counts"] == {"Acme": 140, "Beta": 50, "Gamma": 50}, result["customer_counts"]

    # Reconcile denominators (rule-3): the status buckets partition the book, and
    # both the per-priority and per-customer breakdowns sum back to the headlines.
    assert result["pending"] + result["partial"] + result["dispatched"] + result["cancelled"] == result["total_orders"]
    assert sum(result["priority_counts"].values()) == result["total_orders"]
    assert sum(result["customer_counts"].values()) == result["total_order_qty"]
    print("PASS customer-order analytics: SQL buckets reconcile (5 orders, late=2, 22% dispatch)")


def test_customer_order_analytics_empty_book():
    # Empty table: COALESCE(SUM(..),0) must not surface a NULL, dispatch_rate must
    # guard the zero divisor, and the group-by breakdowns are empty dicts.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        result = orders_routes.get_customer_order_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)

    assert result["total_orders"] == 0, result["total_orders"]
    assert result["total_order_qty"] == 0, result["total_order_qty"]
    assert result["total_dispatched_qty"] == 0, result["total_dispatched_qty"]
    assert result["dispatch_rate"] == 0, result["dispatch_rate"]
    assert result["late"] == 0, result["late"]
    assert result["priority_counts"] == {}, result["priority_counts"]
    assert result["customer_counts"] == {}, result["customer_counts"]
    print("PASS customer-order analytics: empty book returns zeroes, no NULL, no divide-by-zero")


def test_customer_order_analytics_null_status_overdue_counts_as_late():
    # status is String default="Pending" (not NOT NULL). SQL's `status NOT IN (...)`
    # is NULL — not TRUE — for a NULL status, which would silently drop an overdue
    # NULL-status order from the late count. The OR status IS NULL branch keeps the
    # old Python `None not in [...]` semantics, so the overdue unknown is still late.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        co = orders_routes.create_customer_order(_co_full("CO-N", "Acme", 10, 0, "Pending", "Medium", -2), db=db, current_user={"tenant": "TA"})
        db.execute(text("UPDATE customer_orders SET status = NULL WHERE id = :i"), {"i": co.id})
        db.commit()
        db.expire_all()

        result = orders_routes.get_customer_order_analytics(db=db, current_user={"tenant": "TA"})
    finally:
        T.reset_current_tenant(tok)

    assert result["total_orders"] == 1, result["total_orders"]
    # NULL status is none of Pending/Partial/Dispatched/Cancelled...
    assert result["pending"] == 0, result["pending"]
    # ...but the order is past due and not dispatched/cancelled, so it IS late.
    assert result["late"] == 1, result["late"]
    print("PASS customer-order analytics: NULL-status overdue order still counts as late")


def test_late_order_escalation_generator_reconciles_with_late_headline():
    # The /analytics/customer-orders headline counts a NULL-status overdue order as
    # late (see the test above). The generate-late-order-escalations generator must
    # use the SAME basis, or the two disagree — the headline says "2 late" while the
    # generator raises fewer escalations. Before the fix its `status NOT IN (...)`
    # evaluated to NULL (not TRUE) for a NULL status, silently dropping the very same
    # overdue order the headline still counts. Fixture is hand-derived: two overdue
    # orders that ARE late (one Pending, one forced to NULL) and one dispatched-overdue
    # that is NOT late -> late basis == 2, so exactly 2 escalations must be created.
    db = _iso_session()
    tok = T.set_current_tenant("TA")
    try:
        orders_routes.create_customer_order(_co_full("CO-P", "Acme", 10, 0, "Pending", "High", -3), db=db, current_user={"tenant": "TA"})
        n = orders_routes.create_customer_order(_co_full("CO-NULL", "Beta", 10, 0, "Pending", "Medium", -2), db=db, current_user={"tenant": "TA"})
        # dispatched >= qty -> create_customer_order auto-derives "Dispatched": overdue
        # but NOT late, so it must not produce an escalation.
        orders_routes.create_customer_order(_co_full("CO-D", "Gamma", 10, 10, "Pending", "Low", -5), db=db, current_user={"tenant": "TA"})
        db.execute(text("UPDATE customer_orders SET status = NULL WHERE id = :i"), {"i": n.id})
        db.commit()
        db.expire_all()

        # Headline basis: 2 late (Pending overdue + NULL overdue; the dispatched one is out).
        analytics = orders_routes.get_customer_order_analytics(db=db, current_user={"tenant": "TA"})
        assert analytics["late"] == 2, analytics["late"]

        # Generator must match that basis exactly: 2 escalations, one for the NULL row.
        created = orders_routes.generate_late_order_escalations(db=db, current_user={"tenant": "TA"})["created"]
        assert created == analytics["late"] == 2, (created, analytics["late"])
        titles = {e.title for e in db.query(models.Escalation).all()}
        assert "Late customer order: CO-NULL" in titles, titles   # the NULL-status row is escalated
        assert "Late customer order: CO-P" in titles, titles
        assert "Late customer order: CO-D" not in titles, titles  # dispatched -> not late
    finally:
        T.reset_current_tenant(tok)
    print("PASS late-order escalation generator reconciles with the late headline (incl. NULL status)")


if __name__ == "__main__":
    test_procurement_paths_owned_by_orders_routes()
    test_duplicate_order_no_across_tenants_is_409_not_500()
    test_same_tenant_duplicate_order_no_is_400()
    test_purchasing_analytics_supplier_pending_reconciled_and_no_n_plus_1()
    test_purchasing_analytics_empty_book()
    test_purchasing_analytics_survives_null_received_quantity()
    test_customer_order_analytics_survives_null_dispatched_quantity()
    test_customer_order_analytics_buckets_and_reconciles()
    test_customer_order_analytics_empty_book()
    test_customer_order_analytics_null_status_overdue_counts_as_late()
    test_late_order_escalation_generator_reconciles_with_late_headline()
    print("ALL ORDERS ROUTE TESTS PASSED")
