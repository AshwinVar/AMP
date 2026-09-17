"""Every GMATS inventory write leaves an audit row.

The GMATS pilot proposal (docs/GMATS-Pilot-Proposal.md) promises "Admin can
correct/void any operator mistake; full audit trail". None of the module's write
handlers wrote an audit row, and none of the GMATS records stores who made it.
So an Admin could set an item's physical stock from 10 to 7, delete an item, or
void a tax invoice (the invoice row itself is deleted), and nothing anywhere said
who did it, when, or what the figure was before.

The rule these tests pin:

  * every write route in gmats_inventory_routes (POST/PATCH/PUT/DELETE) writes
    exactly one audit row, named after the handler, attributed to the caller;
  * the row names the record (item code, PI/INV/MIN number) and the change
    (old -> new for a correction, the quantity for a stock-in or a void);
  * the row belongs to the company whose records changed. A founder working on a
    customer's records from their own workspace would otherwise stamp the row
    DEFAULT, and the customer's Admin would never see it;
  * a refused write (403 another company, 404, 400) writes no row;
  * a structural guard finds every write route by AST, so a new one fails here
    until it audits itself too.

Run:  python backend/test_gmats_writes_leave_a_trail.py     (exit 0 = pass)
"""
import ast
import asyncio
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gmats_inventory_routes as gmats
import models
import tenancy
from database import Base

FOUNDER = {"sub": "founder", "role": "Admin", "tenant": "DEFAULT"}
GMATS_ADMIN = {"sub": "gmats_admin", "role": "Admin", "tenant": "GMATS"}
GMATS_SUPERVISOR = {"sub": "gmats_super", "role": "Supervisor", "tenant": "GMATS"}
ACME_ADMIN = {"sub": "acme_admin", "role": "Admin", "tenant": "ACME"}

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmats_inventory_routes.py")


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _item(db, iid=1, tenant="GMATS", code="CMP-001", physical=10, reserved=0):
    db.add(models.GmatsItem(id=iid, tenant_code=tenant, item_code=code, item_name=f"Item {code}",
                            category="General", unit="Nos", physical_stock=physical,
                            reserved_stock=reserved, reorder_level=2, purchase_rate=100,
                            location="Rack A", supplier=""))
    db.commit()


def _rows(db):
    return db.query(models.AuditLog).order_by(models.AuditLog.id.asc()).all()


def _only_new_row(db, before, action):
    rows = _rows(db)[before:]
    assert len(rows) == 1, f"{action}: expected exactly one audit row, got {[(r.action, r.details) for r in rows]}"
    row = rows[0]
    assert row.action == action, f"expected action {action!r}, got {row.action!r}"
    return row


class _Upload:
    def __init__(self, text):
        self._data = text.encode("utf-8")

    async def read(self):
        return self._data


# ── Behaviour: each handler, one row, naming the record and the change ──────


def test_correct_item_records_old_and_new_stock():
    db = _db()
    _item(db, physical=10, reserved=3)
    before = len(_rows(db))
    gmats.gmats_correct_item(1, {"physical_stock": 7, "reserved_stock": 3}, db=db, current_user=GMATS_ADMIN)
    row = _only_new_row(db, before, "gmats_correct_item")
    assert row.actor == "gmats_admin"
    assert row.tenant_code == "GMATS"
    assert row.entity_type == "gmats_item" and row.entity_id == 1
    assert "CMP-001" in row.details
    assert "physical_stock 10 -> 7" in row.details, row.details
    assert "reserved_stock" not in row.details, f"an unchanged field is noise: {row.details}"
    print("PASS a stock correction records the item and old -> new physical stock")


def test_update_item_records_what_changed():
    db = _db()
    _item(db)
    before = len(_rows(db))
    gmats.gmats_update_item(1, {"reorder_level": 5, "purchase_rate": 100, "location": "Rack B"},
                            db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_update_item")
    assert row.actor == "gmats_super" and row.tenant_code == "GMATS"
    assert "reorder_level 2 -> 5" in row.details, row.details
    assert "location Rack A -> Rack B" in row.details, row.details
    assert "purchase_rate" not in row.details, row.details
    print("PASS an item edit records only the fields that changed")


def test_stock_in_records_quantity_and_resulting_stock():
    db = _db()
    _item(db, physical=10)
    before = len(_rows(db))
    gmats.gmats_stock_in(1, {"qty": 4}, db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_stock_in")
    assert "CMP-001" in row.details
    assert "+4" in row.details, row.details
    assert "physical_stock 10 -> 14" in row.details, row.details
    print("PASS a stock-in records the quantity and 10 -> 14")


def test_add_alias_records_the_alias():
    db = _db()
    _item(db)
    before = len(_rows(db))
    gmats.gmats_add_alias(1, {"alias_name": "Blue compressor"}, db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_add_alias")
    assert "CMP-001" in row.details and "Blue compressor" in row.details, row.details
    print("PASS an alias records the item and the alias name")


def test_create_item_records_code_and_opening_stock():
    db = _db()
    before = len(_rows(db))
    out = gmats.gmats_create_item({"tenant": "GMATS", "item_code": "NEW-9", "item_name": "Valve",
                                   "physical_stock": 12}, db=db, current_user=GMATS_ADMIN)
    row = _only_new_row(db, before, "gmats_create_item")
    assert row.entity_id == out["id"]
    assert "NEW-9" in row.details and "12" in row.details, row.details
    print("PASS a new item records its code and opening stock")


def test_delete_item_records_what_was_deleted():
    db = _db()
    _item(db, physical=6, reserved=1)
    before = len(_rows(db))
    gmats.gmats_delete_item(1, db=db, current_user=GMATS_ADMIN)
    assert db.query(models.GmatsItem).count() == 0
    row = _only_new_row(db, before, "gmats_delete_item")
    assert row.entity_id == 1
    assert "CMP-001" in row.details and "physical 6" in row.details and "reserved 1" in row.details, row.details
    print("PASS a deleted item records its code and the stock it held")


def _proforma(db, user=GMATS_SUPERVISOR, qty=3):
    return gmats.gmats_create_proforma({"tenant": "GMATS", "customer_name": "Buyer Ltd",
                                        "lines": [{"item_id": 1, "qty": qty}]}, db=db, current_user=user)


def test_proforma_lifecycle_each_step_leaves_a_row():
    db = _db()
    _item(db, physical=10)

    before = len(_rows(db))
    pi = _proforma(db)
    row = _only_new_row(db, before, "gmats_create_proforma")
    assert pi["proforma_no"] in row.details and "Buyer Ltd" in row.details, row.details
    assert "CMP-001 x3" in row.details, row.details

    before = len(_rows(db))
    inv = gmats.gmats_generate_invoice(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_generate_invoice")
    assert inv["invoice_no"] in row.details and pi["proforma_no"] in row.details, row.details
    assert "CMP-001 x3" in row.details, row.details

    before = len(_rows(db))
    gmats.gmats_void_invoice(inv["id"], db=db, current_user=GMATS_ADMIN)
    assert db.query(models.GmatsInvoice).count() == 0, "the invoice row itself is gone"
    row = _only_new_row(db, before, "gmats_void_invoice")
    assert row.actor == "gmats_admin" and row.entity_id == inv["id"]
    assert inv["invoice_no"] in row.details and pi["proforma_no"] in row.details, row.details
    assert "restored CMP-001 x3" in row.details, row.details
    print("PASS proforma -> invoice -> void each leave a row; the void row outlives the invoice")


def test_cancel_proforma_records_released_reservation():
    db = _db()
    _item(db, physical=10)
    pi = _proforma(db, qty=2)
    before = len(_rows(db))
    gmats.gmats_cancel_proforma(pi["id"], db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_cancel_proforma")
    assert pi["proforma_no"] in row.details and "released CMP-001 x2" in row.details, row.details
    print("PASS a cancelled proforma records the reservation it released")


def test_min_issue_and_void_each_leave_a_row():
    db = _db()
    _item(db, physical=10)
    before = len(_rows(db))
    out = gmats.gmats_create_min({"tenant": "GMATS", "customer_name": "Cust", "machine_ref": "Rig 4",
                                  "lines": [{"item_id": 1, "qty": 3}]}, db=db, current_user=GMATS_SUPERVISOR)
    row = _only_new_row(db, before, "gmats_create_min")
    assert out["min_no"] in row.details and "CMP-001 x3" in row.details, row.details

    before = len(_rows(db))
    gmats.gmats_void_min(out["id"], db=db, current_user=GMATS_ADMIN)
    assert db.query(models.GmatsMIN).count() == 0, "the MIN row itself is gone"
    row = _only_new_row(db, before, "gmats_void_min")
    assert row.entity_id == out["id"]
    assert out["min_no"] in row.details and "restored CMP-001 x3" in row.details, row.details
    print("PASS a MIN issue and its void each leave a row; the void row outlives the MIN")


def test_csv_import_records_counts_and_overwritten_stock():
    db = _db()
    _item(db, code="CMP-001", physical=10)
    _item(db, iid=2, code="CMP-002", physical=5)
    csv_text = ("item_code,item_name,physical_stock\n"
                "CMP-001,Item CMP-001,3\n"       # overwrites 10 -> 3
                "CMP-002,Item CMP-002,5\n"       # same figure: updated, stock unchanged
                "CMP-003,Brand new,8\n")         # created
    before = len(_rows(db))
    out = asyncio.run(gmats.gmats_import_csv(file=_Upload(csv_text), tenant="GMATS", db=db, current_user=GMATS_ADMIN))
    assert (out["created"], out["updated"]) == (1, 2), out
    row = _only_new_row(db, before, "gmats_import_csv")
    assert "created 1" in row.details and "updated 2" in row.details, row.details
    assert "CMP-001 physical_stock 10 -> 3" in row.details, row.details
    assert "CMP-002" not in row.details, f"an unchanged stock figure is noise: {row.details}"
    print("PASS a CSV import records its counts and every stock figure it overwrote")


def test_csv_import_caps_the_listed_changes_and_counts_the_rest():
    db = _db()
    for n in range(1, 5):
        _item(db, iid=n, code=f"CMP-00{n}", physical=10)
    csv_text = "item_code,item_name,physical_stock\n" + "".join(
        f"CMP-00{n},Item CMP-00{n},{n}\n" for n in range(1, 5))
    saved = gmats.AUDIT_IMPORT_CHANGES_SHOWN
    gmats.AUDIT_IMPORT_CHANGES_SHOWN = 1
    try:
        asyncio.run(gmats.gmats_import_csv(file=_Upload(csv_text), tenant="GMATS", db=db, current_user=GMATS_ADMIN))
    finally:
        gmats.AUDIT_IMPORT_CHANGES_SHOWN = saved
    row = [r for r in _rows(db) if r.action == "gmats_import_csv"][0]
    assert "CMP-001 physical_stock 10 -> 1" in row.details, row.details
    assert "CMP-002" not in row.details and "(+3 more)" in row.details, row.details
    print("PASS a large import lists the first changes and counts the rest")


def test_document_lines_never_name_another_companys_item():
    """Lines are tenant-checked when a document is created, but a legacy line can
    still point at another company's item id. The audit row is filed under this
    company, so it must not carry the other company's item code."""
    db = _db()
    _item(db, iid=1, tenant="GMATS", code="CMP-001", physical=10)
    _item(db, iid=2, tenant="ACME", code="ACME-SECRET", physical=10)
    db.add(models.GmatsProforma(id=1, tenant_code="GMATS", proforma_no="PI-1001",
                                customer_name="Buyer", status="Open"))
    db.add(models.GmatsProformaLine(proforma_id=1, item_id=1, qty=1))
    db.add(models.GmatsProformaLine(proforma_id=1, item_id=2, qty=1))
    db.commit()
    gmats.gmats_cancel_proforma(1, db=db, current_user=GMATS_SUPERVISOR)
    row = [r for r in _rows(db) if r.action == "gmats_cancel_proforma"][0]
    assert "CMP-001 x1" in row.details, row.details
    assert "ACME-SECRET" not in row.details and "item 2 x1" in row.details, row.details
    print("PASS an audit row never names another company's item")


# ── Tenancy: the row belongs to the company whose records changed ───────────


def test_founder_correction_is_visible_to_the_customer():
    """The founder works on GMATS records from the DEFAULT workspace (the request
    context is DEFAULT; the by-id routes take no tenant). Without an explicit
    tenant the before_flush stamp files the row under DEFAULT, and the GMATS
    Admin's audit log (tenant-scoped) never shows who changed their stock."""
    db = _db()
    _item(db, physical=10)
    tenancy.install_scoping()
    token = tenancy.set_current_tenant("DEFAULT")
    try:
        gmats.gmats_correct_item(1, {"physical_stock": 4}, db=db, current_user=FOUNDER)
    finally:
        tenancy.reset_current_tenant(token)
    rows = [r for r in _rows(db) if r.action == "gmats_correct_item"]
    assert len(rows) == 1
    assert rows[0].tenant_code == "GMATS", f"filed under {rows[0].tenant_code!r}, invisible to the customer"
    assert rows[0].actor == "founder"

    token = tenancy.set_current_tenant("GMATS")
    try:
        seen = db.query(models.AuditLog).filter(models.AuditLog.action == "gmats_correct_item").count()
    finally:
        tenancy.reset_current_tenant(token)
    assert seen == 1, "the GMATS Admin's scoped audit read must include the founder's correction"
    print("PASS a founder's correction is filed under the customer and shows in their audit log")


# ── Refused writes leave no row ─────────────────────────────────────────────


def _expect(status, fn):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == status, f"expected {status}, got {e.status_code}"
        return
    raise AssertionError(f"expected HTTP {status}")


def test_refused_writes_leave_no_row():
    db = _db()
    _item(db, physical=2)
    before = len(_rows(db))
    _expect(403, lambda: gmats.gmats_correct_item(1, {"physical_stock": 99}, db=db, current_user=ACME_ADMIN))
    _expect(403, lambda: gmats.gmats_delete_item(1, db=db, current_user=ACME_ADMIN))
    _expect(404, lambda: gmats.gmats_correct_item(999, {"physical_stock": 1}, db=db, current_user=GMATS_ADMIN))
    _expect(404, lambda: gmats.gmats_void_invoice(999, db=db, current_user=GMATS_ADMIN))
    _expect(400, lambda: gmats.gmats_create_min({"tenant": "GMATS", "customer_name": "C",
                                                 "lines": [{"item_id": 1, "qty": 50}]},
                                                db=db, current_user=GMATS_SUPERVISOR))
    _expect(400, lambda: gmats.gmats_cancel_proforma(999, db=db, current_user=GMATS_SUPERVISOR))
    assert len(_rows(db)) == before, [(r.action, r.details) for r in _rows(db)[before:]]
    assert db.query(models.GmatsItem).first().physical_stock == 2
    print("PASS refused writes (403 / 404 / 400) leave no audit row")


# ── Structure: every write route audits itself, after its last write ───────

WRITE_VERBS = {"post", "patch", "put", "delete"}


def _write_routes(tree):
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in WRITE_VERBS
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
                found.append(node)
                break
    return found


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _action_of(call):
    for kw in call.keywords:
        if kw.arg == "action" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    # _audit(db, current_user, tenant, action, ...)
    if len(call.args) >= 4 and isinstance(call.args[3], ast.Constant):
        return call.args[3].value
    return None


def audit_violations(source):
    """Return (write_route_names, problems) for a module's source."""
    tree = ast.parse(source)
    routes = _write_routes(tree)
    problems = []
    for fn in routes:
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        audits = [c for c in calls if _call_name(c) == "_audit"]
        own = [c for c in audits if _action_of(c) == fn.name]
        if not own:
            named = [_action_of(c) for c in audits]
            problems.append(f"{fn.name}: no _audit(..., {fn.name!r}, ...) call (found {named})")
            continue
        last_write = max([c.lineno for c in calls
                          if _call_name(c) in ("commit", "_guard_record")] or [0])
        if max(c.lineno for c in own) < last_write:
            problems.append(f"{fn.name}: audits before its last commit/guard (line {last_write})")
    return [fn.name for fn in routes], problems


def test_every_write_route_audits_itself():
    with open(MODULE_PATH, encoding="utf-8") as fh:
        names, problems = audit_violations(fh.read())
    assert len(names) >= 13, f"guard found only {len(names)} write routes: {names}"
    assert not problems, "GMATS write routes without an audit row:\n  " + "\n  ".join(problems)
    print(f"PASS all {len(names)} GMATS write routes audit themselves after their last write")


def test_the_guard_catches_what_it_claims_to():
    probe = '''
router = None
@router.post("/a")
def silent(db):
    db.commit()
    return {}

@router.patch("/b")
def early(db, current_user, item):
    _audit(db, current_user, item.tenant_code, "early", "x", 1, "d")
    db.commit()

@router.delete("/c")
def wrong_name(db, current_user, item):
    db.commit()
    _audit(db, current_user, item.tenant_code, "gmats_something_else", "x", 1, "d")

@router.post("/d")
async def good(db, current_user, item):
    _guard_record(current_user, item.tenant_code)
    db.commit()
    _audit(db, current_user, item.tenant_code, "good", "x", 1, "d")

@router.get("/e")
def reader(db):
    return []
'''
    names, problems = audit_violations(probe)
    assert names == ["silent", "early", "wrong_name", "good"], names
    flagged = sorted(p.split(":")[0] for p in problems)
    assert flagged == ["early", "silent", "wrong_name"], problems
    print("PASS the guard flags a silent route, an early audit and a mis-named audit; ignores reads")


def test_log_audit_honours_an_explicit_tenant():
    from platform_routes import log_audit
    db = _db()
    tenancy.install_scoping()
    token = tenancy.set_current_tenant("DEFAULT")
    try:
        log_audit(db, "founder", "probe_explicit", "x", 1, "d", tenant_code="GMATS")
        log_audit(db, "founder", "probe_context", "x", 2, "d")
    finally:
        tenancy.reset_current_tenant(token)
    by_action = {r.action: r.tenant_code for r in _rows(db)}
    assert by_action == {"probe_explicit": "GMATS", "probe_context": "DEFAULT"}, by_action
    print("PASS log_audit files an explicit tenant, and still stamps the request tenant otherwise")


if __name__ == "__main__":
    test_correct_item_records_old_and_new_stock()
    test_update_item_records_what_changed()
    test_stock_in_records_quantity_and_resulting_stock()
    test_add_alias_records_the_alias()
    test_create_item_records_code_and_opening_stock()
    test_delete_item_records_what_was_deleted()
    test_proforma_lifecycle_each_step_leaves_a_row()
    test_cancel_proforma_records_released_reservation()
    test_min_issue_and_void_each_leave_a_row()
    test_csv_import_records_counts_and_overwritten_stock()
    test_csv_import_caps_the_listed_changes_and_counts_the_rest()
    test_document_lines_never_name_another_companys_item()
    test_founder_correction_is_visible_to_the_customer()
    test_refused_writes_leave_no_row()
    test_every_write_route_audits_itself()
    test_the_guard_catches_what_it_claims_to()
    test_log_audit_honours_an_explicit_tenant()
    print("ALL GMATS AUDIT-TRAIL TESTS PASSED")
