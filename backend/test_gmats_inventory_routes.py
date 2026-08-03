"""GMATS-inventory route registration guard.

gmats_inventory_routes predates the ADR-0009 guard-test discipline. It owns the
GMATS tenant's enterprise-inventory surface — items (+ aliases / correct /
stock-in), the 4-bucket summary, resolve, MINs, proformas (cancel / invoice),
invoices, and CSV import. Assert every path is registered exactly once and owned
solely by the module.

Run:  python backend/test_gmats_inventory_routes.py     (exit 0 = pass)
"""
import asyncio

import main

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import gmats_inventory_routes as gmats
import models
from database import Base
from payload_fields import MAX_QTY, int_cell

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


# --- Duplicate-line safety on the multi-line issue paths. A proforma/MIN may
# carry two lines for the SAME item. The availability check must validate the
# SUM of those lines, not each in isolation, or the cumulative reserve/deduct
# below over-commits stock past what's on the shelf. gmats_generate_invoice was
# already hardened + tested this way (test_invoice_rejects_over_issue_...); these
# pin the same guarantee on the proforma and MIN create paths. Expected values
# are derived by hand. ---


def _plain_item(db, iid, physical):
    db.add(models.GmatsItem(id=iid, tenant_code="GMATS", item_code=f"C{iid}",
                            item_name=f"Part {iid}", physical_stock=physical,
                            reserved_stock=0, reorder_level=0, purchase_rate=0, unit="ea"))
    db.commit()


def test_create_proforma_sums_duplicate_lines_and_rejects_over_reserve():
    # Item has 10 physical / 0 reserved. Two 8-unit lines for it total 16 > 10:
    # each line passed the OLD per-line check (both saw available=10), then the
    # reserve loop added 8+8=16, leaving reserved 16 > physical 10 (available -6).
    # The summed guard rejects the whole proforma and reserves nothing.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_proforma(
            {"tenant": "GMATS", "customer_name": "Cust",
             "lines": [{"item_id": 1, "qty": 8}, {"item_id": 1, "qty": 8}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "two 8-unit lines for a 10-stock item (total 16) should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    it = _stock(db)
    assert it.reserved_stock == 0, f"rejected proforma must reserve nothing, got {it.reserved_stock}"
    assert it.physical_stock == 10
    # And no proforma row was persisted (the 400 fired before any was created).
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma sums duplicate lines and rejects over-reservation (no negative available)")


def test_create_proforma_allows_duplicate_lines_within_stock():
    # Two lines (3 + 4) for the same item total 7 <= 10 available, so it succeeds
    # and reserves the SUM: reserved_stock 0 -> 7. Both lines are recorded.
    db = _db()
    _plain_item(db, 1, physical=10)
    gmats.gmats_create_proforma(
        {"tenant": "GMATS", "customer_name": "Cust",
         "lines": [{"item_id": 1, "qty": 3}, {"item_id": 1, "qty": 4}]},
        db=db, current_user=_ADMIN,
    )
    it = _stock(db)
    assert it.reserved_stock == 7, f"expected reserved 3+4=7, got {it.reserved_stock}"
    assert it.physical_stock == 10                       # reservation never touches physical
    assert db.query(models.GmatsProformaLine).count() == 2
    print("PASS proforma allows duplicate lines within stock and reserves their sum")


def test_create_min_sums_duplicate_lines_and_rejects_over_issue():
    # Two 8-unit MIN lines for a 10-stock item total 16 > 10. The OLD code let both
    # pass the per-line check, then clamped physical to 0 via max(0, ...) while
    # writing both lines at 8 — a later void would restore 16, inflating stock. The
    # summed guard rejects the issue and leaves stock untouched.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_min(
            {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
             "lines": [{"item_id": 1, "qty": 8}, {"item_id": 1, "qty": 8}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "two 8-unit MIN lines for a 10-stock item (total 16) should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db).physical_stock == 10               # rejected issue left stock untouched
    assert db.query(models.GmatsMIN).count() == 0
    print("PASS MIN sums duplicate lines and rejects over-issue (no phantom-stock on void)")


def test_create_min_duplicate_lines_within_stock_then_void_is_neutral():
    # Two lines (3 + 4) total 7 <= 10, so the MIN issues exactly 7: physical 10 -> 3.
    # Voiding it restores exactly 7 (both lines at full qty), back to 10 — proving
    # the exact deduction makes the void a true inverse even with duplicate lines
    # (the old max(0, ...) clamp would only have bitten past physical, but exact
    # deduction is what keeps void neutral here).
    db = _db()
    _plain_item(db, 1, physical=10)
    gmats.gmats_create_min(
        {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
         "lines": [{"item_id": 1, "qty": 3}, {"item_id": 1, "qty": 4}]},
        db=db, current_user=_ADMIN,
    )
    assert _stock(db).physical_stock == 3, f"expected 10-(3+4)=3, got {_stock(db).physical_stock}"
    min_id = db.query(models.GmatsMIN).first().id
    gmats.gmats_void_min(min_id, db=db, current_user=_ADMIN)
    assert _stock(db).physical_stock == 10               # restored exactly 7, back to start
    print("PASS MIN duplicate lines within stock deduct their sum and void restores exactly")


# --- Cross-tenant isolation on the WRITE paths. GmatsItem is not in
# tenancy.SCOPED_MODELS, so the proforma/MIN CREATE endpoints — which locked the
# document HEADER to the caller's tenant but resolved line items by a bare id — let
# a client login reserve/deduct stock on ANOTHER company's item by passing a foreign
# item_id. The read paths were already tenant-filtered; these pin the same scope on
# the create paths (a foreign/unknown id is "not found" for this tenant, and the
# victim's stock is never touched). Expected values are derived by hand. ---


def test_create_proforma_rejects_foreign_tenant_item():
    # ACME client tries to reserve GLOBEX's item (id 2) on its own proforma. The
    # scoped lookup finds no such item for ACME -> 404, and GLOBEX's reserved_stock
    # stays 0 (never reserved by a foreign tenant).
    db = _db()
    _item(db, 1, "ACME", "Acme Widget")      # physical 10, reserved 0
    _item(db, 2, "GLOBEX", "Globex Secret")  # physical 10, reserved 0
    try:
        gmats.gmats_create_proforma(
            {"tenant": "ACME", "customer_name": "Cust", "lines": [{"item_id": 2, "qty": 5}]},
            db=db, current_user=ACME,
        )
        assert False, "reserving a foreign tenant's item should 404, not reserve it"
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
    globex = db.query(models.GmatsItem).filter(models.GmatsItem.id == 2).first()
    assert globex.reserved_stock == 0, f"foreign stock was reserved: {globex.reserved_stock}"
    # No proforma was persisted (the 404 fired before the header was created).
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma create rejects a foreign-tenant item id (victim stock untouched)")


def test_create_min_rejects_foreign_tenant_item():
    # ACME client tries to ISSUE (deduct physical) GLOBEX's item on its own MIN.
    # Scoped lookup -> 404, and GLOBEX's physical_stock is untouched.
    db = _db()
    _item(db, 1, "ACME", "Acme Spare")
    _item(db, 2, "GLOBEX", "Globex Spare")   # physical 10
    try:
        gmats.gmats_create_min(
            {"tenant": "ACME", "customer_name": "Cust", "machine_ref": "Rig",
             "lines": [{"item_id": 2, "qty": 4}]},
            db=db, current_user=ACME,
        )
        assert False, "issuing a foreign tenant's item should 404, not deduct it"
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
    globex = db.query(models.GmatsItem).filter(models.GmatsItem.id == 2).first()
    assert globex.physical_stock == 10, f"foreign stock was deducted: {globex.physical_stock}"
    assert db.query(models.GmatsMIN).count() == 0
    print("PASS MIN create rejects a foreign-tenant item id (victim stock untouched)")


def test_create_proforma_rejects_mixed_own_and_foreign_lines_atomically():
    # A proforma mixing ACME's own item (id 1) with GLOBEX's (id 2) must be rejected
    # WHOLE — the summed validation loop runs before any header/reserve, so the own
    # item is NOT reserved either. Proves the guard is pre-commit, not partial.
    db = _db()
    _item(db, 1, "ACME", "Acme Widget")      # reserved 0
    _item(db, 2, "GLOBEX", "Globex Secret")  # reserved 0
    try:
        gmats.gmats_create_proforma(
            {"tenant": "ACME", "customer_name": "Cust",
             "lines": [{"item_id": 1, "qty": 3}, {"item_id": 2, "qty": 3}]},
            db=db, current_user=ACME,
        )
        assert False, "a proforma with any foreign line should 404 wholesale"
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
    acme = db.query(models.GmatsItem).filter(models.GmatsItem.id == 1).first()
    globex = db.query(models.GmatsItem).filter(models.GmatsItem.id == 2).first()
    assert acme.reserved_stock == 0, "own item must NOT be reserved when a sibling line is foreign"
    assert globex.reserved_stock == 0
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma with a foreign line is rejected wholesale (own item not reserved)")


def test_create_proforma_same_tenant_still_reserves():
    # Regression guard: the new tenant filter must not break the legitimate
    # same-tenant path. ACME reserves its own item exactly as before.
    db = _db()
    _item(db, 1, "ACME", "Acme Widget")      # physical 10, reserved 0
    gmats.gmats_create_proforma(
        {"tenant": "ACME", "customer_name": "Cust", "lines": [{"item_id": 1, "qty": 4}]},
        db=db, current_user=ACME,
    )
    it = db.query(models.GmatsItem).filter(models.GmatsItem.id == 1).first()
    assert it.reserved_stock == 4 and it.physical_stock == 10
    assert db.query(models.GmatsProforma).count() == 1
    print("PASS same-tenant proforma still reserves correctly after the scope fix")


# --- Non-positive line-quantity safety on the create paths. qty comes straight off
# the payload as a bare int, and (unlike gmats_stock_in, which guards `qty <= 0`) the
# proforma/MIN create paths had no lower bound. A negative qty is never > available /
# > physical, so it slipped the guard and was then applied line-by-line: `reserved +=
# qty` drove reserved negative (available past physical) on a proforma, and `physical
# -= qty` INCREASED physical (phantom stock) on a MIN. These pin a clean 400 and prove
# nothing is written. Expected values are derived by hand. ---


def test_create_proforma_rejects_negative_line_qty():
    # Item has 10 physical / 0 reserved. A -5 line is never > available (10), so it
    # used to slip through and run `reserved_stock += -5` -> reserved -5, making
    # available 10-(-5)=15 > physical (phantom availability). The guard 400s first.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_proforma(
            {"tenant": "GMATS", "customer_name": "Cust",
             "lines": [{"item_id": 1, "qty": -5}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "a negative proforma line qty should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    it = _stock(db)
    assert it.reserved_stock == 0, f"rejected proforma must reserve nothing, got {it.reserved_stock}"
    assert it.physical_stock == 10
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma rejects a negative line qty (no negative reserve, no phantom availability)")


def test_create_proforma_rejects_zero_line_qty():
    # A zero-qty line is a meaningless no-op; reject it rather than persist an empty line.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_proforma(
            {"tenant": "GMATS", "customer_name": "Cust",
             "lines": [{"item_id": 1, "qty": 0}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "a zero proforma line qty should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma rejects a zero line qty")


def test_create_proforma_rejects_a_negative_line_mixed_with_a_valid_one_atomically():
    # Two lines for the same item: +8 then -3. Summed that is 5 (<= 10 available), so a
    # sum-only check would PASS — but the apply loop runs each line as-is, and the -3
    # would drive reserved down after the +8. The per-line guard rejects the whole
    # proforma before any write, so reserved stays 0 and no row is persisted.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_proforma(
            {"tenant": "GMATS", "customer_name": "Cust",
             "lines": [{"item_id": 1, "qty": 8}, {"item_id": 1, "qty": -3}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "a proforma with any negative line should 400 wholesale"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    it = _stock(db)
    assert it.reserved_stock == 0, f"the valid +8 line must NOT reserve when a sibling is negative, got {it.reserved_stock}"
    assert it.physical_stock == 10
    assert db.query(models.GmatsProforma).count() == 0
    print("PASS proforma with a negative line is rejected wholesale (valid sibling not reserved)")


def test_create_min_rejects_negative_line_qty():
    # Item has 10 physical. A -4 MIN line is never > physical (10), so it used to slip
    # the guard and run `physical_stock -= -4` -> physical 14 (phantom stock conjured
    # from nothing). The guard 400s first and leaves physical untouched at 10.
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_min(
            {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
             "lines": [{"item_id": 1, "qty": -4}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "a negative MIN line qty should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db).physical_stock == 10, "rejected MIN must not inflate physical stock"
    assert db.query(models.GmatsMIN).count() == 0
    print("PASS MIN rejects a negative line qty (no phantom stock)")


def test_create_min_rejects_zero_line_qty():
    db = _db()
    _plain_item(db, 1, physical=10)
    try:
        gmats.gmats_create_min(
            {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
             "lines": [{"item_id": 1, "qty": 0}]},
            db=db, current_user=_ADMIN,
        )
        assert False, "a zero MIN line qty should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    assert _stock(db).physical_stock == 10
    assert db.query(models.GmatsMIN).count() == 0
    print("PASS MIN rejects a zero line qty")


def test_create_min_positive_line_still_issues():
    # Regression guard: the new qty guard must not break the legitimate positive path.
    # A 3-unit issue deducts exactly: physical 10 -> 7.
    db = _db()
    _plain_item(db, 1, physical=10)
    gmats.gmats_create_min(
        {"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig",
         "lines": [{"item_id": 1, "qty": 3}]},
        db=db, current_user=_ADMIN,
    )
    assert _stock(db).physical_stock == 7, f"expected 10-3=7, got {_stock(db).physical_stock}"
    assert db.query(models.GmatsMIN).count() == 1
    print("PASS MIN still issues a positive line correctly after the qty guard")


# --- Bounded windows + batched line fetch on the transactional listings. The
# proforma / invoice / MIN lists grow without limit as a tenant trades, and each is
# polled — yet they were the only list endpoints in the backend with no .limit()
# cap. /gmats/proformas and /gmats/min also fired a per-row line query (an N+1 that
# grew with every document); those are now collapsed into one batched IN(...) fetch
# per page. These pin the newest-500 bound AND that the batched fetch still groups
# each document's own lines, in insertion order. Expected values derived by hand. ---


def test_proforma_listing_batches_lines_per_document_correctly():
    # Two proformas, each with its own lines: the single batched fetch must group
    # each document's lines under it (no cross-contamination) and keep insertion
    # order — byte-for-byte what the old per-proforma query returned.
    db = _db()
    _item(db, 1, "GMATS", "Part A")
    _item(db, 2, "GMATS", "Part B")
    db.add(models.GmatsProforma(id=1, tenant_code="GMATS", proforma_no="PI-1",
                                customer_name="C1", status="Open"))
    db.add(models.GmatsProforma(id=2, tenant_code="GMATS", proforma_no="PI-2",
                                customer_name="C2", status="Open"))
    db.commit()
    db.add(models.GmatsProformaLine(id=1, proforma_id=1, item_id=1, qty=3))
    db.add(models.GmatsProformaLine(id=2, proforma_id=1, item_id=2, qty=4))
    db.add(models.GmatsProformaLine(id=3, proforma_id=2, item_id=2, qty=7))
    db.commit()

    out = gmats.gmats_proformas(tenant="GMATS", db=db, current_user=_ADMIN)
    by_no = {p["proforma_no"]: p for p in out}
    assert [(l["item_id"], l["qty"]) for l in by_no["PI-1"]["lines"]] == [(1, 3), (2, 4)]
    assert [(l["item_id"], l["qty"]) for l in by_no["PI-2"]["lines"]] == [(2, 7)]
    # Names still resolve from this tenant's item map through the batched path.
    assert by_no["PI-1"]["lines"][0]["item_name"] == "Part A"
    print("PASS proforma listing batches lines per document, in order, names resolved")


def test_proforma_listing_bounds_to_newest_500():
    # 501 proformas -> only the newest 500 (id desc) are returned; the oldest (id 1)
    # is dropped, the second-oldest (id 2) survives — the window is exactly 500.
    db = _db()
    for i in range(1, 502):
        db.add(models.GmatsProforma(id=i, tenant_code="GMATS", proforma_no=f"PI-{i}",
                                    customer_name="C", status="Open"))
    db.commit()

    out = gmats.gmats_proformas(tenant="GMATS", db=db, current_user=_ADMIN)
    assert len(out) == 500, f"expected the newest 500, got {len(out)}"
    assert out[0]["proforma_no"] == "PI-501"          # newest first
    nos = {p["proforma_no"] for p in out}
    assert "PI-1" not in nos, "the oldest proforma beyond the 500-row window must be dropped"
    assert "PI-2" in nos
    print("PASS proforma listing bounds to the newest 500 (oldest beyond the window dropped)")


def test_invoice_listing_bounds_to_newest_500():
    db = _db()
    for i in range(1, 502):
        db.add(models.GmatsInvoice(id=i, tenant_code="GMATS", invoice_no=f"INV-{i}",
                                   proforma_id=None, customer_name="C", status="Generated"))
    db.commit()

    out = gmats.gmats_invoices(tenant="GMATS", db=db, current_user=_ADMIN)
    assert len(out) == 500, f"expected the newest 500, got {len(out)}"
    assert out[0]["invoice_no"] == "INV-501"
    nos = {v["invoice_no"] for v in out}
    assert "INV-1" not in nos and "INV-2" in nos
    print("PASS invoice listing bounds to the newest 500")


def test_min_listing_batches_lines_and_bounds_to_newest_500():
    # 501 MINs -> newest 500; the newest (id 501) carries two lines that the batched
    # fetch must resolve under it, in order, with names from this tenant's items.
    db = _db()
    _item(db, 1, "GMATS", "Spare A")
    _item(db, 2, "GMATS", "Spare B")
    for i in range(1, 502):
        db.add(models.GmatsMIN(id=i, tenant_code="GMATS", min_no=f"MIN-{i}",
                               customer_name="C", machine_ref="R", status="Issued"))
    db.commit()
    db.add(models.GmatsMINLine(id=1, min_id=501, item_id=1, qty=2))
    db.add(models.GmatsMINLine(id=2, min_id=501, item_id=2, qty=5))
    db.commit()

    out = gmats.gmats_min_list(tenant="GMATS", db=db, current_user=_ADMIN)
    assert len(out) == 500, f"expected the newest 500, got {len(out)}"
    assert out[0]["min_no"] == "MIN-501"
    assert [(l["item_id"], l["qty"]) for l in out[0]["lines"]] == [(1, 2), (2, 5)]
    assert out[0]["lines"][0]["item_name"] == "Spare A"
    nos = {m["min_no"] for m in out}
    assert "MIN-1" not in nos and "MIN-2" in nos
    print("PASS MIN listing batches lines and bounds to the newest 500")


# --- CSV import quantity bounds. gmats_import_csv parsed physical_stock /
# reorder_level / purchase_rate with a bare int(float(cell)) — decimal-tolerant
# (Excel/Tally write "5.0") but UNBOUNDED — while every JSON write of these columns
# is bounded to [0, MAX_QTY] by int_field (gmats_create_item / gmats_update_item)
# and the proforma/MIN line paths reject a negative qty. So the CSV path was the one
# write that stored a "-5" as a NEGATIVE physical_stock and widened an out-of-range
# value silently, both then dragging the displayed available_stock and the
# /gmats/summary totals off the truth. int_cell now applies the same bound on the
# CSV side: a bad cell raises, and the importer's per-row except reports it as a
# skipped row rather than writing a corrupt one. Expected values are derived by hand.


class _Upload:
    """Minimal stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text: str):
        self._data = text.encode("utf-8")

    async def read(self):
        return self._data


def _import(db, csv_text, tenant="GMATS", user=_ADMIN):
    return asyncio.run(
        gmats.gmats_import_csv(file=_Upload(csv_text), tenant=tenant, db=db, current_user=user)
    )


def test_int_cell_coerces_defaults_and_bounds():
    # Blank / missing cell -> the default 0 (a blank quantity cell means 0, matching
    # the `or "0"` the call sites applied). None here stands for an absent column.
    assert int_cell(None) == 0
    assert int_cell("") == 0
    assert int_cell("   ") == 0
    # Decimal-tolerant and truncating, exactly like the old int(float(cell)).
    assert int_cell("5") == 5
    assert int_cell("5.0") == 5
    assert int_cell("5.9") == 5
    assert int_cell(0) == 0
    # A negative cell is refused (it used to be stored as a negative stock).
    for bad in ("-1", "-5", -3):
        try:
            int_cell(bad, "physical_stock")
            assert False, f"{bad!r} should be rejected"
        except ValueError as e:
            assert "physical_stock" in str(e) and "less than 0" in str(e), str(e)
    # Non-numeric text is refused, naming the column.
    try:
        int_cell("abc", "reorder_level")
        assert False, "'abc' should be rejected"
    except ValueError as e:
        assert "reorder_level" in str(e) and "whole number" in str(e), str(e)
    # Out-of-range / non-finite refused: "1e999" -> int(inf) OverflowError; "1e20"
    # and MAX_QTY+1 are finite ints past the ceiling. All rejected, none stored.
    for bad in ("1e999", "1e20", str(MAX_QTY + 1)):
        try:
            int_cell(bad, "purchase_rate")
            assert False, f"{bad!r} should be rejected"
        except ValueError as e:
            assert "purchase_rate" in str(e), str(e)
    # The exact ceiling: MAX_QTY passes.
    assert int_cell(str(MAX_QTY)) == MAX_QTY
    print("PASS int_cell coerces decimals, defaults blanks to 0, and bounds to [0, MAX_QTY]")


def test_import_csv_rejects_negative_stock_and_reports_the_row():
    db = _db()
    csv_text = (
        "item_code,item_name,physical_stock,reorder_level,purchase_rate\n"
        "GOOD-1,Good Part,40,10,650\n"     # valid -> created
        "NEG-1,Neg Part,-5,10,650\n"        # negative physical -> raised, reported, NOT stored
    )
    r = _import(db, csv_text)
    assert r["created"] == 1, r
    assert r["updated"] == 0 and r["skipped"] == 0, r
    # The bad row is reported (Row 3 = second data row; header is row 1), naming the field.
    assert any("Row 3" in e and "physical_stock" in e for e in r["errors"]), r["errors"]
    # Only the valid row was written, with its exact values.
    codes = {i.item_code for i in db.query(models.GmatsItem).all()}
    assert codes == {"GOOD-1"}, codes
    good = db.query(models.GmatsItem).filter(models.GmatsItem.item_code == "GOOD-1").first()
    assert good.physical_stock == 40 and good.reorder_level == 10 and good.purchase_rate == 650
    print("PASS CSV import rejects a negative-stock row (reported, not stored) and keeps the valid one")


def test_import_csv_rejects_overflow_quantity():
    db = _db()
    csv_text = (
        "item_code,item_name,physical_stock\n"
        "BIG-1,Big Part,99999999999999999999\n"   # ~1e20, past MAX_QTY -> rejected, not widened
    )
    r = _import(db, csv_text)
    assert r["created"] == 0, r
    assert any("physical_stock" in e for e in r["errors"]), r["errors"]
    assert db.query(models.GmatsItem).count() == 0, "an out-of-range quantity must not be stored"
    print("PASS CSV import rejects an out-of-range stock quantity instead of widening it silently")


def test_import_csv_valid_rows_create_update_and_reconcile():
    db = _db()
    # Pre-existing item to exercise the update branch.
    db.add(models.GmatsItem(tenant_code="GMATS", item_code="UPD-1", item_name="Old Name",
                            physical_stock=1, reserved_stock=0, reorder_level=1,
                            purchase_rate=1, unit="Nos"))
    db.commit()
    csv_text = (
        "item_code,item_name,physical_stock,reorder_level,purchase_rate\n"
        "UPD-1,New Name,25.0,5,700\n"    # update; decimal "25.0" truncates to 25
        "NEW-1,New Part,8,20,\n"          # create; blank rate -> 0 (not a crash)
    )
    r = _import(db, csv_text)
    assert r["created"] == 1 and r["updated"] == 1 and r["errors"] == [], r
    upd = db.query(models.GmatsItem).filter(models.GmatsItem.item_code == "UPD-1").first()
    assert upd.item_name == "New Name"
    assert upd.physical_stock == 25            # "25.0" -> 25
    assert upd.reorder_level == 5 and upd.purchase_rate == 700
    new = db.query(models.GmatsItem).filter(models.GmatsItem.item_code == "NEW-1").first()
    assert new.physical_stock == 8 and new.reorder_level == 20
    assert new.purchase_rate == 0              # blank purchase_rate coalesced to 0
    # The summary reconciles over the two clean rows — no negative dragging it below:
    #   UPD-1 physical 25 + NEW-1 physical 8 = 33 ; reserved 0 ; available 33
    s = gmats.gmats_summary(tenant="GMATS", db=db, current_user=_ADMIN)
    assert s["total_physical"] == 33
    assert s["total_available"] == 33
    assert s["total_available"] == s["total_physical"] - s["total_reserved"]
    print("PASS CSV import creates/updates valid rows (decimals, blanks) and the summary reconciles")


def test_import_csv_header_only_is_a_noop():
    db = _db()
    r = _import(db, "item_code,item_name,physical_stock\n")
    assert r["created"] == 0 and r["updated"] == 0 and r["skipped"] == 0 and r["errors"] == [], r
    assert db.query(models.GmatsItem).count() == 0
    print("PASS CSV import over a header-only file creates nothing")


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
    test_create_proforma_sums_duplicate_lines_and_rejects_over_reserve()
    test_create_proforma_allows_duplicate_lines_within_stock()
    test_create_min_sums_duplicate_lines_and_rejects_over_issue()
    test_create_min_duplicate_lines_within_stock_then_void_is_neutral()
    test_create_proforma_rejects_foreign_tenant_item()
    test_create_min_rejects_foreign_tenant_item()
    test_create_proforma_rejects_mixed_own_and_foreign_lines_atomically()
    test_create_proforma_same_tenant_still_reserves()
    test_create_proforma_rejects_negative_line_qty()
    test_create_proforma_rejects_zero_line_qty()
    test_create_proforma_rejects_a_negative_line_mixed_with_a_valid_one_atomically()
    test_create_min_rejects_negative_line_qty()
    test_create_min_rejects_zero_line_qty()
    test_create_min_positive_line_still_issues()
    test_proforma_listing_batches_lines_per_document_correctly()
    test_proforma_listing_bounds_to_newest_500()
    test_invoice_listing_bounds_to_newest_500()
    test_min_listing_batches_lines_and_bounds_to_newest_500()
    test_int_cell_coerces_defaults_and_bounds()
    test_import_csv_rejects_negative_stock_and_reports_the_row()
    test_import_csv_rejects_overflow_quantity()
    test_import_csv_valid_rows_create_update_and_reconcile()
    test_import_csv_header_only_is_a_noop()
    print("ALL GMATS-INVENTORY ROUTE TESTS PASSED")
