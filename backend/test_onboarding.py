"""Second-tenant onboarding tests.

Proves the three onboarding pieces against an in-memory SQLite DB:
  * ``effective_tenant`` — only a DEFAULT (founder) claim may preview another
    tenant via the X-Tenant header; client tokens stay locked to their own;
  * ``seed_starter_factory`` — writes a complete starter set under the new
    tenant only, leaves DEFAULT/GMATS untouched, and never reseeds over
    existing data;
  * the read-models light up for the new tenant from the seeded data.

Run:  python backend/test_onboarding.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from tenancy import (DEFAULT_TENANT, effective_tenant, install_scoping,
                     set_current_tenant, reset_current_tenant)
from onboard_tenant import seed_starter_factory


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    install_scoping()
    return sessionmaker(bind=engine)()


def _count(db, model, tenant):
    token = set_current_tenant(tenant)
    try:
        return db.query(model).count()
    finally:
        reset_current_tenant(token)


def test_effective_tenant_matrix():
    # founder (DEFAULT claim) may preview any tenant via the header
    assert effective_tenant("DEFAULT", "APEX") == "APEX"
    assert effective_tenant("DEFAULT", "GMATS") == "GMATS"
    # no header → everyone stays on their claim
    assert effective_tenant("DEFAULT", None) == "DEFAULT"
    assert effective_tenant("GMATS", None) == "GMATS"
    # a client token can never escape its tenant via the header
    assert effective_tenant("GMATS", "DEFAULT") == "GMATS"
    assert effective_tenant("GMATS", "APEX") == "GMATS"
    assert effective_tenant("APEX", "GMATS") == "APEX"
    # unauthenticated (no claim) is never granted a preview
    assert effective_tenant(None, "APEX") is None
    assert DEFAULT_TENANT == "DEFAULT"
    print("PASS effective_tenant matrix")


def test_seed_scopes_to_new_tenant_only():
    db = _fresh_session()
    # pre-existing DEFAULT data that must survive untouched
    db.add(models.Machine(name="SMT-01", status="Running", tenant_code="DEFAULT"))
    db.commit()

    assert seed_starter_factory(db, "APEX", "Apex Gear Works") is True

    # the new tenant got a full starter set…
    assert _count(db, models.Machine, "APEX") == 4
    assert _count(db, models.FactoryLayoutNode, "APEX") == 4
    assert _count(db, models.ProductionRecord, "APEX") == 9
    assert _count(db, models.InventoryItem, "APEX") == 3
    assert _count(db, models.CustomerOrder, "APEX") == 2
    assert _count(db, models.WorkOrder, "APEX") == 2
    assert _count(db, models.QualityInspection, "APEX") == 1
    # …every row stamped with the tenant
    token = set_current_tenant("APEX")
    try:
        assert all(m.tenant_code == "APEX" for m in db.query(models.Machine).all())
    finally:
        reset_current_tenant(token)

    # DEFAULT is exactly as it was; GMATS got nothing
    assert _count(db, models.Machine, "DEFAULT") == 1
    assert _count(db, models.Machine, "GMATS") == 0
    assert _count(db, models.ProductionRecord, "DEFAULT") == 0
    print("PASS seed scopes to the new tenant only")


def test_seed_never_overwrites_existing_tenant_data():
    db = _fresh_session()
    assert seed_starter_factory(db, "APEX", "Apex Gear Works") is True
    # second call is a no-op: the tenant already has a factory
    assert seed_starter_factory(db, "APEX", "Apex Gear Works") is False
    assert _count(db, models.Machine, "APEX") == 4
    assert _count(db, models.ProductionRecord, "APEX") == 9
    print("PASS seed is idempotent")


def test_read_models_light_up_for_new_tenant():
    import ai
    db = _fresh_session()
    seed_starter_factory(db, "APEX", "Apex Gear Works")

    token = set_current_tenant("APEX")
    try:
        oee = ai.oee.build_oee_summary(db, "APEX")
        assert oee["machines"], "OEE should see the seeded production records"
        briefing = ai.briefing.build_briefing(db, "APEX")
        assert briefing["headline"] and "No production data" not in briefing["headline"]
        twins = ai.twin.build_twins(db, "APEX")
        assert len(twins) == 4
        hits = ai.search.build_search(db, "APEX", "CNC")
        assert any(h["label"] == "CNC-01" for h in hits["results"])
    finally:
        reset_current_tenant(token)

    # and the founder's DEFAULT view of the same models stays empty
    token = set_current_tenant("DEFAULT")
    try:
        assert ai.twin.build_twins(db, "DEFAULT") == []  # nothing leaks to DEFAULT
    finally:
        reset_current_tenant(token)
    print("PASS read-models light up for the new tenant")


def test_registry_scoped_to_own_tenant():
    """The tenant registry (list + SaaS analytics) is founder data: a client
    workspace sees only its own row, never other companies' names or fees."""
    import main
    db = _fresh_session()
    db.add(models.CompanyTenant(company_code="GMATS", company_name="GMATS Compressors",
                                industry="Compressors", plan_name="Growth",
                                subscription_status="Active", seats=10, monthly_fee=14999))
    db.add(models.CompanyTenant(company_code="APEX", company_name="Apex Gear Works",
                                industry="Precision Components", plan_name="Starter",
                                subscription_status="Trial", seats=5, monthly_fee=0))
    db.commit()

    founder = {"tenant": "DEFAULT", "role": "Admin"}
    client = {"tenant": "GMATS", "role": "Admin"}

    assert {t.company_code for t in main.get_company_tenants(db=db, current_user=founder)} == {"GMATS", "APEX"}
    assert [t.company_code for t in main.get_company_tenants(db=db, current_user=client)] == ["GMATS"]

    founder_saas = main.get_saas_analytics(db=db, current_user=founder)
    client_saas = main.get_saas_analytics(db=db, current_user=client)
    assert founder_saas["total_tenants"] == 2
    assert founder_saas["monthly_recurring_revenue"] == 14999
    assert client_saas["total_tenants"] == 1
    assert client_saas["monthly_recurring_revenue"] == 14999  # their own fee only
    assert client_saas["total_seats"] == 10
    print("PASS registry scoped to the caller's own tenant")


def test_sim_tick_cannot_touch_other_tenants():
    """A simulation tick bound to one tenant must never mutate another tenant's
    machines — the guarantee that lets real customer tenants coexist with the
    animated demo factory."""
    from factory_simulator import tick_machine_status
    db = _fresh_session()
    db.add(models.Machine(name="DEMO-M1", status="Running", utilization=80, tenant_code="DEFAULT"))
    db.add(models.Machine(name="REAL-M1", status="Breakdown", utilization=0, tenant_code="APEX"))
    db.commit()

    token = set_current_tenant("DEFAULT")
    try:
        for _ in range(40):   # plenty of flips — only DEFAULT may be picked
            tick_machine_status(db)
        db.commit()
    finally:
        reset_current_tenant(token)

    real = db.query(models.Machine).filter(models.Machine.name == "REAL-M1").first()
    assert real.status == "Breakdown" and real.utilization == 0, "sim leaked into APEX"
    print("PASS sim tick stays inside its bound tenant")


def test_sim_tenants_default():
    import main
    assert main.SIM_TENANTS == ["DEFAULT"], main.SIM_TENANTS
    print("PASS SIM_TENANTS defaults to the demo workspace only")


def test_admin_provisioning_and_password_change():
    """Founder provisions a tenant admin with a one-time password; the admin can
    then rotate it. Non-founders can't provision; repeats are rejected."""
    import main
    from fastapi import HTTPException
    from security import verify_password
    db = _fresh_session()
    db.add(models.CompanyTenant(company_code="APEX", company_name="Apex Gear Works",
                                industry="", plan_name="Starter",
                                subscription_status="Trial", seats=5, monthly_fee=0))
    db.commit()
    tid = db.query(models.CompanyTenant).first().id
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}

    creds = main.create_tenant_admin(tid, db=db, current_user=founder)
    assert creds["username"] == "apex_admin" and len(creds["temporary_password"]) >= 10
    made = db.query(models.User).filter(models.User.username == "apex_admin").first()
    assert made.tenant_code == "APEX" and made.role == "Admin"
    assert verify_password(creds["temporary_password"], made.password)

    # repeat -> 400; non-founder -> 403
    for user, code in ((founder, 400), ({"tenant": "GMATS", "role": "Admin", "sub": "g"}, 403)):
        try:
            main.create_tenant_admin(tid, db=db, current_user=user)
            assert False, "should have raised"
        except HTTPException as e:
            assert e.status_code == code

    # the provisioned admin rotates their password
    apex_admin = {"sub": "apex_admin", "tenant": "APEX", "role": "Admin"}
    try:
        main.change_password(_pw(creds["temporary_password"][:-1] + "x", "new-password-1"), db=db, current_user=apex_admin)
        assert False, "wrong current password should 401"
    except HTTPException as e:
        assert e.status_code == 401
    try:
        main.change_password(_pw(creds["temporary_password"], "short"), db=db, current_user=apex_admin)
        assert False, "short password should 400"
    except HTTPException as e:
        assert e.status_code == 400
    main.change_password(_pw(creds["temporary_password"], "rotated-pass-9"), db=db, current_user=apex_admin)
    db.refresh(made)
    assert verify_password("rotated-pass-9", made.password)
    print("PASS admin provisioning + password rotation")


def _pw(current, new):
    import schemas
    return schemas.ChangePasswordRequest(current_password=current, new_password=new)


def test_cancelled_subscription_blocks_login():
    """A tenant whose registry row says Cancelled cannot sign in; Trial/Active,
    registry-less tenants, and the founder are unaffected."""
    import main
    import schemas
    from fastapi import HTTPException
    from security import hash_password
    db = _fresh_session()
    db.add(models.CompanyTenant(company_code="APEX", company_name="Apex", industry="",
                                plan_name="Starter", subscription_status="Cancelled",
                                seats=5, monthly_fee=0))
    db.add(models.User(username="apex_admin", password=hash_password("pw-apex-123"),
                       role="Admin", tenant_code="APEX"))
    db.add(models.User(username="ghost_admin", password=hash_password("pw-ghost-12"),
                       role="Admin", tenant_code="GHOST"))   # no registry row
    db.commit()

    try:
        main.login(schemas.UserLogin(username="apex_admin", password="pw-apex-123"), db=db)
        assert False, "cancelled tenant login should 403"
    except HTTPException as e:
        assert e.status_code == 403

    # registry-less tenant still signs in
    assert main.login(schemas.UserLogin(username="ghost_admin", password="pw-ghost-12"), db=db)["access_token"]

    # flip back to Active -> login works again
    db.query(models.CompanyTenant).first().subscription_status = "Active"
    db.commit()
    assert main.login(schemas.UserLogin(username="apex_admin", password="pw-apex-123"), db=db)["tenant"] == "APEX"
    print("PASS cancelled subscription blocks login (and only that)")


def test_plan_tier_drives_licence():
    """The SaaS plan picked in SaaS Admin drives the tenant's licence
    (TenantConfig.enabled_modules) — Starter sees core only, Enterprise sees
    everything, unknown plans fail open, and a plan change re-syncs."""
    import main
    import schemas
    from platform_routes import apply_plan_tier
    db = _fresh_session()
    founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "admin_new"}

    row = main.create_company_tenant(schemas.CompanyTenantCreate(
        company_code="APEX", company_name="Apex Gear Works", industry="",
        plan_name="Starter", subscription_status="Trial", seats=5, monthly_fee=0,
    ), db=db, current_user=founder)
    cfg = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == "APEX").first()
    assert cfg.plan == "starter" and cfg.enabled_modules == "core"

    # plan change in SaaS Admin re-syncs the licence
    main.update_company_tenant(row.id, schemas.CompanyTenantUpdate(plan_name="Enterprise"),
                               db=db, current_user=founder)
    db.refresh(cfg)
    assert cfg.plan == "enterprise" and "intelligence" in cfg.enabled_modules

    # Professional maps to the growth tier; unknown fails open to enterprise
    assert apply_plan_tier(db, "P1", "Professional").enabled_modules == "core,operations,factory"
    assert apply_plan_tier(db, "U1", "Mystery Plan").plan == "enterprise"
    print("PASS plan tier drives the licence")


def test_sim_diagnostics_founder_only():
    """/platform/status exposes the sim allowlist + heartbeat to the founder
    only — the allowlist names tenants, which a client must not see."""
    import main
    db = _fresh_session()
    founder_view = main.platform_status(db=db, current_user={"tenant": "DEFAULT", "role": "Admin"})
    client_view = main.platform_status(db=db, current_user={"tenant": "APEX", "role": "Admin"})
    assert founder_view["sim"]["tenants"] == ["DEFAULT"]
    assert "tick_count" in founder_view["sim"]
    assert "sim" not in client_view
    print("PASS sim diagnostics are founder-only")


if __name__ == "__main__":
    test_effective_tenant_matrix()
    test_seed_scopes_to_new_tenant_only()
    test_seed_never_overwrites_existing_tenant_data()
    test_read_models_light_up_for_new_tenant()
    test_registry_scoped_to_own_tenant()
    test_sim_tick_cannot_touch_other_tenants()
    test_sim_tenants_default()
    test_admin_provisioning_and_password_change()
    test_cancelled_subscription_blocks_login()
    test_plan_tier_drives_licence()
    test_sim_diagnostics_founder_only()
    print("ALL ONBOARDING TESTS PASSED")
