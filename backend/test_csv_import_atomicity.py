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


def test_inventory_import_keeps_the_good_rows_when_one_code_is_taken():
    """THE BUG. A code owned by another workspace used to cost the whole file."""
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.InventoryItem(item_code="WIDGET", item_name="Alpha widget",
                                    category="Raw", unit="pc", current_stock=5,
                                    reorder_level=1)),
        db.commit()))

    csv_text = (
        "item_code,item_name,current_stock\n"
        "BOLT-1,Beta bolt,10\n"      # file row 2 — valid
        "WIDGET,Beta widget,7\n"     # file row 3 — collides with ALPHA
        "NUT-2,Beta nut,3\n"         # file row 4 — valid
    )
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 2, r          # the two good rows, not 3 and not 0
    assert len(r["errors"]) == 1, r
    # Blamed on its OWN line. Before the fix the autoflush surfaced on the NEXT
    # row, so the message accused row 4 — a row with nothing wrong with it.
    assert r["errors"][0].startswith("Row 3:"), r["errors"]
    assert "UNIQUE" in r["errors"][0].upper(), r["errors"]

    codes = _as("BETA", lambda: sorted(
        i.item_code for i in db.query(models.InventoryItem).all()))
    assert codes == ["BOLT-1", "NUT-2"], codes
    # ALPHA's row is untouched.
    assert db.execute(text(
        "SELECT COUNT(*) FROM inventory_items WHERE tenant_code='ALPHA'")).scalar() == 1
    print("PASS inventory import keeps the good rows and blames the right one")


def test_supplier_import_keeps_the_good_rows_when_one_code_is_taken():
    """supplier_code is UNIQUE globally too, and its lookup is equally scoped."""
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.Supplier(supplier_code="SUP-1", supplier_name="Alpha steel",
                               status="Active")),
        db.commit()))

    csv_text = (
        "supplier_code,supplier_name\n"
        "SUP-9,Beta fasteners\n"     # row 2 — valid
        "SUP-1,Beta steel\n"         # row 3 — collides with ALPHA
        "SUP-7,Beta coatings\n"      # row 4 — valid
    )
    r = _as("BETA", lambda: asyncio.run(
        orders_routes.import_suppliers_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 2, r
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("Row 3:"), r["errors"]
    codes = _as("BETA", lambda: sorted(s.supplier_code for s in db.query(models.Supplier).all()))
    assert codes == ["SUP-7", "SUP-9"], codes
    print("PASS supplier import keeps the good rows and blames the right one")


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
    parameters — which are the customer's own row. Reporting only the first line
    keeps the reason and drops the SQL and the payload.

    Asserting "UNIQUE is in the message" does NOT test this: the full dump's
    first line contains it too, so that assertion cannot tell the two apart.
    Mutation testing caught that; this checks what actually differs.
    """
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.InventoryItem(item_code="SHARED", item_name="Alpha part",
                                    category="Raw", unit="pc", current_stock=1,
                                    reorder_level=0)),
        db.commit()))
    csv_text = "item_code,item_name,current_stock\nSHARED,CONFIDENTIAL-PART-NAME,4\n"
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    msg = r["errors"][0]
    assert "\n" not in msg, msg
    assert "INSERT INTO" not in msg.upper(), msg
    assert "parameters" not in msg.lower(), msg
    assert "CONFIDENTIAL-PART-NAME" not in msg, msg      # the row is not handed back
    assert "UNIQUE constraint failed" in msg, msg        # ...but the reason survives
    print("PASS the row error names the reason without echoing the row")


def test_the_counters_never_credit_a_row_that_did_not_land():
    """created/updated move only after the savepoint releases, so the response
    cannot claim a row the database refused."""
    db = _sess()
    _as("ALPHA", lambda: (
        db.add(models.InventoryItem(item_code="TAKEN", item_name="Alpha", category="Raw",
                                    unit="pc", current_stock=1, reorder_level=0)),
        db.commit()))
    csv_text = "item_code,item_name,current_stock\nTAKEN,Beta wants it,4\n"
    r = _as("BETA", lambda: asyncio.run(
        eir.import_inventory_csv(file=_Upload(csv_text), db=db, current_user=ADMIN)))

    assert r["created"] == 0 and r["updated"] == 0, r
    assert len(r["errors"]) == 1, r
    assert _as("BETA", lambda: db.query(models.InventoryItem).count()) == 0
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
    test_inventory_import_keeps_the_good_rows_when_one_code_is_taken()
    test_supplier_import_keeps_the_good_rows_when_one_code_is_taken()
    test_a_duplicate_inside_the_same_file_costs_only_that_row()
    test_a_parse_error_is_still_reported_per_row()
    test_the_error_message_does_not_echo_the_uploaded_row_back()
    test_the_counters_never_credit_a_row_that_did_not_land()
    test_machines_import_still_imports_a_clean_file()
    print("CSV IMPORT ATOMICITY OK: a rejected row costs only itself")
