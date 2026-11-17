"""The quality seed must not crash when there are work orders but no machines.

THE BUG
-------
``factory_simulator._quality`` picks a machine for each seeded inspection with::

    machine = next((m for m in machines if m.id == wo.machine_id),
                   random.choice(machines))

The ``random.choice(machines)`` is the DEFAULT argument of ``next()``, and Python
evaluates a default argument EAGERLY — every iteration, before ``next()`` even
inspects the generator. So with ``machines == []`` it raises
``IndexError: Cannot choose from an empty sequence`` whether or not a work order
would have matched a machine. ``_quality`` guarded only ``if not wos``, so a DB
holding work orders but no machine rows (a partial reseed, or machines wiped)
500'd here.

The three sibling seeders that use the identical idiom all guard the machine set
— ``_production_plans`` and ``_schedules`` with ``if not wos or not machines``,
and the live ``tick_quality`` the same way. ``_quality`` was the lone omission;
this pins the guard so it cannot regress, and the normal-path control proves the
guard did not change what the seed produces when machines ARE present.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_sim_quality_empty_machines.py
      (exit 0 = pass)
"""
import math

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import tenancy
from database import Base
from factory_simulator import _quality

# A simulator tick runs for ONE bound tenant and refuses otherwise
# (factory_simulator._bound_tenant). Bind DEFAULT around EACH test, mirroring
# test_sim_null_counts: under pytest conftest resets the binding before every
# test, so a module-level binding would be silently undone.
try:
    import pytest
except ImportError:                      # the standalone runner needs no pytest
    pytest = None
if pytest is not None:
    @pytest.fixture(autouse=True)
    def _bind_default_tenant():
        token = tenancy.set_current_tenant(tenancy.DEFAULT_TENANT)
        try:
            yield
        finally:
            tenancy.reset_current_tenant(token)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add_work_order(db, n, machine_id):
    db.add(models.WorkOrder(
        work_order_no=f"WO-{n}", part_number="P", batch_number="B",
        machine_id=machine_id, target_quantity=1000, actual_quantity=50,
        status="In Progress", tenant_code="DEFAULT",
    ))


def test_quality_seed_survives_work_orders_but_no_machines():
    """THE REGRESSION: work orders present, zero machines. Before the fix the
    eager ``random.choice([])`` default raised IndexError on the first iteration;
    now the ``not machines`` guard returns cleanly and writes nothing — a seed
    that cannot pick a machine records no inspection rather than crashing."""
    db = _fresh_session()
    # Work orders reference machine ids that do not exist (there are no machines).
    for n in (1, 2, 3):
        _add_work_order(db, n, machine_id=n)
    db.commit()
    assert db.query(models.Machine).count() == 0
    assert db.query(models.WorkOrder).count() == 3

    _quality(db)                         # must NOT raise IndexError

    # No machine to attribute an inspection to -> no inspections created.
    assert db.query(models.QualityInspection).count() == 0
    print("PASS _quality returns cleanly with work orders but no machines (no IndexError)")


def test_quality_seed_survives_empty_everything():
    """The pre-existing ``not wos`` short-circuit still holds: nothing at all ->
    return before the loop, no rows, no crash."""
    db = _fresh_session()
    assert db.query(models.WorkOrder).count() == 0
    assert db.query(models.Machine).count() == 0

    _quality(db)

    assert db.query(models.QualityInspection).count() == 0
    print("PASS _quality returns cleanly on an empty database")


def test_quality_seed_populates_when_machines_present():
    """CONTROL: the guard did not change the populated path. With machines AND
    work orders present, the seed writes exactly 12 inspections (the loop is
    ``range(1, 13)``), each obeying the quality identity it constructs —
    ``passed + failed == inspected`` — within the documented bounds, and attributed
    to a REAL machine (never a fabricated id). The expected count 12 and the
    identity are derived from the loop and the field assignments, independently of
    the random draws."""
    db = _fresh_session()
    db.add(models.Machine(name="M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.add(models.Machine(name="M2", status="Idle", utilization=30, tenant_code="DEFAULT"))
    db.commit()
    machine_ids = {m.id for m in db.query(models.Machine).all()}
    # Two of the three work orders point at a real machine; one points at a
    # non-existent id (4) so the next() fallback branch — random.choice(machines)
    # — is exercised and must still land on a real machine.
    _add_work_order(db, 1, machine_id=min(machine_ids))
    _add_work_order(db, 2, machine_id=max(machine_ids))
    _add_work_order(db, 3, machine_id=4)
    db.commit()

    _quality(db)

    inspections = db.query(models.QualityInspection).all()
    assert len(inspections) == 12, len(inspections)          # range(1, 13)
    for qi in inspections:
        # The quality identity the seed builds: passed = inspected - failed.
        assert qi.passed_quantity + qi.failed_quantity == qi.inspected_quantity, (
            qi.inspection_no, qi.passed_quantity, qi.failed_quantity, qi.inspected_quantity)
        assert 50 <= qi.inspected_quantity <= 200, qi.inspected_quantity
        # failed is random.randint(0, max(1, int(inspected * 0.07))).
        assert 0 <= qi.failed_quantity <= max(1, int(qi.inspected_quantity * 0.07)), (
            qi.inspection_no, qi.failed_quantity, qi.inspected_quantity)
        # scrap <= failed // 3, rework <= failed — never more scrap/rework than failures.
        assert 0 <= qi.scrap_quantity <= max(0, qi.failed_quantity // 3), (
            qi.inspection_no, qi.scrap_quantity, qi.failed_quantity)
        assert 0 <= qi.rework_quantity <= qi.failed_quantity, (
            qi.inspection_no, qi.rework_quantity, qi.failed_quantity)
        # Attributed to a real machine, not a crash-fabricated one.
        assert qi.machine_id in machine_ids, (qi.inspection_no, qi.machine_id)
        # Passed <=> no failures (the status rule).
        assert (qi.status == "Passed") == (qi.failed_quantity == 0), (
            qi.inspection_no, qi.status, qi.failed_quantity)
    print(f"PASS _quality seeds 12 reconciling inspections across {len(machine_ids)} machines")


if __name__ == "__main__":
    tenancy.set_current_tenant(tenancy.DEFAULT_TENANT)   # one process, one tenant
    test_quality_seed_survives_work_orders_but_no_machines()
    test_quality_seed_survives_empty_everything()
    test_quality_seed_populates_when_machines_present()
    print("ALL SIM QUALITY-EMPTY-MACHINES TESTS PASSED")
