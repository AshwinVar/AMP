"""A duplicate business key returns 409, not 500 - and only a duplicate.

ORIGINALLY this file pinned CROSS-tenant duplicates. Twelve create handlers
pre-check a business key through the tenant-SCOPED ORM, so a key another tenant
already used passed the check and then tripped a PLATFORM-WIDE DB constraint on
commit; the 409 stopped that being a 500.

ADR-0012 removed the constraint that made it possible: those keys are now unique
per tenant, so a key another customer owns is not a conflict at all and the
create simply succeeds. That is the point - a customer could not previously use
their own WO-001 if somebody else had got there first.

What remains, and is what this file now pins:

  * a cross-tenant duplicate SUCCEEDS (the blocker is gone), and each tenant
    keeps its own row;
  * a duplicate WITHIN one tenant is still a 409 with a surviving session -
    that path is still reachable, is the operator's own mistake, and an
    unhandled IntegrityError there would be a 500 plus a poisoned session for
    the rest of the request;
  * every create-path module still imports and handles IntegrityError, so the
    guard cannot be quietly dropped from one of them.

Run:  python backend/test_create_paths_409.py
"""
import importlib
import inspect as _inspect

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import schemas
import tenancy as T
import work_orders_routes
import quality_routes
from database import Base

FIXED_MODULES = [
    "work_orders_routes", "production_planning_routes", "quality_routes",
    "inventory_routes", "costing_routes", "reports_routes",
    "operator_routes", "industrial_iot_routes", "factory_ops_routes",
]


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _as(db, tenant, fn):
    tok = T.set_current_tenant(tenant)
    try:
        return fn()
    finally:
        T.reset_current_tenant(tok)


def _commit_as(db, tenant, *objs):
    # Commit INSIDE the tenant context so the before_flush write-stamp assigns the
    # rows to `tenant` (stamping happens at flush/commit, not at db.add).
    tok = T.set_current_tenant(tenant)
    for o in objs:
        db.add(o)
    db.commit()
    T.reset_current_tenant(tok)


def test_work_order_no_duplicate_across_tenants_now_succeeds():
    """ADR-0012: WO-1 belongs to a tenant, so two tenants may each have one."""
    db = _iso_session()
    _commit_as(db, "TA", models.Machine(id=1, name="M-A", status="Running"))
    _commit_as(db, "TB", models.Machine(id=2, name="M-B", status="Running"))

    def create(tenant, mid, no):
        return _as(db, tenant, lambda: work_orders_routes.create_work_order(
            schemas.WorkOrderCreate(work_order_no=no, part_number="P", batch_number="B",
                                    machine_id=mid, target_quantity=10),
            db=db, current_user={"tenant": tenant}))

    a = create("TA", 1, "WO-1")
    b = create("TB", 2, "WO-1")
    assert a.id != b.id, (a.id, b.id)
    assert a.work_order_no == b.work_order_no == "WO-1"

    # CONTROL: two distinct rows, one per tenant - not one row read twice.
    owners = sorted(
        r.tenant_code for r in
        _as(db, None, lambda: db.query(models.WorkOrder).all()))
    assert owners == ["TA", "TB"], owners
    print("PASS two tenants may each own WO-1")


def test_work_order_no_duplicate_inside_one_tenant_is_still_409():
    """The guard that still has a job: the operator's own duplicate."""
    db = _iso_session()
    _commit_as(db, "TA", models.Machine(id=1, name="M-A", status="Running"))

    def create(no):
        return _as(db, "TA", lambda: work_orders_routes.create_work_order(
            schemas.WorkOrderCreate(work_order_no=no, part_number="P", batch_number="B",
                                    machine_id=1, target_quantity=10),
            db=db, current_user={"tenant": "TA"}))

    create("WO-1")
    try:
        create("WO-1")
        assert False, "a tenant duplicating its OWN work_order_no should raise"
    except HTTPException as e:
        assert e.status_code in (400, 409), e.status_code

    wo = create("WO-2")               # the session survived
    assert wo.work_order_no == "WO-2"
    print("PASS a tenant's own duplicate work_order_no -> refused, session survives")


def test_inspection_no_duplicate_across_tenants_now_succeeds():
    db = _iso_session()

    def create(tenant, no):
        return _as(db, tenant, lambda: quality_routes.create_quality_inspection(
            schemas.QualityInspectionCreate(inspection_no=no, inspector="qa",
                                            inspected_quantity=10),
            db=db, current_user={"tenant": tenant}))

    create("TA", "QC-1")
    create("TB", "QC-1")
    owners = sorted(
        r.tenant_code for r in
        _as(db, None, lambda: db.query(models.QualityInspection).all()))
    assert owners == ["TA", "TB"], owners

    # ...and the same tenant still cannot repeat it.
    try:
        create("TA", "QC-1")
        assert False, "a tenant duplicating its OWN inspection_no should raise"
    except HTTPException as e:
        assert e.status_code in (400, 409), e.status_code
    print("PASS inspection_no: shared across tenants, refused within one")


def test_every_fixed_module_guards_integrity_error():
    # Pins the whole class: each create-path module imports IntegrityError and has
    # an except clause, so a future edit can't quietly drop the guard from one.
    for name in FIXED_MODULES:
        src = _inspect.getsource(importlib.import_module(name))
        assert "from sqlalchemy.exc import IntegrityError" in src, f"{name}: missing import"
        assert "except IntegrityError" in src, f"{name}: missing guard"
    print(f"PASS all {len(FIXED_MODULES)} create-path modules guard IntegrityError -> 409")


if __name__ == "__main__":
    test_work_order_no_duplicate_across_tenants_now_succeeds()
    test_work_order_no_duplicate_inside_one_tenant_is_still_409()
    test_inspection_no_duplicate_across_tenants_now_succeeds()
    test_every_fixed_module_guards_integrity_error()
    print("CREATE-PATHS 409 OK: cross-tenant duplicate keys return 409 (not 500) across all create paths")
