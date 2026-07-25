"""Orders & procurement route registration test (ADR-0009).

The order-to-procurement CRUD (customer orders, suppliers, purchase orders,
their analytics, CSV export, escalation generation) lives in
orders_routes.register(app), peeled out of main.py. Guards registration +
ownership. (The CSV export helper is exercised by test_orders_export.py.)

Run:  python backend/test_orders_routes.py     (exit 0 = pass)
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine
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


if __name__ == "__main__":
    test_procurement_paths_owned_by_orders_routes()
    test_duplicate_order_no_across_tenants_is_409_not_500()
    test_same_tenant_duplicate_order_no_is_400()
    print("ALL ORDERS ROUTE TESTS PASSED")
