"""SaaS registry rollup — NULL-safety + write-boundary guard (ADR-0008).

`seats` and `monthly_fee` on company_tenants are Integer columns with a
Python-side default only (NOT nullable=False), so a legacy / raw-SQL / migration
row — or an update that sent an explicit `null` — can carry a NULL. A bare
sum() over that None TypeError-500'd /analytics/saas, and the non-optional
CompanyTenantResponse then 500'd the very next /saas/tenants list.

These tests call the saas_routes handlers directly (the pattern used by
test_onboarding / test_offboarding) and pin the rollup to independently-derived
numbers, plus the edges: a NULL row, an empty registry, and an explicit-null
update.

Run:  python backend/test_saas_analytics_null_safe.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import saas_routes
import schemas
from database import Base

FOUNDER = {"tenant": "DEFAULT", "sub": "founder"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add(db, code, status, fee, seats):
    db.add(models.CompanyTenant(
        company_code=code, company_name=code.title(),
        subscription_status=status, monthly_fee=fee, seats=seats,
    ))


def _seed_registry(db):
    # Independently-derived expectations are computed from THESE numbers below.
    _add(db, "APEX", "Active", 1000, 10)
    _add(db, "BOLT", "Trial", 500, 5)
    _add(db, "CRUX", "Cancelled", 2000, 20)
    _add(db, "DELTA", "Active", 0, 0)      # a REAL zero-fee / zero-seat tenant
    _add(db, "ECHO", "Past Due", 300, 3)
    db.commit()
    # ECHO is written NULL for both columns to simulate a raw-SQL / migration row
    # the ORM default never touched. (Active status keeps it inside MRR's filter,
    # so a naive sum would trip over the None.)
    db.execute(text(
        "UPDATE company_tenants SET monthly_fee=NULL, seats=NULL "
        "WHERE subscription_status='Active' AND company_code='DELTA'"
    ))
    db.execute(text(
        "UPDATE company_tenants SET monthly_fee=NULL, seats=NULL "
        "WHERE company_code='ECHO'"
    ))
    db.commit()


def test_analytics_null_safe_and_reconciled():
    db = _fresh_session()
    _seed_registry(db)

    a = saas_routes.get_saas_analytics(db, FOUNDER)

    # Counts by status (independently derived from the seed):
    assert a["total_tenants"] == 5, a
    assert a["trial"] == 1, a
    assert a["active"] == 2, a          # APEX + DELTA(now-NULL fee)
    assert a["past_due"] == 1, a        # ECHO
    assert a["cancelled"] == 1, a       # CRUX

    # Status buckets reconcile to the whole (every seeded row has one of the 4).
    assert a["trial"] + a["active"] + a["past_due"] + a["cancelled"] == a["total_tenants"], a

    # MRR = fees of Trial+Active only, NULL treated as 0:
    #   APEX 1000 (Active) + BOLT 500 (Trial) + DELTA 0 (Active, NULL->0) = 1500.
    #   CRUX (Cancelled) and ECHO (Past Due) are outside the filter.
    assert a["monthly_recurring_revenue"] == 1500, a

    # Seats over ALL rows, NULL treated as 0:
    #   APEX 10 + BOLT 5 + CRUX 20 + DELTA 0(NULL) + ECHO 0(NULL) = 35.
    assert a["total_seats"] == 35, a
    print("PASS analytics is NULL-safe and reconciled (MRR=1500, seats=35)")


def test_analytics_empty_registry():
    db = _fresh_session()
    a = saas_routes.get_saas_analytics(db, FOUNDER)
    assert a["total_tenants"] == 0
    assert a["monthly_recurring_revenue"] == 0
    assert a["total_seats"] == 0
    assert a["trial"] == a["active"] == a["past_due"] == a["cancelled"] == 0
    print("PASS analytics survives an empty registry (all zeros)")


def test_update_rejects_explicit_null_seat_and_fee():
    db = _fresh_session()
    _add(db, "APEX", "Active", 1000, 10)
    db.commit()
    row = db.query(models.CompanyTenant).filter_by(company_code="APEX").first()

    from fastapi import HTTPException

    for field in ("seats", "monthly_fee"):
        payload = schemas.CompanyTenantUpdate.model_validate({field: None})
        try:
            saas_routes.update_company_tenant(row.id, payload, db, FOUNDER)
            assert False, f"expected 400 on explicit null {field}"
        except HTTPException as e:
            assert e.status_code == 400, e.status_code
            assert field in e.detail, e.detail
        db.rollback()

    # The row must be untouched — no NULL leaked into the required columns.
    db.refresh(row)
    assert row.seats == 10 and row.monthly_fee == 1000
    print("PASS update rejects an explicit null seats/monthly_fee (row untouched)")


def test_update_partial_and_valid_numbers_still_work():
    db = _fresh_session()
    _add(db, "APEX", "Active", 1000, 10)
    db.commit()
    row = db.query(models.CompanyTenant).filter_by(company_code="APEX").first()

    # Partial update that omits seats/fee leaves them alone.
    saas_routes.update_company_tenant(
        row.id, schemas.CompanyTenantUpdate.model_validate({"company_name": "Apex Gear"}),
        db, FOUNDER)
    db.refresh(row)
    assert row.company_name == "Apex Gear"
    assert row.seats == 10 and row.monthly_fee == 1000

    # A real numeric update (including a genuine 0) still applies.
    saas_routes.update_company_tenant(
        row.id, schemas.CompanyTenantUpdate.model_validate({"seats": 12, "monthly_fee": 0}),
        db, FOUNDER)
    db.refresh(row)
    assert row.seats == 12 and row.monthly_fee == 0

    # And the rollup reads the update back cleanly.
    a = saas_routes.get_saas_analytics(db, FOUNDER)
    assert a["total_seats"] == 12 and a["monthly_recurring_revenue"] == 0
    print("PASS partial + valid-number updates apply (0 stays a real 0)")


if __name__ == "__main__":
    test_analytics_null_safe_and_reconciled()
    test_analytics_empty_registry()
    test_update_rejects_explicit_null_seat_and_fee()
    test_update_partial_and_valid_numbers_still_work()
    print("ALL SAAS ANALYTICS NULL-SAFE TESTS PASSED")
