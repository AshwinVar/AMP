"""The BOM finished-goods auto-receive is bounded by MAX_QTY.

WHAT WAS WRONG ON MASTER
------------------------
`subscribers.move_bom_on_production_completed` receives a finished good when a
work order completes:

    fg_item.current_stock = (fg_item.current_stock or 0) + qty

`qty` is the completed quantity, and a work order's target_quantity /
actual_quantity carry NO upper bound (create/PATCH only reject negatives). So a
mistyped huge quantity — or a finished-goods item already near the ceiling —
drove this sum past MAX_QTY and, on PostgreSQL's INTEGER column, past
2,147,483,647 into an `integer out of range` DataError. A subscriber error
propagates by design (events.py), so that 500-ed the whole completion write;
on SQLite it stored a stock the inventory endpoints would reject, corrupting
the low-stock read-model. This is the exact overflow the PO receipt (#779),
gmats stock-in (#772) and the inventory ledger (#766) already refuse.

THE FIX
-------
The resulting finished-goods stock is bound-checked against MAX_QTY BEFORE any
stock is moved. Over the ceiling, the completion is refused with a clean 400
(HTTPException) and — because the completion handler never commits on a raised
exception — nothing persists: no finished-goods receipt and no raw-material
consumption.

These tests pin the boundary with INDEPENDENTLY-DERIVED numbers and cover the
edges: exactly at the cap (allowed), one over (refused and atomic), an
already-at-cap item, and the NULL-stock heal.

Run: DATABASE_URL="sqlite:///./ci.db" python test_bom_fg_receive_bounds.py
"""
import os
import sys

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import subscribers
import tenancy
from database import Base
from events import ProductionCompleted
from payload_fields import MAX_QTY

_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []
_open = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    _open.append(db)
    return db


def stock(db, tenant, code):
    tok = tenancy.set_current_tenant(None)
    item = db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == tenant,
        models.InventoryItem.item_code == code).first()
    tenancy.reset_current_tenant(tok)
    return None if item is None else item.current_stock


def ledger_count(db, tenant=None):
    tok = tenancy.set_current_tenant(None)
    q = db.query(models.InventoryTransaction)
    n = q.count()
    tenancy.reset_current_tenant(tok)
    return n


def seed(db, tenant, raw_start, fg_start, per_unit=1.0):
    """One raw item, one finished good, one active BOM (1 raw -> 1 FG)."""
    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code=tenant, item_code="RM-1",
                                item_name="Steel", category="Raw", unit="kg",
                                current_stock=raw_start, reorder_level=5))
    db.add(models.InventoryItem(tenant_code=tenant, item_code="FG-1",
                                item_name="Shaft", category="Finished", unit="pc",
                                current_stock=fg_start, reorder_level=0))
    b = models.BillOfMaterials(tenant_code=tenant, part_number="P-1", revision="A",
                               output_item_code="FG-1", active=True, created_by="seed")
    b.components.append(models.BomComponent(tenant_code=tenant, component_code="RM-1",
                                            quantity_per_unit=per_unit, unit="kg"))
    db.add(b)
    db.commit()
    tenancy.reset_current_tenant(tok)


def complete(db, tenant, qty, wo="WO-1"):
    """Fire the subscriber and commit, exactly as the completion handler does on
    the success path."""
    tok = tenancy.set_current_tenant(tenant)
    try:
        subscribers.move_bom_on_production_completed(
            ProductionCompleted(tenant_code=tenant, work_order_id=1, work_order_no=wo,
                                part_number="P-1", quantity=qty), db)
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)


def complete_expecting_refusal(db, tenant, qty, wo="WO-1"):
    """Fire the subscriber WITHOUT committing. On a raised HTTPException the real
    handler skips its commit, so we roll back here to mirror that, proving the
    refusal persisted nothing. Returns the status code, or None if it did not
    raise."""
    tok = tenancy.set_current_tenant(tenant)
    try:
        subscribers.move_bom_on_production_completed(
            ProductionCompleted(tenant_code=tenant, work_order_id=1, work_order_no=wo,
                                part_number="P-1", quantity=qty), db)
        db.commit()
        return None
    except HTTPException as exc:
        db.rollback()
        return exc.status_code
    finally:
        tenancy.reset_current_tenant(tok)


def test_a_completion_within_the_cap_still_receives():
    db = fresh_db()
    print("=" * 74)
    print("1. A NORMAL COMPLETION STILL RECEIVES FINISHED GOODS")
    print("=" * 74)
    # Raw 100, FG 0, consume 1/unit, make 40.
    seed(db, "T", raw_start=100, fg_start=0, per_unit=1.0)
    status = complete_expecting_refusal(db, "T", 40)
    check("a within-cap completion is NOT refused", status is None, f"status={status}")
    # Independently derived: raw 100 - 40*1 = 60, FG 0 + 40 = 40.
    check("raw stock consumed to 60", stock(db, "T", "RM-1") == 60, str(stock(db, "T", "RM-1")))
    check("finished goods received to 40", stock(db, "T", "FG-1") == 40, str(stock(db, "T", "FG-1")))
    check("two ledger rows written (1 issue + 1 receive)", ledger_count(db) == 2,
          str(ledger_count(db)))


def test_completion_exactly_at_the_cap_is_allowed():
    db = fresh_db()
    print()
    print("=" * 74)
    print("2. RESULTING STOCK EXACTLY AT MAX_QTY IS ALLOWED (<=, NOT <)")
    print("=" * 74)
    # FG starts one short of the cap; completing 1 unit lands it EXACTLY on MAX_QTY.
    seed(db, "T", raw_start=100, fg_start=MAX_QTY - 1, per_unit=1.0)
    status = complete_expecting_refusal(db, "T", 1)
    check("landing exactly on MAX_QTY is not refused", status is None, f"status={status}")
    check("finished goods received to exactly MAX_QTY",
          stock(db, "T", "FG-1") == MAX_QTY, str(stock(db, "T", "FG-1")))


def test_completion_one_over_the_cap_is_refused_and_atomic():
    db = fresh_db()
    print()
    print("=" * 74)
    print("3. ONE UNIT OVER MAX_QTY IS REFUSED — AND MOVES NOTHING")
    print("=" * 74)
    # FG starts one short of the cap; completing 2 units would reach MAX_QTY + 1.
    seed(db, "T", raw_start=100, fg_start=MAX_QTY - 1, per_unit=1.0)
    status = complete_expecting_refusal(db, "T", 2)
    check("a completion that would overflow FG stock is refused with 400",
          status == 400, f"status={status}")
    # Atomicity: the refusal rolled back, so NOTHING moved — not the finished good
    # and not the raw material consumed before the (pre-consumption) check.
    check("finished goods unchanged after the refusal",
          stock(db, "T", "FG-1") == MAX_QTY - 1, str(stock(db, "T", "FG-1")))
    check("raw material unchanged after the refusal",
          stock(db, "T", "RM-1") == 100, str(stock(db, "T", "RM-1")))
    check("no ledger row survived the refused completion", ledger_count(db) == 0,
          str(ledger_count(db)))


def test_an_already_capped_item_refuses_any_positive_completion():
    db = fresh_db()
    print()
    print("=" * 74)
    print("4. AN ITEM ALREADY AT THE CAP REFUSES ANY FURTHER RECEIPT")
    print("=" * 74)
    seed(db, "T", raw_start=100, fg_start=MAX_QTY, per_unit=1.0)
    status = complete_expecting_refusal(db, "T", 1)
    check("receiving onto an already-capped item is refused", status == 400,
          f"status={status}")
    # A zero-quantity completion moves nothing, so it is NOT an overflow and is
    # allowed even at the cap (MAX_QTY + 0 == MAX_QTY).
    status0 = complete_expecting_refusal(db, "T", 0)
    check("a zero-quantity completion at the cap is allowed", status0 is None,
          f"status={status0}")
    check("stock stays exactly at the cap", stock(db, "T", "FG-1") == MAX_QTY,
          str(stock(db, "T", "FG-1")))


def test_a_null_fg_stock_heals_to_zero_and_is_bounded():
    db = fresh_db()
    print()
    print("=" * 74)
    print("5. A NULL FINISHED-GOODS STOCK HEALS TO 0 AND IS BOUND-CHECKED")
    print("=" * 74)
    # A legacy / raw-SQL row with NULL current_stock: `(None or 0) + qty` must not
    # TypeError, and the bound check must read it as 0.
    seed(db, "T", raw_start=100, fg_start=0, per_unit=1.0)
    tok = tenancy.set_current_tenant(None)
    fg = db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == "T",
        models.InventoryItem.item_code == "FG-1").first()
    fg.current_stock = None
    db.commit()
    tenancy.reset_current_tenant(tok)

    status = complete_expecting_refusal(db, "T", 7)
    check("a NULL FG stock does not crash the completion", status is None,
          f"status={status}")
    # 0 (healed) + 7 = 7.
    check("NULL healed to 0, then received 7", stock(db, "T", "FG-1") == 7,
          str(stock(db, "T", "FG-1")))

    # And a NULL that would still overflow (impossible to reach here without a
    # huge qty) is refused: qty beyond the cap on a healed-0 item.
    db2 = fresh_db()
    seed(db2, "T", raw_start=MAX_QTY, fg_start=0, per_unit=0)  # 0-rate line => raw untouched
    tok = tenancy.set_current_tenant(None)
    fg2 = db2.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == "T",
        models.InventoryItem.item_code == "FG-1").first()
    fg2.current_stock = None
    db2.commit()
    tenancy.reset_current_tenant(tok)
    status2 = complete_expecting_refusal(db2, "T", MAX_QTY + 1)
    check("a NULL FG stock + an over-cap qty is still refused", status2 == 400,
          f"status={status2}")


def test_a_recipe_naming_an_unstocked_finished_good_moves_on():
    db = fresh_db()
    print()
    print("=" * 74)
    print("6. A RECIPE WHOSE FINISHED GOOD IS NOT STOCKED NEITHER CRASHES NOR REFUSES")
    print("=" * 74)
    # The BOM's output_item_code points at an item this tenant does not stock
    # (fg_item is None): the bound check must be skipped, the raw material still
    # consumed, and nothing 500s — a stale finished-goods reference is not a
    # reason to abort a completion or refuse it.
    seed(db, "T", raw_start=100, fg_start=0, per_unit=1.0)
    tok = tenancy.set_current_tenant(None)
    db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == "T",
        models.InventoryItem.item_code == "FG-1").delete()
    db.commit()
    tenancy.reset_current_tenant(tok)

    status = complete_expecting_refusal(db, "T", 40)
    check("a missing finished-goods item does not refuse the completion",
          status is None, f"status={status}")
    check("the raw material was still consumed (100 - 40)",
          stock(db, "T", "RM-1") == 60, str(stock(db, "T", "RM-1")))
    check("one ledger row written (the issue only, no receive)", ledger_count(db) == 1,
          str(ledger_count(db)))


if __name__ == "__main__":
    test_a_completion_within_the_cap_still_receives()
    test_completion_exactly_at_the_cap_is_allowed()
    test_completion_one_over_the_cap_is_refused_and_atomic()
    test_an_already_capped_item_refuses_any_positive_completion()
    test_a_null_fg_stock_heals_to_zero_and_is_bounded()
    test_a_recipe_naming_an_unstocked_finished_good_moves_on()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE BOM FINISHED-GOODS RECEIVE IS BOUNDED BY MAX_QTY")
