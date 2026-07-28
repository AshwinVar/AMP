"""GET /customer-orders and GET /purchase-orders survive a NULL count column.

CustomerOrder.dispatched_quantity and PurchaseOrder.received_quantity are declared
Column(Integer, default=0) WITHOUT nullable=False — the ORM default only fills a
value the *inserter* omitted, so a row written by raw SQL, a migration, or an update
that cleared the field can legitimately hold NULL. CustomerOrderResponse.dispatched_quantity
and PurchaseOrderResponse.received_quantity are typed non-optional int, so a single
such NULL row raised Pydantic ValidationError during response serialisation and 500-ed
the WHOLE list endpoint — one bad row hiding every good order, on endpoints the
order-book / procurement dashboards poll.

This is the exact response-serialisation NULL class already healed on the
production-plan / operator-execution lists (#375) and the GET /machines roster (#395):
the compute paths for these very columns already coalesce the NULL (the PATCH heals it
in-handler, the analytics rollup COALESCEs it, the CSV export reads `x or 0`), but the
raw-ORM list serializer was the one reader left un-healed. The response models now
coalesce a NULL to the column's own declared default of 0 on the way out; a real
recorded value — including a real 0 — is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_orders_list_null_count_safe.py   (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import orders_routes as OR
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


# ── CustomerOrder.dispatched_quantity ──────────────────────────────────────


def _seed_normal_order(db, tenant, dispatched):
    """A fully-populated order — proves real values (incl. a real 0) survive the heal."""
    tok = T.set_current_tenant(tenant)
    try:
        o = models.CustomerOrder(
            order_no="CO-OK", customer_name="Acme", product_name="Widget",
            order_quantity=10, dispatched_quantity=dispatched,
            due_date=date(2026, 1, 1), status="Partial",
        )
        db.add(o)
        db.commit()
        db.refresh(o)
    finally:
        T.reset_current_tenant(tok)
    return o


def _insert_null_dispatched_order(db, tenant):
    """A legacy row that left dispatched_quantity NULL (raw-SQL / migration)."""
    db.execute(
        text(
            "INSERT INTO customer_orders "
            "(tenant_code, order_no, customer_name, product_name, order_quantity, "
            " dispatched_quantity, priority, due_date, status) "
            "VALUES (:t, 'CO-NULL', 'Legacy', 'Gadget', 20, NULL, 'Medium', '2026-02-01', 'Pending')"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def test_customer_order_response_heals_null_dispatched():
    db = _iso_session()
    # Independent expected value: a real dispatched count of 7 must pass through as 7,
    # NOT be treated as a healed NULL — so the heal is not a tautology that zeroes
    # everything.
    _seed_normal_order(db, "DEFAULT", dispatched=7)
    _insert_null_dispatched_order(db, "DEFAULT")

    by_name = {o.order_no: schemas.CustomerOrderResponse.model_validate(o)
               for o in db.query(models.CustomerOrder).order_by(models.CustomerOrder.id).all()}

    assert set(by_name) == {"CO-OK", "CO-NULL"}, set(by_name)
    # The NULL row heals to the column's declared default of 0.
    assert by_name["CO-NULL"].dispatched_quantity == 0, by_name["CO-NULL"].dispatched_quantity
    # The real recorded value is untouched (independently derived: seeded 7 -> 7).
    assert by_name["CO-OK"].dispatched_quantity == 7, by_name["CO-OK"].dispatched_quantity
    print("PASS CustomerOrderResponse heals NULL dispatched_quantity, preserves a real 7")


def test_customer_order_response_preserves_real_zero():
    db = _iso_session()
    # A genuinely-recorded 0 (an un-dispatched order) must NOT be mistaken for a NULL —
    # both serialise to 0, but the heal only rewrites None, so this is the same value
    # either way; the point is it does not raise and the status logic still holds.
    _seed_normal_order(db, "DEFAULT", dispatched=0)
    o = db.query(models.CustomerOrder).filter_by(order_no="CO-OK").one()
    assert schemas.CustomerOrderResponse.model_validate(o).dispatched_quantity == 0
    print("PASS CustomerOrderResponse preserves a real recorded 0 dispatched_quantity")


def test_get_customer_orders_list_survives_a_null_row():
    db = _iso_session()
    _seed_normal_order(db, "DEFAULT", dispatched=7)
    _insert_null_dispatched_order(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = OR.get_customer_orders(db=db, current_user={"tenant": "DEFAULT"})
        # Mirror FastAPI's response_model=List[CustomerOrderResponse] serialisation —
        # the step that used to raise ValidationError on the NULL row.
        serialised = [schemas.CustomerOrderResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    names = {s.order_no for s in serialised}
    assert names == {"CO-OK", "CO-NULL"}, names
    for s in serialised:
        assert isinstance(s.dispatched_quantity, int)
    print("PASS GET /customer-orders returns the full book with a NULL row present (no 500)")


# ── PurchaseOrder.received_quantity ────────────────────────────────────────


def _seed_normal_po(db, tenant, received):
    tok = T.set_current_tenant(tenant)
    try:
        p = models.PurchaseOrder(
            po_no="PO-OK", supplier_id=1, item_name="Steel", order_quantity=100,
            received_quantity=received, unit="kg",
            expected_delivery_date=date(2026, 1, 1), status="Partial",
        )
        db.add(p)
        db.commit()
        db.refresh(p)
    finally:
        T.reset_current_tenant(tok)
    return p


def _insert_null_received_po(db, tenant):
    """A legacy PO that left received_quantity NULL. supplier_id is set to a real
    value so this row isolates the received_quantity heal (a NULL supplier_id is a
    separate FK concern, out of scope for this fix)."""
    db.execute(
        text(
            "INSERT INTO purchase_orders "
            "(tenant_code, po_no, supplier_id, item_name, order_quantity, "
            " received_quantity, unit, expected_delivery_date, status) "
            "VALUES (:t, 'PO-NULL', 2, 'Copper', 50, NULL, 'kg', '2026-02-01', 'Open')"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def test_purchase_order_response_heals_null_received():
    db = _iso_session()
    _seed_normal_po(db, "DEFAULT", received=42)
    _insert_null_received_po(db, "DEFAULT")

    by_no = {p.po_no: schemas.PurchaseOrderResponse.model_validate(p)
             for p in db.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id).all()}

    assert set(by_no) == {"PO-OK", "PO-NULL"}, set(by_no)
    assert by_no["PO-NULL"].received_quantity == 0, by_no["PO-NULL"].received_quantity
    # Independently derived: seeded 42 -> 42, not zeroed.
    assert by_no["PO-OK"].received_quantity == 42, by_no["PO-OK"].received_quantity
    print("PASS PurchaseOrderResponse heals NULL received_quantity, preserves a real 42")


def test_get_purchase_orders_list_survives_a_null_row():
    db = _iso_session()
    _seed_normal_po(db, "DEFAULT", received=42)
    _insert_null_received_po(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = OR.get_purchase_orders(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.PurchaseOrderResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    nos = {s.po_no for s in serialised}
    assert nos == {"PO-OK", "PO-NULL"}, nos
    for s in serialised:
        assert isinstance(s.received_quantity, int)
    print("PASS GET /purchase-orders returns the full book with a NULL row present (no 500)")


if __name__ == "__main__":
    test_customer_order_response_heals_null_dispatched()
    test_customer_order_response_preserves_real_zero()
    test_get_customer_orders_list_survives_a_null_row()
    test_purchase_order_response_heals_null_received()
    test_get_purchase_orders_list_survives_a_null_row()
    print("ALL ORDERS/PO LIST NULL-COUNT-SAFE TESTS PASSED")
