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


if __name__ == "__main__":
    test_effective_tenant_matrix()
    test_seed_scopes_to_new_tenant_only()
    test_seed_never_overwrites_existing_tenant_data()
    test_read_models_light_up_for_new_tenant()
    print("ALL ONBOARDING TESTS PASSED")
