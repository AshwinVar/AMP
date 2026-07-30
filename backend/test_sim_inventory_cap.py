"""The simulator's inventory-consumption cap must actually bound the ledger.

``factory_simulator.tick_inventory`` runs in the live background loop
(``run_simulation`` -> ``main._simulation_loop``). Its docstring promises the
auto-consumption is "capped at 120 auto transactions", implemented as: count the
rows this ticker has written, and stop issuing once that reaches the cap.

The cap was DEAD. It counted ``InventoryTransaction`` rows whose
``notes == "Auto-issued by simulator"`` — a string NOTHING in the codebase ever
writes (the row the ticker adds carries ``notes="Issued to production line"``,
and the only other auto-issuer, ``subscribers.py``, writes ``"Auto-issued for
WO …"``). So the count was permanently 0, ``count >= 120`` never fired, and a
long-running deployment kept issuing forever: raw-material stock drained to its
floor and ``inventory_transactions`` (a growing table) grew without bound — all
of it surfacing on the live inventory / low-stock / recommendation read paths.

The fix binds the guard's filter and the write to one shared constant
(``_SIM_ISSUE_NOTE``) so the two literals can never drift apart again. These
tests pin the contract that was broken:

  * the rows the ticker writes are the rows the guard counts (parity);
  * the cap genuinely bounds the row count under sustained ticking;
  * a single tick drains exactly the issued quantity and floors stock at 0;
  * no eligible raw material -> a clean no-op, not a crash.

Run:  python backend/test_sim_inventory_cap.py     (exit 0 = pass)
"""
import random

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from factory_simulator import tick_inventory, _SIM_ISSUE_NOTE, _SIM_ISSUE_CAP


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _raw_item(stock, code="RAW-1"):
    return models.InventoryItem(
        item_code=code, item_name="Copper Wire", category="Raw Material",
        unit="m", current_stock=stock, reorder_level=10, tenant_code="DEFAULT",
    )


def _guard_visible_count(db):
    """Recompute the guard's own count — the rows tick_inventory believes it has
    written. If the write note and the guard filter drift apart (the bug), this
    stays 0 no matter how many rows the ticker actually adds."""
    return (db.query(models.InventoryTransaction)
            .filter(models.InventoryTransaction.notes == _SIM_ISSUE_NOTE)
            .count())


def test_written_row_is_the_row_the_guard_counts():
    """The essential regression: after one tick, the row written is visible to
    the guard's own filter. Under the old code the ticker wrote
    'Issued to production line' while the guard counted 'Auto-issued by
    simulator', so this count was 0 despite a real write."""
    db = _fresh_session()
    db.add(_raw_item(stock=100))
    db.commit()

    tick_inventory(db)

    assert db.query(models.InventoryTransaction).count() == 1, "one row should be written"
    assert _guard_visible_count(db) == 1, (
        "the written row must carry the note the cap counts (guard/write parity)")
    print("PASS the row the ticker writes is the row its cap counts")


def test_cap_bounds_the_ledger_under_sustained_ticking():
    """Tick far more times than the cap, against an item with effectively
    unlimited stock, and the sim-issued row count must stop at exactly the cap —
    not grow unbounded. With the dead guard this reached the tick count (200)."""
    random.seed(1234)
    db = _fresh_session()
    # 10M units: each tick removes <= 25, so 200 ticks (<= 5000) never exhausts
    # the > 20 eligibility threshold — the ONLY thing that may stop issuing is the
    # cap under test.
    db.add(_raw_item(stock=10_000_000))
    db.commit()

    ticks = 200
    assert ticks > _SIM_ISSUE_CAP, "the loop must exceed the cap to prove it fires"
    for _ in range(ticks):
        tick_inventory(db)

    total = db.query(models.InventoryTransaction).count()
    assert total == _SIM_ISSUE_CAP, (
        f"cap should bound the ledger at {_SIM_ISSUE_CAP}, got {total}")
    print(f"PASS {ticks} ticks are bounded to {total} rows (cap = {_SIM_ISSUE_CAP})")


def test_cap_boundary_lets_the_last_row_through():
    """Boundary: with exactly cap-1 sim rows already present, one more tick is
    allowed (reaching the cap); a further tick is refused. Pins the '>=' edge."""
    db = _fresh_session()
    db.add(_raw_item(stock=10_000))
    db.commit()
    # Seed cap-1 pre-existing sim rows carrying the counted note.
    for i in range(_SIM_ISSUE_CAP - 1):
        db.add(models.InventoryTransaction(
            item_id=1, transaction_type="Issue", quantity=1,
            reference="Production", notes=_SIM_ISSUE_NOTE, tenant_code="DEFAULT"))
    db.commit()

    tick_inventory(db)                         # cap-1 -> cap: allowed
    assert db.query(models.InventoryTransaction).count() == _SIM_ISSUE_CAP

    tick_inventory(db)                         # already at cap: refused
    assert db.query(models.InventoryTransaction).count() == _SIM_ISSUE_CAP
    print(f"PASS boundary: the {_SIM_ISSUE_CAP}th row is admitted, the next refused")


def test_single_tick_drains_exactly_the_issued_quantity_and_floors_at_zero():
    """A tick removes exactly the quantity it records, and stock is floored at 0
    (never negative). Stock-after is derived independently from stock-before minus
    the transaction's own recorded quantity."""
    random.seed(7)
    db = _fresh_session()
    # Just above the > 20 eligibility floor, so a large issue (up to 25) can push
    # stock to the max(0, ...) floor rather than negative.
    db.add(_raw_item(stock=21))
    db.commit()

    tick_inventory(db)

    txn = db.query(models.InventoryTransaction).one()
    assert 5 <= txn.quantity <= 25, txn.quantity
    item = db.query(models.InventoryItem).one()
    expected = max(0, 21 - txn.quantity)       # independent of the code's own math
    assert item.current_stock == expected, (item.current_stock, expected)
    assert item.current_stock >= 0, "stock must never go negative"
    print(f"PASS one tick issues {txn.quantity}, stock 21 -> {item.current_stock} (floored at 0)")


def test_no_eligible_item_is_a_clean_no_op():
    """No raw material above the threshold -> no write, no exception. Covers the
    empty-candidate edge (only finished goods, and a raw item at/below the > 20
    threshold)."""
    db = _fresh_session()
    db.add(models.InventoryItem(
        item_code="FG-1", item_name="Widget", category="Finished Good",
        unit="pcs", current_stock=999, reorder_level=10, tenant_code="DEFAULT"))
    db.add(_raw_item(stock=20, code="RAW-LOW"))   # 20 is NOT > 20 -> ineligible
    db.commit()

    tick_inventory(db)                             # must not raise

    assert db.query(models.InventoryTransaction).count() == 0
    print("PASS no eligible raw material -> clean no-op (no row, no crash)")


if __name__ == "__main__":
    test_written_row_is_the_row_the_guard_counts()
    test_cap_bounds_the_ledger_under_sustained_ticking()
    test_cap_boundary_lets_the_last_row_through()
    test_single_tick_drains_exactly_the_issued_quantity_and_floors_at_zero()
    test_no_eligible_item_is_a_clean_no_op()
    print("ALL SIM INVENTORY-CAP TESTS PASSED")
