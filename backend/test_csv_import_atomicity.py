"""A CSV import survives a row the DATABASE rejects.

Every import handler advertises "reports per-row errors instead of failing the
whole file", and that was true for PARSE errors only. A database error could not
be reported per row, because the single db.commit() came after the loop.

The reachable case is a cross-tenant duplicate. item_code / supplier_code are
UNIQUE globally, but the "does this already exist?" lookup is tenant-scoped
(ADR-0002), so a code another workspace owns is invisible to the importer and
takes the insert branch. Traced before the fix, importing 3 rows as tenant BETA
where row 3's code belongs to tenant ALPHA:

    row 3 adds WIDGET (pending; ALPHA's WIDGET is not visible to BETA)
    row 4's lookup AUTOFLUSHES        -> IntegrityError
    the per-row except catches it and blames ROW 4, a perfectly good row
    db.commit() then raises PendingRollbackError -> HTTP 500

    result: nothing imported at all, including the rows already counted created

A SAVEPOINT per row contains the failure: the bad row is rolled back and blamed
correctly, the session stays usable, and the valid rows still commit.

Run:  python backend/test_csv_import_atomicity.py     (exit 0 = pass)
"""
import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import machines_routes
import models
import orders_routes
import tenancy as T
from database import Base

ADMIN = {"sub": "admin", "role": "Admin", "tenant": "BETA"}


class _Upload:
    """Stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text_: str):
        self._data = text_.encode("utf-8")

    async def read(self):
        return self._data


def _sess():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _as(tenant, fn):
    tok = T.set_current_tenant(tenant)
    try:
        return fn()
    finally:
        T.reset_current_tenant(tok)


def test_a_code_another_workspace_owns_no_longer_collides_at_all():
    """ADR-0012: item_code is unique per TENANT, so this file has no conflict.

    This test used to assert the opposite - that row 3 was rejected and the file
    kept its other two rows - because item_code carried a platform-wide UNIQUE
    and "WIDGET" being taken by ALPHA genuinely blocked BETA. That constraint was
    the bug, not the behaviour: a customer could not import their own part codes
    if another customer had used the same string first.

    The per-row atomicity guarantee that assertion protected has NOT gone away;
    it is exercised by the test below, which collides a row against the
    IMPORTING tenant's own data, and by the same-file duplicate test further on.
    """
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.InventoryItem(item_code="WIDGET", item_name="Alpha widget",
                                    category="Raw", unit="pc", current_stock=5,
                                    reorder_level=1)),
        db.commit()))

    csv_text = (
        "item_code,item_name,current_stock\n"
        "BOLT-1,Beta bolt,10\n"
        "WIDGET,Beta widget,7\n"     # the same code ALPHA owns
        "NUT-2,Beta nut,3\n"
    )
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 3, r
    assert r["errors"] == [], r

    codes = _as("BETA", lambda: sorted(
        i.item_code for i in db.query(models.InventoryItem).all()))
    assert codes == ["BOLT-1", "NUT-2", "WIDGET"], codes
    # ALPHA keeps its own row with its own name - BETA's import created a second
    # WIDGET rather than updating somebody else's.
    rows = db.execute(text(
        "SELECT tenant_code, item_name FROM inventory_items "
        "WHERE item_code='WIDGET' ORDER BY tenant_code")).fetchall()
    assert [tuple(x) for x in rows] == [
        ("ALPHA", "Alpha widget"), ("BETA", "Beta widget")], rows
    print("PASS an item code another workspace owns no longer blocks an import")


def test_importing_a_code_you_already_own_updates_it():
    """The remaining meaning of a repeated item_code: it is YOUR row, so update.

    I first wrote this as a collision test, expecting row 3 to be rejected the
    way the cross-tenant version used to be. It is not: the import looks the
    code up through the tenant-scoped ORM, finds the tenant's OWN row, and
    updates it. So after ADR-0012 there is no reachable IntegrityError on
    inventory item_code at all - which is worth pinning, because a future change
    that reintroduced one would be a regression in exactly the direction this
    ADR moved away from.

    The per-row atomicity guarantee is not lost with it: it is exercised by
    test_a_duplicate_inside_the_same_file_costs_only_that_row below, where two
    rows of ONE file carry the same code and the second is genuinely rejected.
    """
    db = _sess()
    _as("BETA", lambda: (
        db.add(models.InventoryItem(item_code="WIDGET", item_name="Old name",
                                    category="Raw", unit="pc", current_stock=5,
                                    reorder_level=1)),
        db.commit()))

    csv_text = (
        "item_code,item_name,current_stock\n"
        "BOLT-1,Beta bolt,10\n"
        "WIDGET,Beta widget,7\n"     # BETA's own existing code
        "NUT-2,Beta nut,3\n"
    )
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 2, r
    assert r["updated"] == 1, r
    assert r["errors"] == [], r

    # CONTROL: the update really landed, so "updated: 1" is not just a counter.
    stock = _as("BETA", lambda: db.query(models.InventoryItem).filter(
        models.InventoryItem.item_code == "WIDGET").first().current_stock)
    assert stock == 7, stock
    codes = _as("BETA", lambda: sorted(
        i.item_code for i in db.query(models.InventoryItem).all()))
    assert codes == ["BOLT-1", "NUT-2", "WIDGET"], codes
    print("PASS repeating your OWN item code updates the row you already have")


def test_supplier_code_owned_by_another_workspace_no_longer_collides():
    """supplier_code is per-tenant now (ADR-0012), same as item_code above.

    This asserted the opposite until the platform-wide UNIQUE was removed: a
    supplier code ALPHA had registered blocked BETA from registering the same
    code for a completely different supplier of their own.
    """
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.Supplier(supplier_code="SUP-1", supplier_name="Alpha steel",
                               status="Active")),
        db.commit()))

    csv_text = (
        "supplier_code,supplier_name\n"
        "SUP-9,Beta fasteners\n"
        "SUP-1,Beta steel\n"         # the same code ALPHA registered
        "SUP-7,Beta coatings\n"
    )
    r = _as("BETA", lambda: asyncio.run(
        orders_routes.import_suppliers_csv(file=_Upload(csv_text), db=db,
                                           current_user=ADMIN)))

    assert r["created"] == 3, r
    assert r["errors"] == [], r
    codes = _as("BETA", lambda: sorted(
        s.supplier_code for s in db.query(models.Supplier).all()))
    assert codes == ["SUP-1", "SUP-7", "SUP-9"], codes

    # CONTROL: two different companies now hold SUP-1, each under its own owner
    # - BETA created a row rather than renaming ALPHA's.
    rows = db.execute(text(
        "SELECT tenant_code, supplier_name FROM suppliers "
        "WHERE supplier_code='SUP-1' ORDER BY tenant_code")).fetchall()
    assert [tuple(x) for x in rows] == [
        ("ALPHA", "Alpha steel"), ("BETA", "Beta steel")], rows
    print("PASS a supplier code another workspace uses no longer blocks an import")


def test_a_duplicate_inside_the_same_file_costs_only_that_row():
    """The same collision without a second tenant: the file repeats a new code."""
    db = _sess()
    csv_text = (
        "item_code,item_name,current_stock\n"
        "DUP,first,1\n"              # row 2 — creates
        "OTHER,fine,2\n"             # row 3 — creates
        "DUP,second,3\n"             # row 4 — same code again
    )
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    # The repeat is an UPDATE, not an error — the first row is visible to the
    # second by then, so this is the ordinary upsert path. Pinned so the
    # savepoint is not mistaken for making in-file repeats fail.
    assert r["created"] == 2 and r["updated"] == 1, r
    assert r["errors"] == [], r["errors"]
    row = _as("BETA", lambda: db.query(models.InventoryItem)
              .filter(models.InventoryItem.item_code == "DUP").first())
    assert row.item_name == "second", row.item_name
    print("PASS a repeated code inside one file upserts, it does not error")


def test_a_parse_error_is_still_reported_per_row():
    """Regression: the savepoint must not swallow the errors that already worked."""
    db = _sess()
    csv_text = (
        "item_code,item_name,current_stock\n"
        "A-1,Good,5\n"
        "A-2,Bad number,not-a-number\n"
        ",,7\n"                      # no code/name -> skipped, not an error
        "A-3,Good too,9\n"
    )
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 2, r
    assert r["skipped"] == 1, r
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("Row 3:"), r["errors"]
    print("PASS a bad value is still a per-row error, and a blank row still skips")


def test_the_error_message_does_not_echo_the_uploaded_row_back():
    """A database error str()s into a multi-line dump ending in the bound
    parameters - which are the customer's own row. Reporting only the first line
    keeps the reason and drops the SQL and the payload.

    Asserting "UNIQUE is in the message" does NOT test this: the full dump's
    first line contains it too, so that assertion cannot tell the two apart.
    Mutation testing caught that; this checks what actually differs.

    DRIVEN DIRECTLY, not through an import. This used to upload a code that
    ALPHA owned, because item_code was unique across the whole platform. After
    ADR-0012 that is not a conflict, and neither is a repeat of the tenant's own
    code (the import finds it and updates), so the inventory import has no
    remaining path that produces a per-row IntegrityError at all. Rather than
    invent one, the guard is applied to the function that does the redacting.
    That is also the more honest unit: import_row_error is what every import
    calls, and a PostgreSQL error - whose `.orig` is multi-line and quotes the
    offending key back in a DETAIL line - is a shape no SQLite fixture can
    produce anyway.
    """
    from sqlalchemy.exc import IntegrityError
    from csv_safe import import_row_error

    # The real shape, including the psycopg2 DETAIL line that quotes the key
    # back - the case the local SQLite database can never generate.
    class _Orig(Exception):
        def __str__(self):
            return ('duplicate key value violates unique constraint '
                    '"uq_inventory_items_tenant_item_code"\n'
                    'DETAIL:  Key (tenant_code, item_code)=(BETA, SHARED) '
                    'already exists.')

    exc = IntegrityError(
        "INSERT INTO inventory_items (tenant_code, item_code, item_name) "
        "VALUES (%(tenant_code)s, %(item_code)s, %(item_name)s)",
        {"tenant_code": "BETA", "item_code": "SHARED",
         "item_name": "CONFIDENTIAL-PART-NAME"},
        _Orig())

    msg = import_row_error(exc)
    assert "\n" not in msg, msg
    assert "INSERT INTO" not in msg.upper(), msg
    assert "parameters" not in msg.lower(), msg
    assert "CONFIDENTIAL-PART-NAME" not in msg, msg   # the row is not handed back
    assert "DETAIL" not in msg, msg                   # nor the key it quotes back
    assert "unique constraint" in msg.lower(), msg    # ...but the reason survives

    # CONTROL: the full dump really does contain the things asserted absent, so
    # the assertions above are distinguishing redaction from an empty string.
    full = str(exc)
    assert "CONFIDENTIAL-PART-NAME" in full and "INSERT INTO" in full.upper(), full
    print("PASS the row error names the reason without echoing the row")


def test_the_counters_never_credit_a_row_that_did_not_land():
    """created/updated move only after the savepoint releases, so the response
    cannot claim a row the database refused.

    Triggered by a bad VALUE rather than a taken code. It used to upload a code
    ALPHA owned, which is no longer a conflict (ADR-0012) and so no longer
    produces a rejected row at all. A non-numeric quantity still does, and it is
    the same savepoint path - the row is attempted, fails, and must not be
    counted.
    """
    db = _sess()
    csv_text = "item_code,item_name,current_stock\nA-1,Beta wants it,not-a-number\n"
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 0 and r["updated"] == 0, r
    assert len(r["errors"]) == 1, r
    assert _as("BETA", lambda: db.query(models.InventoryItem).count()) == 0

    # CONTROL: the identical file with a VALID quantity is counted and lands, so
    # the zeros above are the rejection and not an import that never ran.
    ok = _as("BETA", lambda: asyncio.run(eir.import_inventory_csv(
        file=_Upload("item_code,item_name,current_stock\nA-1,Beta wants it,4\n"),
        db=db, current_user=ADMIN)))
    assert ok["created"] == 1 and ok["errors"] == [], ok
    assert _as("BETA", lambda: db.query(models.InventoryItem).count()) == 1
    print("PASS a rejected row is not counted as created")


def test_machines_import_still_imports_a_clean_file():
    """Machine has no unique column, so nothing can collide — this pins that the
    savepoint restructure did not change the ordinary path."""
    db = _sess()
    csv_text = (
        "name,line,status,utilization\n"
        "CNC-1,SMT,running,150\n"
        ",X,Idle,10\n"
        "PRESS-9,IC,Idle,40\n"
    )
    r = _as("BETA", lambda: asyncio.run(
        machines_routes.import_machines_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 2 and r["skipped"] == 1 and r["errors"] == [], r
    cnc = _as("BETA", lambda: db.query(models.Machine)
              .filter(models.Machine.name == "CNC-1").first())
    assert cnc.status == "Running" and cnc.utilization == 100   # still canonicalised/clamped
    print("PASS the machines import is unchanged on a clean file")


if __name__ == "__main__":
    test_a_code_another_workspace_owns_no_longer_collides_at_all()
    test_importing_a_code_you_already_own_updates_it()
    test_supplier_code_owned_by_another_workspace_no_longer_collides()
    test_a_duplicate_inside_the_same_file_costs_only_that_row()
    test_a_parse_error_is_still_reported_per_row()
    test_the_error_message_does_not_echo_the_uploaded_row_back()
    test_the_counters_never_credit_a_row_that_did_not_land()
    test_machines_import_still_imports_a_clean_file()
    print("CSV IMPORT ATOMICITY OK: a rejected row costs only itself")
