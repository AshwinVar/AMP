"""Tenant offboarding tests.

Proves the destructive tail of the lifecycle is both complete and contained:
  * purge removes the tenant's rows from every tenant-aware table;
  * other tenants' data and the immutable EventLog survive;
  * DEFAULT and blank codes can never be purged;
  * the delete endpoint wires it all together (registry row + optional purge).

Run:  python backend/test_offboarding.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models
import saas_routes
from database import Base
from tenancy import install_scoping
from onboard_tenant import seed_starter_factory
from offboard_tenant import purge_tenant_data


def _fresh_session():
    # PRAGMA foreign_keys=ON so SQLite enforces FKs like prod Postgres does —
    # without it, deletion-order bugs (machines before their children) pass
    # here and explode only in production.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

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
    row = saas_routes.create_company_tenant(schemas.CompanyTenantCreate(
        company_code="APEX", company_name="Apex Gear Works", industry="",
        plan_name="Starter", subscription_status="Trial", seats=5, monthly_fee=0,
    ), db=db, current_user=founder)

    result = saas_routes.delete_company_tenant(row.id, purge=True, db=db, current_user=founder)
    assert result["purged"] and result["purged"]["machines"] == 4
    assert db.query(models.CompanyTenant).count() == 0
    assert db.query(models.Machine).count() == 0
    # without purge, data would have survived — covered by the registry-only
    # default (purge=False) leaving offboarding to a later decision
    print("PASS delete endpoint purges when asked")


# ── Tenant-less children of tenant tables ─────────────────────────────────────
# The sweep deletes every model with a `tenant_code`. GmatsProformaLine and
# GmatsMINLine have none — they belong to their parent by foreign key — so the
# sweep never deleted them, and on a database that enforces foreign keys
# (PostgreSQL in production; SQLite here with PRAGMA foreign_keys=ON) those rows
# BLOCKED the delete of the tenant's proformas, MINs and items. Measured before
# the fix: "purge blocked by constraints on: gmats_items, gmats_proformas,
# gmats_min" for any tenant that had ever raised a proforma or issued a MIN —
# i.e. every tenant that used the GMATS module could not be offboarded at all.


def _gmats_estate(db, tenant, prefix):
    item = models.GmatsItem(tenant_code=tenant, item_code=f"{prefix}-I1", item_name="Valve",
                            physical_stock=10, reserved_stock=0)
    db.add(item)
    db.flush()
    pf = models.GmatsProforma(tenant_code=tenant, proforma_no=f"{prefix}-PI-1",
                              customer_name="Buyer", status="Open")
    mn = models.GmatsMIN(tenant_code=tenant, min_no=f"{prefix}-MIN-1", customer_name="Buyer",
                         machine_ref="Rig", status="Issued")
    db.add_all([pf, mn])
    db.flush()
    db.add(models.GmatsProformaLine(proforma_id=pf.id, item_id=item.id, qty=2))
    db.add(models.GmatsMINLine(min_id=mn.id, item_id=item.id, qty=1))
    db.commit()
    return item, pf, mn


def test_purge_succeeds_for_a_tenant_that_used_gmats():
    db = _fresh_session()
    _gmats_estate(db, "ACME", "A")
    g_item, g_pf, g_mn = _gmats_estate(db, "GLOBEX", "G")

    counts = purge_tenant_data(db, "ACME")    # raised RuntimeError before the fix

    for model in (models.GmatsItem, models.GmatsProforma, models.GmatsMIN):
        assert db.query(model).filter(model.tenant_code == "ACME").count() == 0, model
    # Lines have no tenant_code: ACME's are gone because their parents were ACME's.
    assert db.query(models.GmatsProformaLine).filter(
        models.GmatsProformaLine.proforma_id != g_pf.id).count() == 0
    assert db.query(models.GmatsMINLine).filter(
        models.GmatsMINLine.min_id != g_mn.id).count() == 0
    assert counts.get("gmats_proforma_lines") == 1 and counts.get("gmats_min_lines") == 1, counts
    # Contained: GLOBEX's estate, lines included, is untouched.
    assert db.query(models.GmatsProformaLine).filter(
        models.GmatsProformaLine.proforma_id == g_pf.id).count() == 1
    assert db.query(models.GmatsMINLine).filter(models.GmatsMINLine.min_id == g_mn.id).count() == 1
    assert db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == "GLOBEX").count() == 1
    print("PASS a tenant that used GMATS proformas and MINs can be purged; others untouched")


def test_another_tenants_line_pointing_at_a_purged_item_is_detached_not_deleted():
    # The GMATS create path resolved items by unscoped id while locking the parent's
    # tenant (see test_gmats_inventory_routes), so a GLOBEX line can reference an
    # ACME item. That line belongs to GLOBEX: deleting it would destroy another
    # company's record. Its reference to an item that is being destroyed is
    # detached (item_id NULL) instead, and counted.
    db = _fresh_session()
    a_item, _, _ = _gmats_estate(db, "ACME", "A")
    _, g_pf, g_mn = _gmats_estate(db, "GLOBEX", "G")
    db.add(models.GmatsProformaLine(proforma_id=g_pf.id, item_id=a_item.id, qty=5))
    db.commit()

    counts = purge_tenant_data(db, "ACME")

    globex_lines = db.query(models.GmatsProformaLine).filter(
        models.GmatsProformaLine.proforma_id == g_pf.id).all()
    assert len(globex_lines) == 2, "a GLOBEX line was deleted by ACME's purge"
    assert sorted(l.item_id is None for l in globex_lines) == [False, True], \
        [l.item_id for l in globex_lines]
    assert counts.get("gmats_line_foreign_items_detached") == 1, counts
    print("PASS another tenant's line to a purged item is detached, never deleted")


def test_every_tenantless_child_of_a_tenant_table_has_a_declared_fate():
    """The structural half. A tenant-less model with a foreign key into a
    tenant-stamped table is invisible to the sweep, and on PostgreSQL it will
    block the purge. So the purge must say, for each one, what happens to it —
    and this fails for the next such model someone adds without deciding."""
    import offboard_tenant
    tables = {m.class_.__tablename__: m.class_ for m in models.Base.registry.mappers}
    tenant_tables = {t for t, c in tables.items() if getattr(c, "tenant_code", None) is not None}
    found = set()
    for t, cls in tables.items():
        if t in tenant_tables:
            continue
        if any(fk.column.table.name in tenant_tables
               for col in cls.__table__.columns for fk in col.foreign_keys):
            found.add(cls.__name__)
    assert len(found) >= 3, f"the enumeration found too little to be trusted: {found}"
    declared = set(offboard_tenant.TENANTLESS_CHILDREN)
    assert found == declared, (
        f"undeclared: {sorted(found - declared)}; declared but no longer a child: "
        f"{sorted(declared - found)}")
    print(f"PASS every tenant-less child of a tenant table has a declared fate: {sorted(found)}")


def test_a_failed_purge_keeps_the_registry_row_so_it_can_be_retried():
    # The registry row used to be deleted and COMMITTED before the purge ran. A
    # purge that then failed left the tenant gone from SaaS Admin with all its
    # data still in place — and no row left to press Delete on again.
    import offboard_tenant
    from fastapi import HTTPException
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}
    db.add(models.CompanyTenant(company_code="ACME", company_name="Acme"))
    db.commit()
    row = db.query(models.CompanyTenant).first()

    real = offboard_tenant.purge_tenant_data

    def boom(db_, code):
        raise RuntimeError("purge blocked by constraints on: somewhere")

    offboard_tenant.purge_tenant_data = boom
    try:
        try:
            saas_routes.delete_company_tenant(row.id, purge=True, db=db, current_user=founder)
            raise AssertionError("a failed purge was reported as success")
        except HTTPException as e:
            assert e.status_code == 500, e.status_code
    finally:
        offboard_tenant.purge_tenant_data = real
    assert db.query(models.CompanyTenant).filter(
        models.CompanyTenant.company_code == "ACME").count() == 1, \
        "the registry row was deleted even though the purge failed"
    print("PASS a failed purge leaves the registry row in place for a retry")


if __name__ == "__main__":
    test_purge_is_complete_and_contained()
    test_default_and_blank_never_purgeable()
    test_delete_endpoint_with_purge()
    test_purge_succeeds_for_a_tenant_that_used_gmats()
    test_another_tenants_line_pointing_at_a_purged_item_is_detached_not_deleted()
    test_every_tenantless_child_of_a_tenant_table_has_a_declared_fate()
    test_a_failed_purge_keeps_the_registry_row_so_it_can_be_retried()
    print("ALL OFFBOARDING TESTS PASSED")
