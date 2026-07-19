"""Tenant offboarding tests.

Proves the destructive tail of the lifecycle is both complete and contained:
  * purge removes the tenant's rows from every tenant-aware table;
  * other tenants' data and the immutable EventLog survive;
  * DEFAULT and blank codes can never be purged;
  * the delete endpoint wires it all together (registry row + optional purge).

Run:  python backend/test_offboarding.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from tenancy import install_scoping
from onboard_tenant import seed_starter_factory
from offboard_tenant import purge_tenant_data


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    install_scoping()
    return sessionmaker(bind=engine)()


def _seed_two_tenants(db):
    seed_starter_factory(db, "APEX", "Apex Gear Works")
    db.add(models.Machine(name="SMT-01", status="Running", tenant_code="DEFAULT"))
    db.add(models.User(username="apex_admin", password="x", role="Admin", tenant_code="APEX"))
    db.add(models.User(username="admin_new", password="x", role="Admin", tenant_code="DEFAULT"))
    db.add(models.TenantConfig(tenant_code="APEX", plan="starter", enabled_modules="core"))
    db.add(models.EventLog(event_type="TenantOnboarded", tenant_code="APEX", payload="{}"))
    db.commit()


def test_purge_is_complete_and_contained():
    db = _fresh_session()
    _seed_two_tenants(db)

    counts = purge_tenant_data(db, "APEX")

    # complete: machines, layout, production, inventory, orders, WOs, QC,
    # shift, user and licence all swept
    assert counts["machines"] == 4
    assert counts["users"] == 1
    assert counts["tenant_configs"] == 1
    assert sum(counts.values()) >= 20
    assert db.query(models.Machine).filter(models.Machine.tenant_code == "APEX").count() == 0
    assert db.query(models.User).filter(models.User.username == "apex_admin").first() is None

    # contained: DEFAULT untouched, immutable history kept
    assert db.query(models.Machine).filter(models.Machine.tenant_code == "DEFAULT").count() == 1
    assert db.query(models.User).filter(models.User.username == "admin_new").first() is not None
    assert db.query(models.EventLog).filter(models.EventLog.tenant_code == "APEX").count() == 1
    print("PASS purge is complete and contained")


def test_default_and_blank_never_purgeable():
    db = _fresh_session()
    for bad in ("DEFAULT", "", "  ", None):
        try:
            purge_tenant_data(db, bad)
            assert False, f"{bad!r} should be refused"
        except ValueError:
            pass
    print("PASS DEFAULT and blank codes are never purgeable")


def test_delete_endpoint_with_purge():
    import main
    import schemas
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}
    row = main.create_company_tenant(schemas.CompanyTenantCreate(
        company_code="APEX", company_name="Apex Gear Works", industry="",
        plan_name="Starter", subscription_status="Trial", seats=5, monthly_fee=0,
    ), db=db, current_user=founder)

    result = main.delete_company_tenant(row.id, purge=True, db=db, current_user=founder)
    assert result["purged"] and result["purged"]["machines"] == 4
    assert db.query(models.CompanyTenant).count() == 0
    assert db.query(models.Machine).count() == 0
    # without purge, data would have survived — covered by the registry-only
    # default (purge=False) leaving offboarding to a later decision
    print("PASS delete endpoint purges when asked")


if __name__ == "__main__":
    test_purge_is_complete_and_contained()
    test_default_and_blank_never_purgeable()
    test_delete_endpoint_with_purge()
    print("ALL OFFBOARDING TESTS PASSED")
