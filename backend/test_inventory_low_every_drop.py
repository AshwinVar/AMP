"""Every write that takes an item below its reorder level tells the Reorder agent.

THE DEFECT
----------
InventoryLow is the event the Reorder agent proposes a purchase order from
(ADR-0005). It was published by exactly two writers: the inventory ledger
(POST /inventory/transactions) and BOM consumption when a work order completes.
Every other write that lowers an item's stock was silent:

  * issuing a material issue slip, the enterprise inventory's main way of taking
    stock off the shelf;
  * approving a cycle count with a negative variance;
  * correcting a purchase order's received quantity downwards;
  * PATCH /inventory/items/{id} setting a lower stock;
  * the CSV import overwriting an item's stock.

So an item drawn down by issue slips could sit below its reorder level with no
proposal ever made. The two writers that did publish also carried two copies of
the crossing rule, which disagreed about an item with no reorder level.

THE RULE
--------
stock_events.stock_dropped(db, item, before) is the one crossing rule: publish
when this write took the item from above its reorder level to at or below it.
An item already below does not re-announce, and an item with no level announces
nothing. Every writer of current_stock calls it; a structural guard below fails
a new one that does not.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_inventory_low_every_drop.py
"""
import ast
import asyncio
import json
import os
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import inventory_routes
import models
import orders_routes
import schemas
import tenancy as T
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN = {"sub": "admin", "role": "Admin", "tenant": "BETA"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class _Upload:
    def __init__(self, text_):
        self._data = text_.encode("utf-8")

    async def read(self):
        return self._data


def _sess():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def beta(fn):
    tok = T.set_current_tenant("BETA")
    try:
        return fn()
    finally:
        T.reset_current_tenant(tok)


def item(db, code, stock, level=5):
    def make():
        row = models.InventoryItem(item_code=code, item_name=code, category="c", unit="pcs",
                                   current_stock=stock, reorder_level=level)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    return beta(make)


def lows(db, code):
    """InventoryLow events published for this item (EventLog is where publish writes)."""
    rows = beta(lambda: db.query(models.EventLog).filter(models.EventLog.event_type == "InventoryLow").all())
    return [r for r in rows if json.loads(r.payload).get("item_code") == code]


# ── the writers ──────────────────────────────────────────────────────────────

def via_ledger(db, it, to):
    beta(lambda: inventory_routes.create_inventory_transaction(schemas.InventoryTransactionCreate(
        item_id=it.id, transaction_type="Issue", quantity=it.current_stock - to), db=db, current_user=ADMIN))


def via_patch(db, it, to):
    beta(lambda: inventory_routes.update_inventory_item(
        it.id, schemas.InventoryItemUpdate(current_stock=to), db=db, current_user=ADMIN))


def via_issue_slip(db, it, to):
    def run():
        slip = eir.create_issue_slip({"item_id": it.id, "requested_qty": it.current_stock - to},
                                     db=db, current_user=ADMIN)
        sid = slip["id"] if isinstance(slip, dict) else slip.id
        eir.approve_issue_slip(sid, db=db, current_user=ADMIN)
        eir.issue_slip(sid, db=db, current_user=ADMIN)
    beta(run)


def via_cycle_count(db, it, to):
    def run():
        count = eir.create_cycle_count({"items": [{"item_id": it.id, "physical_qty": to}]},
                                       db=db, current_user=ADMIN)
        cid = count["id"] if isinstance(count, dict) else count.id
        eir.approve_cycle_count(cid, db=db, current_user=ADMIN)
    beta(run)


def via_po_correction(db, it, to):
    """A receipt can only be corrected back towards where stock stood before it, so
    this writer starts from an empty shelf (START below): a receipt of 10 lifts it
    above the reorder level, and correcting that receipt down to `to` crosses it."""
    def run():
        sup = models.Supplier(supplier_code=f"S-{it.item_code}", supplier_name="Supplier")
        db.add(sup)
        db.commit()
        po = orders_routes.create_purchase_order(schemas.PurchaseOrderCreate(
            po_no=f"PO-{it.item_code}", supplier_id=sup.id, item_id=it.id, item_name=it.item_name,
            order_quantity=100, unit="pcs", expected_delivery_date=date(2026, 12, 1)),
            db=db, current_user=ADMIN)
        orders_routes.update_purchase_order(po.id, schemas.PurchaseOrderUpdate(received_quantity=10),
                                            db=db, current_user=ADMIN)
        orders_routes.update_purchase_order(po.id, schemas.PurchaseOrderUpdate(received_quantity=to),
                                            db=db, current_user=ADMIN)
    beta(run)


def via_csv(db, it, to):
    text = f"item_code,item_name,current_stock,reorder_level\n{it.item_code},{it.item_name},{to},{it.reorder_level}\n"
    beta(lambda: asyncio.run(eir.import_inventory_csv(file=_Upload(text), db=db, current_user=ADMIN)))


# (label, writer, starting stock): each takes the item from above 5 to 3.
WRITERS = [("the ledger (already published: the control)", via_ledger, 10),
           ("PATCH /inventory/items", via_patch, 10), ("an issue slip", via_issue_slip, 10),
           ("a cycle count", via_cycle_count, 10),
           ("a purchase-order receipt correction", via_po_correction, 0), ("the CSV import", via_csv, 10)]


def section_every_writer_announces_a_crossing():
    print("=" * 74)
    print("1. EVERY WRITER THAT TAKES STOCK BELOW ITS REORDER LEVEL ANNOUNCES IT, ONCE")
    print("=" * 74)
    for label, write, start in WRITERS:
        db = _sess()
        code = label.split()[1].upper()[:8] + "-X"
        it = item(db, code, stock=start, level=5)
        try:
            write(db, it, 3)
        except Exception as e:     # a writer that raises is a failure of this check, not a crash
            check(f"{label}: down to 3 across a reorder level of 5", False, repr(e)[:160])
            db.close()
            continue
        stock = beta(lambda: db.query(models.InventoryItem).filter(models.InventoryItem.id == it.id).one().current_stock)
        got = lows(db, code)
        check(f"{label}: down to 3 across a reorder level of 5 publishes one InventoryLow",
              stock == 3 and len(got) == 1, f"stock {stock}, {len(got)} events")
        db.close()


def section_only_a_crossing():
    print()
    print("=" * 74)
    print("2. ONLY A CROSSING: NOT A DROP THAT STAYS ABOVE, NOT ONE ALREADY BELOW")
    print("=" * 74)
    for label, write in (("an issue slip", via_issue_slip), ("PATCH /inventory/items", via_patch)):
        db = _sess()
        above = item(db, "ABOVE", stock=10, level=5)
        write(db, above, 7)
        check(f"{label}: 10 -> 7 (still above 5) publishes nothing", not lows(db, "ABOVE"))
        below = item(db, "BELOW", stock=4, level=5)
        write(db, below, 2)
        check(f"{label}: 4 -> 2 (already below 5) publishes nothing again", not lows(db, "BELOW"))
        exact = item(db, "EXACT", stock=10, level=5)
        write(db, exact, 5)
        check(f"{label}: 10 -> 5 (exactly at its level) is low, as the low-stock view counts it",
              len(lows(db, "EXACT")) == 1, str(len(lows(db, "EXACT"))))
        db.close()

    # NO LEVEL. The constructor gives a NULL the column default of 0, and PATCH heals a
    # NULL level to 0 before the rule runs, so a real NULL is forced in SQL and taken
    # through an issue slip, which heals nothing.
    db = _sess()
    none = item(db, "NOLEVEL", stock=10, level=5)
    beta(lambda: (db.execute(text("UPDATE inventory_items SET reorder_level = NULL WHERE id = :i"),
                             {"i": none.id}), db.commit(), db.expire_all()))
    check("CONTROL: the item really has no reorder level",
          beta(lambda: db.query(models.InventoryItem).filter(models.InventoryItem.id == none.id)
               .one().reorder_level) is None)
    via_issue_slip(db, beta(lambda: db.query(models.InventoryItem).filter(
        models.InventoryItem.id == none.id).one()), 0)
    check("an item with no reorder level publishes nothing, even emptied", not lows(db, "NOLEVEL"))
    db.close()


# Writers of current_stock that are not a stock DROP a Reorder agent should hear
# about, each with its reason. Anything else that assigns current_stock must call
# stock_events.stock_dropped.
EXEMPT = {
    "accept_grn": "a goods receipt only ever adds stock",
    "create_inventory_item": "creates the item; there is no 'before' to cross from",
    "import_inventory_csv": None,           # must call it (the update path)
    "move_bom_on_production_completed": None,   # must call it
}
SKIP_FILES = ("test_", "audit_", "mutate_", "verify_", "seed", "reset_factory", "demo_aeron",
              "onboard_tenant", "factory_simulator", "reseed_inventory", "loadtest", "restore_drill",
              "preflight_backfill_245", "dashboard_perf")


def section_one_rule():
    print()
    print("=" * 74)
    print("3. EVERY FUNCTION THAT WRITES current_stock CALLS THE ONE RULE")
    print("=" * 74)
    offenders, writers = [], []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name.startswith(SKIP_FILES):
            continue
        tree = ast.parse(open(os.path.join(HERE, name), encoding="utf-8").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns = any(isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "current_stock" for t in n.targets)
                for n in ast.walk(fn))
            if not assigns:
                continue
            writers.append(f"{name}:{fn.name}")
            calls = any(isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None))
                        == "stock_dropped" for n in ast.walk(fn))
            if not calls and not EXEMPT.get(fn.name):
                offenders.append(f"{name}:{fn.name}")
    check(f"the scan found the stock writers ({len(writers)})", len(writers) >= 7, str(writers))
    check("each calls stock_events.stock_dropped, or is exempt with a reason", not offenders, str(offenders))
    rules = [n for n in sorted(os.listdir(HERE)) if n.endswith(".py") and not n.startswith(SKIP_FILES)
             and "InventoryLow(" in open(os.path.join(HERE, n), encoding="utf-8").read()]
    check("InventoryLow is constructed in one module, the rule's", rules == ["stock_events.py"], str(rules))


def main():
    section_every_writer_announces_a_crossing()
    section_only_a_crossing()
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


def test_inventory_low_every_drop():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
