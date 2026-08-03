"""Simulator progression ticks must survive NULL integer counts.

The background sim loop (``main._simulation_loop``) runs a handful of
progression ticks against whatever rows are in the DB. Three of them increment
a count column that is ``Column(Integer, default=0)`` WITHOUT ``nullable=False``
— so a row written by raw SQL, a migration, or an update that clears the field
can hold a true NULL:

  * ``tick_work_order_progress`` -> ``WorkOrder.actual_quantity``
  * ``tick_operator``            -> ``OperatorJobExecution.good_count``
  * ``tick_customer_order``      -> ``CustomerOrder.dispatched_quantity``

``None < target`` / ``None + int`` raised ``TypeError``, and because
``tick_work_order_progress`` runs first and unconditionally in the loop, that
rolled back the WHOLE per-tenant tick every 45s — every other tick's writes
lost with it. This is the exact failure the utilization-drift fix (#341) cured
for the same loop; these three sibling counts were missed. Each now coalesces a
missing count to the column's own default of 0 (healing the NULL on write).

A genuine NULL can't be inserted through the ORM (the Column default coerces an
explicit ``None`` at flush), so — as ``test_sim_calibration`` does for the
utilization case — we reproduce the real threat model with a raw ``UPDATE`` that
NULLs the column.

Run:  python backend/test_sim_null_counts.py     (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from factory_simulator import (
    tick_customer_order,
    tick_operator,
    tick_work_order_progress,
)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _null_column(db, table, column, row_id):
    """Force a genuine SQL NULL the way raw SQL / a migration would (the ORM
    default coerces an explicit None at flush, so we can't do it through the ORM)."""
    db.execute(text(f"UPDATE {table} SET {column} = NULL WHERE id = :i"), {"i": row_id})
    db.commit()
    db.expire_all()


# ── tick_work_order_progress ──────────────────────────────────────


def test_work_order_progress_survives_null_actual_quantity():
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    wo = models.WorkOrder(
        work_order_no="WO-1", part_number="P", batch_number="B",
        machine_id=1, target_quantity=1000, actual_quantity=50,
        status="In Progress", tenant_code="DEFAULT",
    )
    db.add(wo)
    db.commit()
    _null_column(db, "work_orders", "actual_quantity", wo.id)
    assert db.query(models.WorkOrder).one().actual_quantity is None

    tick_work_order_progress(db)                       # must not TypeError / roll back

    healed = db.query(models.WorkOrder).one()
    # NULL -> 0, then + random(8, 35): the healed value lands in [8, 35] and,
    # being far below the 1000 target, the WO stays In Progress. Independently
    # derived from the coalesce (0) and the increment bounds, not the old value.
    assert healed.actual_quantity is not None
    assert 8 <= healed.actual_quantity <= 35, healed.actual_quantity
    assert healed.status == "In Progress", healed.status
    print(f"PASS work-order tick heals NULL actual_quantity to {healed.actual_quantity} (no crash)")


def test_work_order_progress_completes_from_null_when_target_is_tiny():
    """The completion branch must also be reachable from a healed NULL: with a
    target of 5, 0 + random(8, 35) is >= 5, so min(5, ...) caps at exactly 5 and
    the WO flips to Completed."""
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    wo = models.WorkOrder(
        work_order_no="WO-2", part_number="P", batch_number="B",
        machine_id=1, target_quantity=5, actual_quantity=1,
        status="In Progress", tenant_code="DEFAULT",
    )
    db.add(wo)
    db.commit()
    _null_column(db, "work_orders", "actual_quantity", wo.id)

    tick_work_order_progress(db)

    healed = db.query(models.WorkOrder).one()
    assert healed.actual_quantity == 5, healed.actual_quantity
    assert healed.status == "Completed", healed.status
    print("PASS work-order tick completes cleanly from a NULL actual_quantity")


def test_work_order_progress_survives_null_target_quantity():
    """target_quantity is nullable=False WITHOUT a default, so a raw-SQL / migration
    / legacy row can hold a genuine NULL (the exact case the #471 route fix defends).
    The tick's `actual < wo.target_quantity` (int < None) and `min(wo.target_quantity,
    ...)` (min(None, int)) both raised TypeError, rolling back the whole per-tenant
    tick. With target coalesced to 0, `actual (0) < 0` is False, so the increment
    branch is skipped and the WO is left In Progress — no crash, status untouched.

    SQLite enforces the nullable=False NOT NULL a pre-existing row predates, so a real
    on-disk NULL is impossible here (a raw UPDATE ... SET target_quantity = NULL is
    rejected). set_committed_value reproduces the exact attribute state a loaded
    legacy-NULL row presents (target_quantity reads None) WITHOUT marking the instance
    dirty, so the tick's own commit never tries to persist the NULL — the same
    "reproduce the loaded state, don't write it" approach test_work_orders_routes uses
    for this column's route-side fix (#471)."""
    from sqlalchemy.orm.attributes import set_committed_value

    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    wo = models.WorkOrder(
        work_order_no="WO-N", part_number="P", batch_number="B",
        machine_id=1, target_quantity=1000, actual_quantity=200,
        status="In Progress", tenant_code="DEFAULT",
    )
    db.add(wo)
    db.commit()
    set_committed_value(wo, "target_quantity", None)   # loaded legacy-NULL state
    assert db.query(models.WorkOrder).one().target_quantity is None

    tick_work_order_progress(db)                       # must not TypeError / roll back

    healed = db.query(models.WorkOrder).one()
    # No known target -> the progression branch is skipped, so actual_quantity is
    # left exactly as recorded (200) and the WO stays In Progress. Independently
    # derived from the "no known target, leave untouched" rule, not the crash path.
    assert healed.actual_quantity == 200, healed.actual_quantity
    assert healed.status == "In Progress", healed.status
    print("PASS work-order tick survives a NULL target_quantity (no crash, WO untouched)")


def test_work_order_progress_preserves_normal_increment():
    """Control: with a real (non-NULL) count the coalesce is a no-op — the tick
    still advances by random(8, 35) from the recorded value, so 50 -> [58, 85]."""
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    db.add(models.WorkOrder(
        work_order_no="WO-3", part_number="P", batch_number="B",
        machine_id=1, target_quantity=1000, actual_quantity=50,
        status="In Progress", tenant_code="DEFAULT",
    ))
    db.commit()

    tick_work_order_progress(db)

    healed = db.query(models.WorkOrder).one()
    assert 58 <= healed.actual_quantity <= 85, healed.actual_quantity
    print(f"PASS work-order tick preserves normal progression (50 -> {healed.actual_quantity})")


# ── tick_operator ─────────────────────────────────────────────────


def test_operator_tick_survives_null_good_count():
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.commit()
    job = models.OperatorJobExecution(
        execution_no="EXE-1", operator_name="Sam", machine_id=1,
        job_status="In Progress", good_count=10, rejected_count=0,
        tenant_code="DEFAULT",
    )
    db.add(job)
    db.commit()
    _null_column(db, "operator_job_executions", "good_count", job.id)
    assert db.query(models.OperatorJobExecution).one().good_count is None

    tick_operator(db)                                  # must not TypeError / roll back

    healed = db.query(models.OperatorJobExecution).one()
    # NULL -> 0, then + random(5, 20) -> [5, 20].
    assert healed.good_count is not None
    assert 5 <= healed.good_count <= 20, healed.good_count
    print(f"PASS operator tick heals NULL good_count to {healed.good_count} (no crash)")


# ── tick_customer_order ───────────────────────────────────────────


def test_customer_order_tick_survives_null_dispatched_quantity():
    db = _fresh_session()
    order = models.CustomerOrder(
        order_no="CO-1", customer_name="Acme", product_name="Widget",
        order_quantity=1000, dispatched_quantity=100, due_date=date(2030, 1, 1),
        status="In Production", tenant_code="DEFAULT",
    )
    db.add(order)
    db.commit()
    _null_column(db, "customer_orders", "dispatched_quantity", order.id)
    assert db.query(models.CustomerOrder).one().dispatched_quantity is None

    tick_customer_order(db)                            # must not TypeError / roll back

    healed = db.query(models.CustomerOrder).one()
    # NULL -> 0, then + random(10, 50) -> [10, 50]; still short of 1000, so the
    # order reads Partially Dispatched.
    assert healed.dispatched_quantity is not None
    assert 10 <= healed.dispatched_quantity <= 50, healed.dispatched_quantity
    assert healed.status == "Partially Dispatched", healed.status
    print(f"PASS customer-order tick heals NULL dispatched_quantity to "
          f"{healed.dispatched_quantity} (no crash)")


if __name__ == "__main__":
    test_work_order_progress_survives_null_actual_quantity()
    test_work_order_progress_completes_from_null_when_target_is_tiny()
    test_work_order_progress_survives_null_target_quantity()
    test_work_order_progress_preserves_normal_increment()
    test_operator_tick_survives_null_good_count()
    test_customer_order_tick_survives_null_dispatched_quantity()
    print("ALL SIM NULL-COUNT TESTS PASSED")
