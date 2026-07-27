"""Workspace branding tests (PATCH /tenant-config).

The white-label surface the Branding settings card drives: a tenant Admin may
re-brand their OWN workspace (name / colour / logo), while plan and licence
edits stay platform-owner-only even through the same endpoint. Pins both sides
plus the effective-tenant behaviour (the founder previewing a client edits the
PREVIEWED tenant's branding, not DEFAULT's).

Run:  python backend/test_tenant_branding.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import platform_routes
import tenancy
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_client_admin_rebrands_own_workspace_but_not_licence():
    db = _fresh_session()
    tok = tenancy.set_current_tenant("ACME")
    try:
        before = platform_routes.get_or_create_config(db, "ACME")
        original_plan = before.plan
        r = platform_routes.update_tenant_config(
            {"brand_name": "Acme Manufacturing", "brand_color": "#e11d2a",
             "brand_logo_url": "https://acme.test/logo.png",
             # a client Admin trying to self-upgrade — must be ignored
             "plan": "enterprise", "enabled_modules": ["core", "operations",
                                                       "factory", "intelligence", "admin"]},
            db=db, current_user={"tenant": "ACME", "role": "Admin", "sub": "acme-admin"})
    finally:
        tenancy.reset_current_tenant(tok)

    assert r["brand_name"] == "Acme Manufacturing"
    assert r["brand_color"] == "#e11d2a"
    assert r["brand_logo_url"] == "https://acme.test/logo.png"
    # licence untouched: branding is self-service, the plan is not
    assert r["plan"] == original_plan
    cfg = platform_routes.get_or_create_config(db, "ACME")
    assert cfg.brand_name == "Acme Manufacturing" and cfg.plan == original_plan
    print("PASS a client Admin re-brands their workspace; licence fields are ignored")


def test_founder_preview_edits_the_previewed_tenant():
    db = _fresh_session()
    platform_routes.get_or_create_config(db, "ACME")
    tok = tenancy.set_current_tenant("ACME")     # founder previewing ACME (X-Tenant)
    try:
        platform_routes.update_tenant_config(
            {"brand_name": "Previewed Name"},
            db=db, current_user={"tenant": "DEFAULT", "role": "Admin", "sub": "founder"})
    finally:
        tenancy.reset_current_tenant(tok)

    assert platform_routes.get_or_create_config(db, "ACME").brand_name == "Previewed Name"
    # DEFAULT's own branding untouched
    assert platform_routes.get_or_create_config(db, "DEFAULT").brand_name == "AMP"
    print("PASS founder preview edits the previewed tenant's branding, not DEFAULT's")


if __name__ == "__main__":
    test_client_admin_rebrands_own_workspace_but_not_licence()
    test_founder_preview_edits_the_previewed_tenant()
    print("\nALL TENANT BRANDING TESTS PASSED")
