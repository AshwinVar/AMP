"""GMATS-inventory route registration guard.

gmats_inventory_routes predates the ADR-0009 guard-test discipline. It owns the
GMATS tenant's enterprise-inventory surface — items (+ aliases / correct /
stock-in), the 4-bucket summary, resolve, MINs, proformas (cancel / invoice),
invoices, and CSV import. Assert every path is registered exactly once and owned
solely by the module.

Run:  python backend/test_gmats_inventory_routes.py     (exit 0 = pass)
"""
import main

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import gmats_inventory_routes as gmats
import models
from database import Base

_ADMIN = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}

EXPECTED = {
    "/gmats/items", "/gmats/items/{item_id}", "/gmats/items/{item_id}/aliases",
    "/gmats/items/{item_id}/correct", "/gmats/items/{item_id}/stock-in",
    "/gmats/summary", "/gmats/resolve", "/gmats/import-csv",
    "/gmats/min", "/gmats/min/{min_id}",
    "/gmats/proformas", "/gmats/proformas/{pid}/cancel", "/gmats/proformas/{pid}/invoice",
    "/gmats/invoices", "/gmats/invoices/{inv_id}",
}


def test_gmats_inventory_paths_registered_once_and_owned():
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"gmats-inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"gmats_inventory_routes"}}
    assert not wrong, f"paths not owned solely by gmats_inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} gmats-inventory paths owned by the module")


# --- Behavioural tests: the proforma / MIN listings resolve item names from the
# CURRENT tenant's items only. GmatsItem is not in tenancy.SCOPED_MODELS, so a
# prior `db.query(GmatsItem).all()` built the name-lookup from EVERY company's
# items — an unbounded cross-tenant scan that also leaked a foreign tenant's item
# name into this tenant's view whenever a line referenced a foreign item id (the
# create path resolves items by unscoped id while locking the parent's tenant). ---

# A client login is locked to its own tenant by _effective_tenant.
ACME = {"sub": "acme", "role": "Admin", "tenant": "ACME"}


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(db, iid, tenant, name):
    db.add(models.GmatsItem(id=iid, tenant_code=tenant, item_code=f"C{iid}",
                            item_name=name, physical_stock=10, reserved_stock=0,
                            reorder_level=0, purchase_rate=0))
    db.commit()


def test_proforma_listing_resolves_only_same_tenant_item_names():
    db = _db()
    _item(db, 1, "ACME", "Acme Widget")       # legit ACME item
    _item(db, 2, "GLOBEX", "Globex Secret")   # a DIFFERENT tenant's item
    p = models.GmatsProforma(id=1, tenant_code="ACME", proforma_no="PI-1001",
                             customer_name="Buyer", status="Open")
    db.add(p); db.commit()
    db.add(models.GmatsProformaLine(proforma_id=1, item_id=1, qty=3))   # own item
    db.add(models.GmatsProformaLine(proforma_id=1, item_id=2, qty=5))   # foreign id
    db.commit()

    out = gmats.gmats_proformas(tenant="ACME", db=db, current_user=ACME)
    assert len(out) == 1
    lines = {l["item_id"]: l["item_name"] for l in out[0]["lines"]}
    assert lines[1] == "Acme Widget", "own item name must resolve"
    # The foreign tenant's item name must NOT leak into ACME's proforma view.
    assert lines[2] == "", f"cross-tenant item name leaked: {lines[2]!r}"
    print("PASS proforma listing resolves own item names, never a foreign tenant's")


def test_min_listing_resolves_only_same_tenant_item_names():
    db = _db()
    _item(db, 1, "ACME", "Acme Spare")
    _item(db, 2, "GLOBEX", "Globex Spare")
    m = models.GmatsMIN(id=1, tenant_code="ACME", min_no="MIN-4001",
                        customer_name="Buyer", machine_ref="Rig", status="Issued")
    db.add(m); db.commit()
    db.add(models.GmatsMINLine(min_id=1, item_id=1, qty=1))
    db.add(models.GmatsMINLine(min_id=1, item_id=2, qty=1))
    db.commit()

    out = gmats.gmats_min_list(tenant="ACME", db=db, current_user=ACME)
    assert len(out) == 1
    lines = {l["item_id"]: l["item_name"] for l in out[0]["lines"]}
    assert lines[1] == "Acme Spare"
    assert lines[2] == "", f"cross-tenant item name leaked into MIN view: {lines[2]!r}"
    print("PASS MIN listing resolves own item names, never a foreign tenant's")


def test_listing_names_match_the_items_endpoint():
    # The name a proforma line shows must be the same one /gmats/items reports —
    # a reconciliation check that the scoped map is still complete for own items.
    db = _db()
    _item(db, 7, "ACME", "Reconciled Part")
    p = models.GmatsProforma(id=1, tenant_code="ACME", proforma_no="PI-1001",
                             customer_name="Buyer", status="Open")
    db.add(p); db.commit()
    db.add(models.GmatsProformaLine(proforma_id=1, item_id=7, qty=2)); db.commit()

    listed = {i["id"]: i["item_name"] for i in gmats.gmats_items(tenant="ACME", db=db, current_user=ACME)}
    pf = gmats.gmats_proformas(tenant="ACME", db=db, current_user=ACME)
    line = pf[0]["lines"][0]
    assert line["item_name"] == listed[line["item_id"]] == "Reconciled Part"
    print("PASS proforma line name reconciles with the /gmats/items name")


def _proforma_with_line(db, qty, physical):
    db.add(models.GmatsItem(id=1, tenant_code="GMATS", item_code="C1", item_name="Part 1",
                            physical_stock=physical, reserved_stock=qty, reorder_level=0,
                            purchase_rate=0, unit="ea"))
    db.add(models.GmatsProforma(id=1, tenant_code="GMATS", proforma_no="PI-1",
                                customer_name="Cust", status="Open"))
    db.add(models.GmatsProformaLine(id=1, proforma_id=1, item_id=1, qty=qty))
    db.commit()


def _stock(db):
    return db.query(models.GmatsItem).filter(models.GmatsItem.id == 1).first()


def test_invoice_rejects_over_issue_instead_of_clamping():
    # physical 3, invoice line 10: the old code clamped physical to 0 (deducting
    # only 3) and let the invoice through, so a later void restored the full 10 ->
    # +7 phantom stock. Now the over-issue is rejected and stock is untouched.
    db = _db()
    _proforma_with_line(db, qty=10, physical=3)
    try:
        gmats.gmats_generate_invoice(1, db=db, current_user=_ADMIN)
        assert False, "invoicing more than physical stock should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db).physical_stock == 3    # rejected invoice left stock untouched
    print("PASS invoice rejects over-issue (no silent clamp that void would over-restore)")


def test_invoice_then_void_is_stock_neutral():
    # With enough stock the invoice deducts exactly and the void restores exactly,
    # so physical returns to where it started — the deduction/restore are inverses.
    db = _db()
    _proforma_with_line(db, qty=4, physical=10)
    inv = gmats.gmats_generate_invoice(1, db=db, current_user=_ADMIN)
    assert _stock(db).physical_stock == 6 and _stock(db).reserved_stock == 0   # deducted 4, reservation cleared
    gmats.gmats_void_invoice(inv["id"], db=db, current_user=_ADMIN)
    assert _stock(db).physical_stock == 10   # restored EXACTLY 4, not more
    print("PASS invoice+void is stock-neutral (void is a true inverse of the deduction)")


# --- NULL-stock safety: physical_stock / reserved_stock / reorder_level are
# Column(Integer, default=0) WITHOUT nullable=False, so a raw-SQL / migration /
# blank-import write can store a true NULL. The shared _item_dict serializer and
# the summary rollup read them raw, so ONE such row 500'd the whole /gmats/items
# list and /gmats/summary card via `None - None` / `sum(None ...)`. The ORM default
# masks an explicit None on insert (it becomes 0), so these tests force a genuine
# NULL with a raw UPDATE — exactly the scenario the guard exists for. ---


def _null_stock(db, iid):
    db.execute(
        text(
            "UPDATE gmats_items SET physical_stock=NULL, reserved_stock=NULL, "
            "reorder_level=NULL, purchase_rate=NULL WHERE id=:id"
        ),
        {"id": iid},
    )
    db.commit()
    db.expire_all()


def test_item_dict_coalesces_null_stock_to_zero():
    # A single all-NULL row must serialize as a concrete 0-stock item, not 500.
    db = _db()
    _item(db, 1, "GMATS", "Blank Row")
    _null_stock(db, 1)
    out = gmats.gmats_items(tenant="GMATS", db=db, current_user=_ADMIN)
    assert len(out) == 1
    row = out[0]
    # Every nullable count is healed to the column's own default of 0 — never null.
    assert row["physical_stock"] == 0
    assert row["reserved_stock"] == 0
    assert row["reorder_level"] == 0
    assert row["purchase_rate"] == 0
    assert row["available_stock"] == 0            # 0 - 0, not None - None
    # reorder_needed is available <= reorder_level -> 0 <= 0 -> True.
    assert row["reorder_needed"] is True
    print("PASS _item_dict coalesces a fully-NULL stock row to 0 instead of 500")


def test_items_list_survives_one_null_row_among_healthy():
    # The high-impact bug: one NULL row must not poison the serializer for the
    # OTHER, healthy items in the same tenant. Healthy figures stay exact.
    db = _db()
    db.add(models.GmatsItem(id=1, tenant_code="GMATS", item_code="C1", item_name="Healthy",
                            physical_stock=100, reserved_stock=30, reorder_level=10,
                            purchase_rate=5, unit="ea"))
    _item(db, 2, "GMATS", "Blank Row")            # will be NULLed below
    db.commit()
    _null_stock(db, 2)

    out = {i["id"]: i for i in gmats.gmats_items(tenant="GMATS", db=db, current_user=_ADMIN)}
    assert len(out) == 2, "the NULL row must not drop or 500 the whole list"
    healthy = out[1]
    assert healthy["physical_stock"] == 100 and healthy["reserved_stock"] == 30
    assert healthy["available_stock"] == 70       # 100 - 30, independently derived
    assert healthy["reorder_needed"] is False     # 70 <= 10 is False
    assert out[2]["available_stock"] == 0
    print("PASS one NULL row doesn't poison the healthy rows in /gmats/items")


def test_summary_treats_null_as_zero_and_reconciles():
    # Independently-derived expected values over healthy + NULL rows:
    #   A: physical 100, reserved 30, reorder 10 -> available 70, not reorder_needed
    #   B: physical   5, reserved  0, reorder 20 -> available  5, reorder_needed
    #   C: all NULL -> coalesced 0/0/0          -> available  0, reorder_needed (0<=0)
    # total_physical = 100+5+0 = 105 ; total_reserved = 30 ; total_available = 75
    # reorder_needed count = B and C = 2 ; items = 3
    db = _db()
    db.add(models.GmatsItem(id=1, tenant_code="GMATS", item_code="A", item_name="A",
                            physical_stock=100, reserved_stock=30, reorder_level=10,
                            purchase_rate=0, unit="ea"))
    db.add(models.GmatsItem(id=2, tenant_code="GMATS", item_code="B", item_name="B",
                            physical_stock=5, reserved_stock=0, reorder_level=20,
                            purchase_rate=0, unit="ea"))
    _item(db, 3, "GMATS", "C")
    db.commit()
    _null_stock(db, 3)

    s = gmats.gmats_summary(tenant="GMATS", db=db, current_user=_ADMIN)
    assert s["items"] == 3
    assert s["total_physical"] == 105
    assert s["total_reserved"] == 30
    # Headline total_available reconciles with the per-item available_stock sum
    # (70 + 5 + 0) that /gmats/items reports — same physical-minus-reserved basis.
    assert s["total_available"] == 75
    assert s["total_available"] == s["total_physical"] - s["total_reserved"]
    assert s["reorder_needed"] == 2
    print("PASS summary coalesces NULL stock to 0 and reconciles the available total")


# --- NULL-stock safety on the WRITE paths. The read path above already heals a
# NULL physical/reserved column; the STOCK-MUTATION paths (stock-in, proforma,
# cancel, invoice, MIN, and their voids) each do integer arithmetic on those same
# columns — `physical += qty`, `physical - reserved`, `max(0, reserved - qty)`,
# `qty > physical` — and on a genuine NULL raised `int + None` / `None - int`
# TypeError, an unhandled 500. `_null_cols` forces a real NULL with a raw UPDATE
# (the ORM default masks an explicit None on insert), exactly the legacy-row
# scenario the guard exists for. Expected values are derived by hand. ---


def _null_cols(db, iid, *columns):
    assignments = ", ".join(f"{c}=NULL" for c in columns)
    db.execute(text(f"UPDATE gmats_items SET {assignments} WHERE id=:id"), {"id": iid})
    db.commit()
    db.expire_all()


def _proforma_status(db, pid):
    return db.query(models.GmatsProforma).filter(models.GmatsProforma.id == pid).first().status


def test_stock_in_heals_null_physical_before_adding():
    db = _db()
    _item(db, 1, "GMATS", "Part")                       # physical 10, reserved 0
    _null_cols(db, 1, "physical_stock", "reserved_stock")
    out = gmats.gmats_stock_in(1, {"qty": 5}, db=db, current_user=_ADMIN)
    # NULL physical heals to 0, then + 5 -> 5 (was a `None + 5` 500).
    assert out["physical_stock"] == 5
    assert out["reserved_stock"] == 0
    assert _stock(db).physical_stock == 5               # healed value persisted
    print("PASS stock-in heals a NULL physical_stock to 0 before adding (no 500)")


def test_create_proforma_reserves_against_null_reserved():
    db = _db()
    _item(db, 1, "GMATS", "Part")                       # physical 10 (real)
    _null_cols(db, 1, "reserved_stock")                 # reserved NULL only
    gmats.gmats_create_proforma(
        {"tenant": "GMATS", "customer_name": "Cust", "lines": [{"item_id": 1, "qty": 3}]},
        db=db, current_user=_ADMIN,
    )
    it = _stock(db)
    # available = 10 - 0(healed) = 10 >= 3, so reserve 3: reserved 0(healed) + 3 = 3.
    assert it.reserved_stock == 3                        # was a `None - None` / `None + 3` 500
    assert it.physical_stock == 10                       # a reservation never touches physical
    print("PASS proforma reserves against a NULL reserved_stock (heals to 0, no 500)")


def test_create_proforma_null_stock_reports_insufficient_not_500():
    db = _db()
    _item(db, 1, "GMATS", "Part")
    _null_cols(db, 1, "physical_stock", "reserved_stock")
    try:
        gmats.gmats_create_proforma(
            {"tenant": "GMATS", "customer_name": "Cust", "lines": [{"item_id": 1, "qty": 1}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "reserving 1 against 0 available should 400"
    except HTTPException as e:
        # An honest 'insufficient available' (available heals to 0), not a None-arithmetic 500.
        assert e.status_code == 400, e.status_code
    assert _stock(db).reserved_stock == 0
    print("PASS proforma against all-NULL stock returns a clean 400, not a 500")


def test_cancel_proforma_releases_null_reservation():
    db = _db()
    _proforma_with_line(db, qty=5, physical=10)         # reserved set to 5
    _null_cols(db, 1, "reserved_stock")                 # force NULL after setup
    gmats.gmats_cancel_proforma(1, db=db, current_user=_ADMIN)
    # reserved heals 0 then max(0, 0 - 5) = 0 (was a `None - 5` 500).
    assert _stock(db).reserved_stock == 0
    assert _proforma_status(db, 1) == "Cancelled"
    print("PASS cancel releases a NULL reservation to 0 without a 500")


def test_generate_invoice_survives_null_reserved():
    db = _db()
    _proforma_with_line(db, qty=4, physical=10)         # reserved 4
    _null_cols(db, 1, "reserved_stock")                 # force NULL after setup
    gmats.gmats_generate_invoice(1, db=db, current_user=_ADMIN)
    it = _stock(db)
    # physical 10 - 4 = 6; reserved heals 0 then max(0, 0 - 4) = 0 (was a `None - 4` 500).
    assert it.physical_stock == 6
    assert it.reserved_stock == 0
    print("PASS invoice deducts and clears a NULL reservation without a 500")


def test_create_min_null_physical_reports_insufficient_not_500():
    db = _db()
    _item(db, 1, "GMATS", "Spare")
    _null_cols(db, 1, "physical_stock")
    try:
        gmats.gmats_create_min(
            {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
             "lines": [{"item_id": 1, "qty": 2}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "issuing 2 from 0 physical should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db).physical_stock == 0               # healed to 0, untouched
    print("PASS MIN against a NULL physical returns a clean 400, not a 500")


def test_void_invoice_restores_onto_null_physical():
    db = _db()
    _proforma_with_line(db, qty=4, physical=10)
    inv = gmats.gmats_generate_invoice(1, db=db, current_user=_ADMIN)   # physical -> 6
    _null_cols(db, 1, "physical_stock")                 # force NULL before the void restore
    gmats.gmats_void_invoice(inv["id"], db=db, current_user=_ADMIN)
    # physical heals 0 then + 4 restored = 4 (was a `None + 4` 500).
    assert _stock(db).physical_stock == 4
    print("PASS void invoice restores onto a NULL physical without a 500")


def test_void_min_restores_onto_null_physical():
    db = _db()
    _item(db, 1, "GMATS", "Spare")                      # physical 10
    gmats.gmats_create_min(
        {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
         "lines": [{"item_id": 1, "qty": 3}]},
        db=db, current_user=_ADMIN,
    )                                                   # physical -> 7
    min_id = db.query(models.GmatsMIN).first().id
    _null_cols(db, 1, "physical_stock")                 # force NULL before the void restore
    gmats.gmats_void_min(min_id, db=db, current_user=_ADMIN)
    # physical heals 0 then + 3 restored = 3 (was a `None + 3` 500).
    assert _stock(db).physical_stock == 3
    print("PASS void MIN restores onto a NULL physical without a 500")


if __name__ == "__main__":
    test_gmats_inventory_paths_registered_once_and_owned()
    test_proforma_listing_resolves_only_same_tenant_item_names()
    test_min_listing_resolves_only_same_tenant_item_names()
    test_listing_names_match_the_items_endpoint()
    test_invoice_rejects_over_issue_instead_of_clamping()
    test_invoice_then_void_is_stock_neutral()
    test_item_dict_coalesces_null_stock_to_zero()
    test_items_list_survives_one_null_row_among_healthy()
    test_summary_treats_null_as_zero_and_reconciles()
    test_stock_in_heals_null_physical_before_adding()
    test_create_proforma_reserves_against_null_reserved()
    test_create_proforma_null_stock_reports_insufficient_not_500()
    test_cancel_proforma_releases_null_reservation()
    test_generate_invoice_survives_null_reserved()
    test_create_min_null_physical_reports_insufficient_not_500()
    test_void_invoice_restores_onto_null_physical()
    test_void_min_restores_onto_null_physical()
    print("ALL GMATS-INVENTORY ROUTE TESTS PASSED")
