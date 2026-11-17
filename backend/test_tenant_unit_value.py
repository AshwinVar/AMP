"""Tests for the configurable per-unit value (TenantConfig.unit_value_gbp).

An Admin sets their tenant's ₹-per-good-unit via PATCH /tenant-config; the
recovery read-model reads it to value the OEE gap. Cover the round-trip, the
validation (must be a non-negative number, null clears it), and that
recovery._unit_value reads back what was set.

Run:  python backend/test_tenant_unit_value.py     (exit 0 = pass)
"""

import sys

# PRINTING THE CURRENCY SYMBOL MUST NOT KILL A PASSING SUITE.
#
# A Windows console is cp1252 by default, and '\u20b9' has no cp1252 code point.
# Before this line, the suite ran green and then died with UnicodeEncodeError on
# the print that announced it -- a non-zero exit from a test that had passed.
# CI's runners are UTF-8, so CI never saw it; only a developer did.
# test_currency_single.py pins this for every suite carrying the symbol.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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


def test_validation_rejects_non_finite():
    # float() accepts "inf"/"nan"/"Infinity"/"NaN" and Starlette's json.loads
    # decodes bare Infinity/NaN, and the old `val < 0` let +inf and nan through
    # (neither is < 0). A stored non-finite rate is read straight back into every
    # money surface and 500s it (loss_value.whole(units x inf) -> OverflowError /
    # ValueError). It must be refused at the door exactly like a negative, the same
    # finite guard part_spec_routes._positive keeps.
    from fastapi import HTTPException
    db = _session()
    for bad in ("inf", "-inf", "nan", "Infinity", "NaN", float("inf"),
                float("-inf"), float("nan")):
        try:
            platform_routes.update_tenant_config({"unit_value_gbp": bad}, db=db, current_user=_ADMIN)
            assert False, f"{bad!r} (non-finite) should have been rejected"
        except HTTPException as e:
            assert e.status_code == 400, f"{bad!r} -> {e.status_code}"
    # the refusals left nothing behind: the tenant is still units-only.
    assert recovery._unit_value(db, "DEFAULT") is None
    # a real 0 is still a valid, stored rate (a ₹0 margin, not "unset").
    out = platform_routes.update_tenant_config({"unit_value_gbp": 0}, db=db, current_user=_ADMIN)
    assert out["unit_value_gbp"] == 0
    assert recovery._unit_value(db, "DEFAULT") == 0
    print("PASS unit_value_gbp rejects NaN / +-Inf (and stores a real 0)")


def test_unset_reads_none():
    db = _session()
    assert recovery._unit_value(db, "DEFAULT") is None  # no config row yet
    print("PASS unconfigured tenant reads a null unit value")


if __name__ == "__main__":
    test_set_and_read_back()
    test_validation_rejects_bad_values()
    test_validation_rejects_non_finite()
    test_unset_reads_none()
    print("ALL TENANT UNIT-VALUE TESTS PASSED")
