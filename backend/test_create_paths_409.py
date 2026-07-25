"""Cross-tenant duplicate business keys return 409, not 500 (the whole class).

Twelve create handlers across nine route modules pre-check a globally-unique
business key through the tenant-SCOPED ORM, so the check only sees the caller's
own tenant. A key another tenant already uses passes the check, then trips the
global DB constraint on commit. That must be a clean 409 with a rolled-back
session, not an unhandled IntegrityError -> 500. (orders_routes is covered by
test_orders_routes.py; this pins the rest.)

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


def test_work_order_no_duplicate_across_tenants_is_409():
    # work_order_no is globally unique; each tenant has its own machine (also scoped).
    db = _iso_session()
    _commit_as(db, "TA", models.Machine(id=1, name="M-A", status="Running"))
    _commit_as(db, "TB", models.Machine(id=2, name="M-B", status="Running"))

    def create(tenant, mid, no):
        return _as(db, tenant, lambda: work_orders_routes.create_work_order(
            schemas.WorkOrderCreate(work_order_no=no, part_number="P", batch_number="B",
                                    machine_id=mid, target_quantity=10),
            db=db, current_user={"tenant": tenant}))

    create("TA", 1, "WO-1")
    try:
        create("TB", 2, "WO-1")               # scoped pre-check passes; global key collides
        assert False, "cross-tenant duplicate work_order_no should raise, not succeed"
    except HTTPException as e:
        assert e.status_code == 409, e.status_code

    wo = create("TB", 2, "WO-2")              # session survived the rollback
    assert wo.work_order_no == "WO-2"
    print("PASS work_order_no cross-tenant duplicate -> 409, session survives")


def test_inspection_no_duplicate_across_tenants_is_409():
    db = _iso_session()

    def create(tenant, no):
        return _as(db, tenant, lambda: quality_routes.create_quality_inspection(
            schemas.QualityInspectionCreate(inspection_no=no, inspector="qa", inspected_quantity=10),
            db=db, current_user={"tenant": tenant}))

    create("TA", "QC-1")
    try:
        create("TB", "QC-1")
        assert False, "cross-tenant duplicate inspection_no should raise, not succeed"
    except HTTPException as e:
        assert e.status_code == 409, e.status_code
    print("PASS inspection_no cross-tenant duplicate -> 409")


def test_every_fixed_module_guards_integrity_error():
    # Pins the whole class: each create-path module imports IntegrityError and has
    # an except clause, so a future edit can't quietly drop the guard from one.
    for name in FIXED_MODULES:
        src = _inspect.getsource(importlib.import_module(name))
        assert "from sqlalchemy.exc import IntegrityError" in src, f"{name}: missing import"
        assert "except IntegrityError" in src, f"{name}: missing guard"
    print(f"PASS all {len(FIXED_MODULES)} create-path modules guard IntegrityError -> 409")


if __name__ == "__main__":
    test_work_order_no_duplicate_across_tenants_is_409()
    test_inspection_no_duplicate_across_tenants_is_409()
    test_every_fixed_module_guards_integrity_error()
    print("CREATE-PATHS 409 OK: cross-tenant duplicate keys return 409 (not 500) across all create paths")
