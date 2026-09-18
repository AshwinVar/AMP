"""An item's stock and reorder level are whole numbers in [0, MAX_QTY] on EVERY write.

THE DEFECT
----------
PATCH /inventory/items/{id} refused a negative stock or reorder level. Its own
comment says why: a -5 stock is physically impossible and corrupts the
low-stock read-model (an escalation labelled "Medium" that prints "Current stock
-5"). It also claims "parity with the create-path ingests". The two other ways
the same columns are written did not refuse anything:

  * POST /inventory/items took any integer (schemas.InventoryItemCreate is a
    plain int), so the item could be CREATED at -5;
  * the enterprise CSV import parsed both cells with a bare int(float(cell)), so
    "-5" was stored and "1e20" was widened into a 21-digit stock. The GMATS
    importer has used the bounded payload_fields.int_cell since #440.

THE RULE
--------
inventory_routes.check_stock_levels is the bound for a JSON write (create and
PATCH) and int_cell is the same bound for a CSV cell. A refused CSV row is
reported against its line and the rest of the file still imports.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_inventory_stock_bounds.py
"""
import ast
import asyncio
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import inventory_routes
import models
import schemas
import tenancy as T
from database import Base
from payload_fields import MAX_QTY

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN = {"sub": "admin", "role": "Admin", "tenant": "BETA"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class _Upload:
    """Stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text_):
        self._data = text_.encode("utf-8")

    async def read(self):
        return self._data


def _sess():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _as_beta(fn):
    tok = T.set_current_tenant("BETA")
    try:
        return fn()
    finally:
        T.reset_current_tenant(tok)


def _refused(fn):
    try:
        fn()
    except HTTPException as e:
        return e.status_code == 400
    return False


def _item(code, **over):
    fields = dict(item_code=code, item_name=code, category="c", unit="pcs")
    fields.update(over)
    return schemas.InventoryItemCreate(**fields)


def section_create():
    print("=" * 74)
    print("1. POST /inventory/items REFUSES WHAT PATCH REFUSES")
    print("=" * 74)
    db = _sess()
    for field, bad in (("current_stock", -5), ("reorder_level", -1),
                       ("current_stock", MAX_QTY + 1), ("reorder_level", MAX_QTY + 1)):
        check(f"create with {field}={bad} is refused (400)",
              _refused(lambda: _as_beta(lambda: inventory_routes.create_inventory_item(
                  _item(f"B-{field}-{bad}", **{field: bad}), db=db, current_user=ADMIN))))
    stored = _as_beta(lambda: db.query(models.InventoryItem).count())
    check("...and nothing was stored", stored == 0, str(stored))
    ok = _as_beta(lambda: inventory_routes.create_inventory_item(
        _item("EDGE", current_stock=MAX_QTY, reorder_level=0), db=db, current_user=ADMIN))
    check("CONTROL: the edges themselves (0 and MAX_QTY) are accepted",
          (ok.current_stock, ok.reorder_level) == (MAX_QTY, 0))
    db.close()


def section_patch():
    print()
    print("=" * 74)
    print("2. PATCH KEEPS ITS FLOOR AND GAINS THE SAME CEILING")
    print("=" * 74)
    db = _sess()
    item = _as_beta(lambda: inventory_routes.create_inventory_item(
        _item("P-1", current_stock=10, reorder_level=2), db=db, current_user=ADMIN))
    for field, bad in (("current_stock", -5), ("current_stock", MAX_QTY + 1),
                       ("reorder_level", MAX_QTY + 1)):
        check(f"PATCH {field}={bad} is refused (400)",
              _refused(lambda: _as_beta(lambda: inventory_routes.update_inventory_item(
                  item.id, schemas.InventoryItemUpdate(**{field: bad}), db=db, current_user=ADMIN))))
        db.rollback()
    fresh = _as_beta(lambda: db.query(models.InventoryItem).filter(models.InventoryItem.id == item.id).one())
    check("...and the stored stock is unchanged", (fresh.current_stock, fresh.reorder_level) == (10, 2),
          f"{fresh.current_stock}/{fresh.reorder_level}")
    db.close()


def section_csv():
    print()
    print("=" * 74)
    print("3. THE ENTERPRISE CSV IMPORT SKIPS A ROW IT CANNOT STORE, AND KEEPS THE REST")
    print("=" * 74)
    db = _sess()
    _as_beta(lambda: db.add(models.InventoryItem(item_code="KEEP", item_name="Keep", category="c",
                                                 unit="pcs", current_stock=7, reorder_level=1)))
    _as_beta(db.commit)
    csv_text = ("item_code,item_name,current_stock,reorder_level\n"
                "OK1,Good,5,1\n"
                "NEG,Negative,-5,0\n"
                "HUGE,Huge,1e20,0\n"
                "DEC,Decimal,5.0,2\n"
                "NEGR,Negative reorder,3,-1\n"
                "BLANK,Blank,,\n"
                "KEEP,Keep,-3,1\n")
    r = _as_beta(lambda: asyncio.run(eir.import_inventory_csv(file=_Upload(csv_text), db=db,
                                                              current_user=ADMIN)))
    rows = {i.item_code: (i.current_stock, i.reorder_level)
            for i in _as_beta(lambda: db.query(models.InventoryItem).all())}
    check("valid rows import: 5, the decimal 5.0 as 5, and a blank cell as 0",
          rows.get("OK1") == (5, 1) and rows.get("DEC") == (5, 2) and rows.get("BLANK") == (0, 0),
          str(rows))
    check("a negative stock, a negative reorder level and 1e20 are not stored",
          not {"NEG", "HUGE", "NEGR"} & set(rows), str(sorted(rows)))
    check("an existing item is not overwritten with a negative stock", rows.get("KEEP") == (7, 1),
          str(rows.get("KEEP")))
    reported = " | ".join(r["errors"])
    check("each refused row is reported against its line",
          all(f"Row {n}" in reported for n in (3, 4, 6, 8)), reported)
    db.close()


def section_one_rule():
    print()
    print("=" * 74)
    print("4. ONE RULE FOR EACH KIND OF WRITE")
    print("=" * 74)
    routes = ast.parse(open(os.path.join(HERE, "inventory_routes.py"), encoding="utf-8").read())
    fns = {f.name: f for f in ast.walk(routes) if isinstance(f, ast.FunctionDef)}

    def calls(fn, name):
        return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == name
                   for n in ast.walk(fn))

    check("create and PATCH both call check_stock_levels",
          calls(fns["create_inventory_item"], "check_stock_levels")
          and calls(fns["update_inventory_item"], "check_stock_levels"))
    importer = ast.parse(open(os.path.join(HERE, "enterprise_inventory_routes.py"), encoding="utf-8").read())
    imp = next(f for f in ast.walk(importer)
               if isinstance(f, ast.AsyncFunctionDef) and f.name == "import_inventory_csv")
    bare = [n.lineno for n in ast.walk(imp)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "int"]
    check("the CSV import parses quantities through int_cell, never a bare int()",
          calls(imp, "int_cell") and not bare, f"bare int() at lines {bare}")


def main():
    section_create()
    section_patch()
    section_csv()
    section_one_rule()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_inventory_stock_bounds():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
