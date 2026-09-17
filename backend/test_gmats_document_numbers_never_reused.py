"""A GMATS tax invoice, MIN or proforma number is never issued twice.

GMATS numbered its documents with ``f"INV-{7000 + count + 1}"``, where count is
the tenant's current number of invoices. Voiding an invoice or a MIN DELETES the
row, so the count drops and the next document is issued a number that is still
printed on a live one:

    INV-7001 issued, INV-7002 issued, INV-7001 voided -> next invoice: INV-7002

Two tax invoices with one number. An invoice number is the legal identity of
the document (GST requires a unique serial per invoice), and nothing in the
schema enforces uniqueness on these tables, so the duplicate is simply stored.

doc_numbers.allocate already exists for exactly this. It keeps a per-tenant
counter that only moves forward and seeds itself from the highest number already
issued, so it can take over from count()+1 on live data. The enterprise
inventory routes use it; the GMATS routes never did. A void now leaves a gap,
which is auditable (the void's audit row names the number). A reuse is not.

Run:  python backend/test_gmats_document_numbers_never_reused.py     (exit 0 = pass)
"""
import ast
import glob
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gmats_inventory_routes as gmats
import models
from database import Base

GMATS_SUPERVISOR = {"sub": "gmats_super", "role": "Supervisor", "tenant": "GMATS"}
GMATS_ADMIN = {"sub": "gmats_admin", "role": "Admin", "tenant": "GMATS"}
ACME_SUPERVISOR = {"sub": "acme_super", "role": "Supervisor", "tenant": "ACME"}
FOUNDER = {"sub": "founder", "role": "Admin", "tenant": "DEFAULT"}

BACKEND = os.path.dirname(os.path.abspath(__file__))


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(db, iid=1, tenant="GMATS", physical=100):
    db.add(models.GmatsItem(id=iid, tenant_code=tenant, item_code=f"{tenant}-{iid}", item_name="Spare",
                            category="General", unit="Nos", physical_stock=physical, reserved_stock=0,
                            reorder_level=0, purchase_rate=0, location="", supplier=""))
    db.commit()


def _proforma(db, user=GMATS_SUPERVISOR, tenant="GMATS", item_id=1, qty=1):
    return gmats.gmats_create_proforma({"tenant": tenant, "customer_name": "Buyer",
                                        "lines": [{"item_id": item_id, "qty": qty}]}, db=db, current_user=user)


def _invoice(db, user=GMATS_SUPERVISOR, tenant="GMATS", item_id=1):
    pi = _proforma(db, user=user, tenant=tenant, item_id=item_id)
    return gmats.gmats_generate_invoice(pi["id"], db=db, current_user=user)


def _min(db, user=GMATS_SUPERVISOR, tenant="GMATS", item_id=1, qty=1):
    return gmats.gmats_create_min({"tenant": tenant, "customer_name": "Cust", "machine_ref": "Rig",
                                   "lines": [{"item_id": item_id, "qty": qty}]}, db=db, current_user=user)


def _live(db, model, column, tenant="GMATS"):
    return sorted(getattr(r, column) for r in db.query(model).filter(model.tenant_code == tenant).all())


def test_a_voided_invoice_never_hands_its_count_to_a_live_number():
    db = _db()
    _item(db)
    a = _invoice(db)
    b = _invoice(db)
    assert (a["invoice_no"], b["invoice_no"]) == ("INV-7001", "INV-7002"), (a, b)
    gmats.gmats_void_invoice(a["id"], db=db, current_user=GMATS_ADMIN)
    c = _invoice(db)
    live = _live(db, models.GmatsInvoice, "invoice_no")
    assert len(live) == len(set(live)), f"two live tax invoices share a number: {live}"
    assert c["invoice_no"] == "INV-7003", f"got {c['invoice_no']}: 7002 is a live invoice, 7001 was voided"
    print("PASS after a void the next invoice is INV-7003, never a live or voided number")


def test_a_voided_min_never_hands_its_count_to_a_live_number():
    db = _db()
    _item(db)
    a = _min(db)
    b = _min(db)
    assert (a["min_no"], b["min_no"]) == ("MIN-4001", "MIN-4002"), (a, b)
    gmats.gmats_void_min(a["id"], db=db, current_user=GMATS_ADMIN)
    c = _min(db)
    live = _live(db, models.GmatsMIN, "min_no")
    assert len(live) == len(set(live)), f"two live MINs share a number: {live}"
    assert c["min_no"] == "MIN-4003", c
    print("PASS after a void the next MIN is MIN-4003")


def test_numbering_continues_above_numbers_already_issued():
    """Production already holds numbers issued by count()+1, including any gap a
    past void left. The first allocation must start above the highest of them."""
    db = _db()
    _item(db)
    db.add(models.GmatsProforma(tenant_code="GMATS", proforma_no="PI-1004", customer_name="Old", status="Invoiced"))
    db.add(models.GmatsInvoice(tenant_code="GMATS", invoice_no="INV-7009", customer_name="Old", status="Generated"))
    db.add(models.GmatsMIN(tenant_code="GMATS", min_no="MIN-4007", customer_name="Old", status="Issued"))
    db.commit()
    pi = _proforma(db)
    assert pi["proforma_no"] == "PI-1005", pi
    inv = gmats.gmats_generate_invoice(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    assert inv["invoice_no"] == "INV-7010", inv
    assert _min(db)["min_no"] == "MIN-4008"
    print("PASS numbering continues above PI-1004 / INV-7009 / MIN-4007 already on file")


def test_each_company_keeps_its_own_series():
    db = _db()
    _item(db, iid=1, tenant="GMATS")
    _item(db, iid=2, tenant="ACME")
    for _ in range(3):
        _invoice(db)
    acme = _invoice(db, user=ACME_SUPERVISOR, tenant="ACME", item_id=2)
    assert acme["invoice_no"] == "INV-7001", acme
    # The founder invoices for GMATS from the DEFAULT workspace: the number comes
    # from the series of the company the invoice is filed under, not the caller's.
    founder = _invoice(db, user=FOUNDER, tenant="GMATS")
    assert founder["invoice_no"] == "INV-7004", founder
    print("PASS ACME's first invoice is INV-7001; the founder's GMATS invoice continues GMATS's series")


def test_a_refused_document_does_not_burn_a_number():
    db = _db()
    _item(db, physical=5)
    try:
        _min(db, qty=50)
        raise AssertionError("an over-issue must be refused")
    except HTTPException as e:
        assert e.status_code == 400
    try:
        _proforma(db, qty=50)
        raise AssertionError("an over-reserve must be refused")
    except HTTPException as e:
        assert e.status_code == 400
    assert _min(db)["min_no"] == "MIN-4001"
    assert _proforma(db)["proforma_no"] == "PI-1001"
    print("PASS a refused MIN or proforma leaves no gap in the series")


# ── Structure: no route numbers a document from a row count ────────────────


def _count_derived_numbers(source):
    """Line numbers of f-strings whose value adds to a count (`count + 1`, `.count() + 1`)."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            for add in ast.walk(part.value):
                if not (isinstance(add, ast.BinOp) and isinstance(add.op, ast.Add)):
                    continue
                for sub in ast.walk(add):
                    if ((isinstance(sub, ast.Name) and sub.id == "count")
                            or (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                                and sub.func.attr == "count")):
                        hits.append(node.lineno)
    return sorted(set(hits))


def test_no_route_numbers_a_document_from_a_row_count():
    modules = sorted(glob.glob(os.path.join(BACKEND, "*_routes.py")))
    assert len(modules) >= 20, f"found only {len(modules)} route modules"
    offenders = {}
    for path in modules:
        with open(path, encoding="utf-8") as fh:
            hits = _count_derived_numbers(fh.read())
        if hits:
            offenders[os.path.basename(path)] = hits
    assert not offenders, ("document numbers derived from a row count (use doc_numbers.allocate): "
                           f"{offenders}")
    print(f"PASS none of {len(modules)} route modules numbers a document from a row count")


def test_gmats_documents_draw_from_the_shared_sequence():
    with open(os.path.join(BACKEND, "gmats_inventory_routes.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = {"gmats_create_proforma": "PI", "gmats_generate_invoice": "INV", "gmats_create_min": "MIN"}
    seen = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in wanted:
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "allocate"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "doc_numbers"):
                    prefixes = [a.value for a in call.args if isinstance(a, ast.Constant)]
                    seen[fn.name] = prefixes
    for name, prefix in wanted.items():
        assert name in seen, f"{name} does not call doc_numbers.allocate"
        assert prefix in seen[name], f"{name} allocates {seen[name]}, expected prefix {prefix}"
    print("PASS proforma, invoice and MIN numbers come from doc_numbers.allocate")


def test_the_count_guard_catches_what_it_claims_to():
    probe = '''
def a(db, count):
    no = f"INV-{7000 + count + 1}"
def b(db, M):
    no = f"MIN-{4000 + db.query(M).count() + 1}"
def c(db):
    no = f"PI-{n + 1}"
def d(db, rows, count):
    label = f"{len(rows)} rows, {count} imported"
'''
    assert _count_derived_numbers(probe) == [3, 5], _count_derived_numbers(probe)
    print("PASS the guard flags count + 1 and .count() + 1, and ignores a count merely printed")


if __name__ == "__main__":
    test_a_voided_invoice_never_hands_its_count_to_a_live_number()
    test_a_voided_min_never_hands_its_count_to_a_live_number()
    test_numbering_continues_above_numbers_already_issued()
    test_each_company_keeps_its_own_series()
    test_a_refused_document_does_not_burn_a_number()
    test_no_route_numbers_a_document_from_a_row_count()
    test_gmats_documents_draw_from_the_shared_sequence()
    test_the_count_guard_catches_what_it_claims_to()
    print("ALL GMATS DOCUMENT-NUMBER TESTS PASSED")
