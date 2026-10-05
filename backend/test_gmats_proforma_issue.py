"""GMATS issues a proforma invoice. It does not raise tax invoices.

WHAT CHANGED, AND WHY
---------------------
The module modelled a two-document sale: a proforma (PI-1000+) reserved the
stock, and "Generate Tax Invoice" created a SECOND document (INV-7000+, its own
number series, a printable PDF) whose only operational effect was to move the
reserved quantity out of physical stock.

GMATS does not raise its tax invoices from AMP. So the second document was a
legal-looking artefact AMP had no business issuing, and the number series was a
second identity for one sale. The proforma is now the only document: issuing it
is what moves the stock.

    Create Proforma  ->  reserve           (available -> reserved, physical untouched)
    Issue            ->  deduct physical, clear the reservation, status "Issued"
    Undo issue       ->  the exact inverse, back to "Open" and reserved (Admin)

THE LEGACY ROWS ARE REAL, AND THEY STAY
---------------------------------------
Production holds 8 proformas with the old status "Invoiced" and the 8
GmatsInvoice rows they produced. Their stock was deducted exactly as an issue
deducts it, so they are the same state under an older name. They are NOT
migrated or deleted: the list shows both, and `undo` accepts both — undoing a
legacy one also removes the tax-invoice row it left behind, so an Open proforma
is never left with an invoice pointing at it.

Run:  python backend/test_gmats_proforma_issue.py     (exit 0 = pass)
"""
import main

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gmats_inventory_routes as gmats
import models
from database import Base

GMATS_SUPERVISOR = {"sub": "gmats_super", "role": "Supervisor", "tenant": "GMATS"}
GMATS_ADMIN = {"sub": "gmats_admin", "role": "Admin", "tenant": "GMATS"}
ACME_ADMIN = {"sub": "acme_admin", "role": "Admin", "tenant": "ACME"}


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(db, iid=1, tenant="GMATS", physical=10, reserved=0):
    db.add(models.GmatsItem(id=iid, tenant_code=tenant, item_code=f"CMP-00{iid}", item_name="Compressor spare",
                            category="General", unit="Nos", physical_stock=physical, reserved_stock=reserved,
                            reorder_level=2, purchase_rate=100, location="Rack A", supplier=""))
    db.commit()
    return db.query(models.GmatsItem).filter(models.GmatsItem.id == iid).first()


def _proforma(db, user=GMATS_SUPERVISOR, tenant="GMATS", item_id=1, qty=3):
    return gmats.gmats_create_proforma({"tenant": tenant, "customer_name": "Buyer Ltd",
                                        "lines": [{"item_id": item_id, "qty": qty}]},
                                       db=db, current_user=user)


def _stock(db, iid=1):
    i = db.query(models.GmatsItem).filter(models.GmatsItem.id == iid).first()
    return (i.physical_stock, i.reserved_stock)


def _status(db, pid):
    return db.query(models.GmatsProforma).filter(models.GmatsProforma.id == pid).first().status


def _expect(code, call):
    try:
        call()
    except HTTPException as e:
        assert e.status_code == code, f"expected {code}, got {e.status_code}: {e.detail}"
        return e
    raise AssertionError(f"expected HTTP {code}, call succeeded")


def _route_roles(path, method):
    """The roles a registered route actually demands, read off its dependency."""
    for r in main.app.routes:
        if getattr(r, "path", "") == path and method in (getattr(r, "methods", None) or set()):
            for d in r.dependant.dependencies:
                fn = d.call
                if getattr(fn, "__qualname__", "").endswith("role_checker"):
                    cells = dict(zip(fn.__code__.co_freevars, (c.cell_contents for c in fn.__closure__ or ())))
                    return cells.get("allowed_roles")
            return None
    raise AssertionError(f"{method} {path} is not registered")


# --------------------------------------------------------------------------- #

def test_issuing_deducts_physical_and_clears_the_reservation():
    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=3)
    assert _stock(db) == (10, 3), "creating the proforma must reserve, not deduct"

    out = gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)

    assert _stock(db) == (7, 0), f"issue must deduct physical and clear the reservation, got {_stock(db)}"
    assert _status(db, pi["id"]) == "Issued", _status(db, pi["id"])
    assert out["proforma_no"] == pi["proforma_no"], out
    print("PASS issuing a proforma deducts physical stock and clears its reservation")


def test_nothing_generates_a_tax_invoice_any_more():
    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=3)
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)

    assert db.query(models.GmatsInvoice).count() == 0, "issuing must not create a tax invoice"

    # The handlers are gone, not merely unused by the UI: a route left registered
    # is a route somebody can still call.
    for gone in ("gmats_generate_invoice", "gmats_void_invoice", "gmats_invoices"):
        assert not hasattr(gmats, gone), f"{gone} is still importable"
    paths = {getattr(r, "path", "") for r in main.app.routes}
    for gone in ("/gmats/proformas/{pid}/invoice", "/gmats/invoices", "/gmats/invoices/{inv_id}"):
        assert gone not in paths, f"{gone} is still registered"
    print("PASS no tax invoice is written, and the tax-invoice routes are gone")


def test_issuing_more_than_the_shelf_holds_is_refused_and_deducts_nothing():
    db = _db()
    _item(db, physical=4)
    pi = _proforma(db, qty=4)
    # Somebody counts the shelf and corrects it downward after the reservation.
    gmats.gmats_correct_item(1, {"physical_stock": 2}, db=db, current_user=GMATS_ADMIN)
    before = _stock(db)

    e = _expect(400, lambda: gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR))

    assert "only 2 physical" in str(e.detail), e.detail
    assert _stock(db) == before, f"a refused issue must change nothing, got {_stock(db)} was {before}"
    assert _status(db, pi["id"]) == "Open", _status(db, pi["id"])
    print("PASS issuing more than the physical stock is refused, and deducts nothing")


def test_a_proforma_is_issued_once():
    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=3)
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    after = _stock(db)

    _expect(400, lambda: gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR))
    _expect(400, lambda: gmats.gmats_cancel_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR))

    assert _stock(db) == after, "a second issue must not deduct twice"
    print("PASS an issued proforma cannot be issued again, nor cancelled")


def test_undo_is_the_exact_inverse_of_an_issue():
    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=3)
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    assert _stock(db) == (7, 0)

    gmats.gmats_undo_issue(pi["id"], db=db, current_user=GMATS_ADMIN)

    # Back to the state the issue found: stock on the shelf AND still spoken for,
    # which is what "Open" means everywhere else in this module.
    assert _stock(db) == (10, 3), f"undo must restore physical and the reservation, got {_stock(db)}"
    assert _status(db, pi["id"]) == "Open", _status(db, pi["id"])
    # ...and the document is live again: it can be issued, or cancelled to release.
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    assert _stock(db) == (7, 0)
    print("PASS undo restores the stock and the reservation, and the proforma is live again")


def test_undo_is_admin_only_and_only_for_an_issued_proforma():
    assert _route_roles("/gmats/proformas/{pid}/issue", "POST") == ["Admin", "Supervisor"], \
        _route_roles("/gmats/proformas/{pid}/issue", "POST")
    assert _route_roles("/gmats/proformas/{pid}/undo-issue", "PATCH") == ["Admin"], \
        _route_roles("/gmats/proformas/{pid}/undo-issue", "PATCH")

    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=3)
    _expect(400, lambda: gmats.gmats_undo_issue(pi["id"], db=db, current_user=GMATS_ADMIN))  # still Open
    _expect(404, lambda: gmats.gmats_undo_issue(999, db=db, current_user=GMATS_ADMIN))
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    gmats.gmats_undo_issue(pi["id"], db=db, current_user=GMATS_ADMIN)
    _expect(400, lambda: gmats.gmats_undo_issue(pi["id"], db=db, current_user=GMATS_ADMIN))  # already undone
    print("PASS issue is Admin/Supervisor, undo is Admin only, and only an issued proforma can be undone")


def test_a_legacy_tax_invoiced_proforma_can_still_be_undone():
    """Production's 8 'Invoiced' proformas are the same state under an older name."""
    db = _db()
    _item(db, physical=10)
    # A decoy first, so the legacy proforma's id (2) differs from its invoice's (1):
    # a lookup keyed on the wrong column would otherwise match by coincidence.
    _proforma(db, qty=1)
    pi = _proforma(db, qty=3)
    # Exactly what the old flow left behind: physical deducted, reservation cleared,
    # status "Invoiced", and a tax-invoice row pointing at the proforma.
    gmats.gmats_issue_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    p = db.query(models.GmatsProforma).filter(models.GmatsProforma.id == pi["id"]).first()
    p.status = "Invoiced"
    db.add(models.GmatsInvoice(tenant_code="GMATS", invoice_no="INV-7003", proforma_id=p.id,
                               customer_name=p.customer_name, status="Generated"))
    db.commit()

    gmats.gmats_undo_issue(pi["id"], db=db, current_user=GMATS_ADMIN)

    # physical back to 10; reserved 4 = this document's 3 plus the decoy's 1,
    # which was never issued and must still be held.
    assert _stock(db) == (10, 4), f"a legacy deduction must reverse the same way, got {_stock(db)}"
    assert _status(db, pi["id"]) == "Open"
    assert db.query(models.GmatsInvoice).count() == 0, \
        "the tax-invoice row must go with it — an Open proforma with an invoice pointing at it is a lie"
    row = (db.query(models.AuditLog).filter(models.AuditLog.action == "gmats_undo_issue")
           .order_by(models.AuditLog.id.desc()).first())
    assert row and "INV-7003" in row.details, f"the audit row must name the invoice it removed: {row and row.details}"
    print("PASS a legacy 'Invoiced' proforma undoes too, and takes its tax-invoice row with it")


def test_the_issued_list_holds_both_spellings_and_another_company_cannot_touch_it():
    db = _db()
    _item(db, physical=20)
    new, legacy, open_one = _proforma(db, qty=3), _proforma(db, qty=2), _proforma(db, qty=1)
    gmats.gmats_issue_proforma(new["id"], db=db, current_user=GMATS_SUPERVISOR)
    gmats.gmats_issue_proforma(legacy["id"], db=db, current_user=GMATS_SUPERVISOR)
    p = db.query(models.GmatsProforma).filter(models.GmatsProforma.id == legacy["id"]).first()
    p.status = "Invoiced"
    db.commit()

    listed = gmats.gmats_proformas(tenant="GMATS", status="Issued,Invoiced", db=db, current_user=GMATS_ADMIN)
    assert {r["proforma_no"] for r in listed} == {new["proforma_no"], legacy["proforma_no"]}, listed
    assert {r["status"] for r in listed} == {"Issued", "Invoiced"}, listed
    assert len(gmats.gmats_proformas(tenant="GMATS", db=db, current_user=GMATS_ADMIN)) == 3, "unfiltered lists all"
    assert [r["proforma_no"] for r in gmats.gmats_proformas(tenant="GMATS", status="Open",
                                                            db=db, current_user=GMATS_ADMIN)] == [open_one["proforma_no"]]

    # Another company's Admin cannot issue or undo a GMATS document.
    before = _stock(db)
    _expect(403, lambda: gmats.gmats_issue_proforma(open_one["id"], db=db, current_user=ACME_ADMIN))
    _expect(403, lambda: gmats.gmats_undo_issue(new["id"], db=db, current_user=ACME_ADMIN))
    assert _stock(db) == before and _status(db, new["id"]) == "Issued"
    print("PASS the issued list holds both spellings, and another company can neither issue nor undo")


if __name__ == "__main__":
    test_issuing_deducts_physical_and_clears_the_reservation()
    test_nothing_generates_a_tax_invoice_any_more()
    test_issuing_more_than_the_shelf_holds_is_refused_and_deducts_nothing()
    test_a_proforma_is_issued_once()
    test_undo_is_the_exact_inverse_of_an_issue()
    test_undo_is_admin_only_and_only_for_an_issued_proforma()
    test_a_legacy_tax_invoiced_proforma_can_still_be_undone()
    test_the_issued_list_holds_both_spellings_and_another_company_cannot_touch_it()
    print("GMATS PROFORMA ISSUE OK: the proforma is the document; issuing deducts, undo restores, "
          "no tax invoice is written, and the legacy rows still work")
