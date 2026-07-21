"""Tests for the configurable per-unit value (TenantConfig.unit_value_gbp).

An Admin sets their tenant's £-per-good-unit via PATCH /tenant-config; the
recovery read-model reads it to value the OEE gap. Cover the round-trip, the
validation (must be a non-negative number, null clears it), and that
recovery._unit_value reads back what was set.

Run:  python backend/test_tenant_unit_value.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import platform_routes
import ai.recovery as recovery

_ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_set_and_read_back():
    db = _session()
    out = platform_routes.update_tenant_config({"unit_value_gbp": 4.5}, db=db, current_user=_ADMIN)
    assert out["unit_value_gbp"] == 4.5
    # persisted + readable by the recovery model's helper
    assert recovery._unit_value(db, "DEFAULT") == 4.5
    # clearing with null goes back to units-only
    out2 = platform_routes.update_tenant_config({"unit_value_gbp": None}, db=db, current_user=_ADMIN)
    assert out2["unit_value_gbp"] is None
    assert recovery._unit_value(db, "DEFAULT") is None
    print("PASS unit_value_gbp round-trips (set, read, clear)")


def test_validation_rejects_bad_values():
    from fastapi import HTTPException
    db = _session()
    for bad in (-1, "abc", "-3"):
        try:
            platform_routes.update_tenant_config({"unit_value_gbp": bad}, db=db, current_user=_ADMIN)
            assert False, f"{bad!r} should have been rejected"
        except HTTPException as e:
            assert e.status_code == 400
    # a numeric string that is valid and non-negative is accepted (coerced)
    out = platform_routes.update_tenant_config({"unit_value_gbp": "2.75"}, db=db, current_user=_ADMIN)
    assert out["unit_value_gbp"] == 2.75
    print("PASS unit_value_gbp rejects negatives / non-numbers, coerces valid strings")


def test_unset_reads_none():
    db = _session()
    assert recovery._unit_value(db, "DEFAULT") is None  # no config row yet
    print("PASS unconfigured tenant reads a null unit value")


if __name__ == "__main__":
    test_set_and_read_back()
    test_validation_rejects_bad_values()
    test_unset_reads_none()
    print("ALL TENANT UNIT-VALUE TESTS PASSED")
