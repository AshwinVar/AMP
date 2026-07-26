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
from sqlalchemy import create_engine
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


if __name__ == "__main__":
    test_gmats_inventory_paths_registered_once_and_owned()
    test_proforma_listing_resolves_only_same_tenant_item_names()
    test_min_listing_resolves_only_same_tenant_item_names()
    test_listing_names_match_the_items_endpoint()
    test_invoice_rejects_over_issue_instead_of_clamping()
    test_invoice_then_void_is_stock_neutral()
    print("ALL GMATS-INVENTORY ROUTE TESTS PASSED")
