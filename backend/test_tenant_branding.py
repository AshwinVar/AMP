"""Workspace branding tests (PATCH /tenant-config).

The white-label surface the Branding settings card drives: a tenant Admin may
re-brand their OWN workspace (name / colour / logo), while plan and licence
edits stay platform-owner-only even through the same endpoint. Pins both sides
plus the effective-tenant behaviour (the founder previewing a client edits the
PREVIEWED tenant's branding, not DEFAULT's).

The colour and the logo were saved and shown back by the settings card, and the
dashboard applied neither (only the name). They now front the sidebar
(frontend BrandMark), so the server refuses what a browser could not render:
set_branding is the one rule, for the tenant Admin and the founder alike.

Run:  python backend/test_tenant_branding.py     (exit 0 = pass)
"""
import ast
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import platform_routes
import tenancy
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))


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


ACME_ADMIN = {"tenant": "ACME", "role": "Admin", "sub": "acme-admin"}
FOUNDER = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}
BAD_COLOURS = ("red", "#12345", "#1234567", "1d4ed8", "#gggggg", "url(https://x.test/a.png)", 5, None)
BAD_LOGOS = ("http://acme.test/l.png", "javascript:alert(1)", "data:image/png;base64,AAAA",
             "//acme.test/l.png", "acme.test/l.png", "https://", "https://acme.test/" + "a" * 2100, 7)


def _refused(write, payload, field):
    try:
        write(payload)
    except HTTPException as e:
        return e.status_code == 400 and field in str(e.detail)
    return False


def test_colour_and_logo_are_what_the_dashboard_can_apply():
    """The dashboard applies the colour and the logo (frontend BrandMark), so the
    server refuses what it could not apply: a colour that is not #rrggbb, a logo
    that is not an https:// address (the page's CSP loads no other)."""
    db = _fresh_session()
    tok = tenancy.set_current_tenant("ACME")
    try:
        def write(payload):
            return platform_routes.update_tenant_config(payload, db=db, current_user=ACME_ADMIN)

        write({"brand_color": "#1d4ed8", "brand_logo_url": "https://acme.test/l.png"})
        bad_colours = [v for v in BAD_COLOURS if not _refused(write, {"brand_color": v}, "brand_color")]
        assert not bad_colours, f"accepted as a colour: {bad_colours}"
        bad_logos = [v for v in BAD_LOGOS if not _refused(write, {"brand_logo_url": v}, "brand_logo_url")]
        assert not bad_logos, f"accepted as a logo: {bad_logos}"
        cfg = platform_routes.get_or_create_config(db, "ACME")
        assert (cfg.brand_color, cfg.brand_logo_url) == ("#1d4ed8", "https://acme.test/l.png"), \
            "a refused write changed what was stored"

        # One bad field refuses the whole write: its other fields do not land.
        assert _refused(write, {"brand_name": "Half written", "brand_logo_url": "http://x.test/l.png"},
                        "brand_logo_url")
        assert platform_routes.get_or_create_config(db, "ACME").brand_name != "Half written"

        # Empty or null removes the logo.
        assert write({"brand_logo_url": ""})["brand_logo_url"] is None
        write({"brand_logo_url": "https://acme.test/2.png"})
        assert write({"brand_logo_url": None})["brand_logo_url"] is None
    finally:
        tenancy.reset_current_tenant(tok)
    print("PASS the server refuses a colour or logo the dashboard could not apply")


def test_the_founder_is_held_to_the_same_rule():
    db = _fresh_session()

    def write(payload):
        return platform_routes.update_any_tenant("ACME", payload, db=db, current_user=FOUNDER)

    assert _refused(write, {"brand_color": "blue"}, "brand_color")
    assert _refused(write, {"brand_logo_url": "http://acme.test/l.png"}, "brand_logo_url")
    r = write({"brand_color": "#e11d2a", "brand_logo_url": "https://acme.test/l.svg"})
    assert (r["brand_color"], r["brand_logo_url"]) == ("#e11d2a", "https://acme.test/l.svg")
    print("PASS the founder's PATCH /tenant-configs/{code} applies the same rule")


def test_one_function_writes_branding():
    """Both writers go through platform_routes.set_branding, and nothing else in
    the module writes brand_color or brand_logo_url (an assignment or a setattr
    over a list of field names, the form the unvalidated loops had)."""
    tree = ast.parse(open(os.path.join(HERE, "platform_routes.py"), encoding="utf-8").read())
    functions = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    for writer in ("update_tenant_config", "update_any_tenant"):
        called = {getattr(n.func, "id", None) for n in ast.walk(functions[writer]) if isinstance(n, ast.Call)}
        assert "set_branding" in called, f"{writer} does not call set_branding"
    fields = {"brand_color", "brand_logo_url"}
    elsewhere = []
    for name, fn in functions.items():
        if name == "set_branding":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr in fields for t in node.targets):
                elsewhere.append(f"{name}:{node.lineno}")
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setattr"
                    and any(isinstance(c, ast.Constant) and c.value in fields for c in ast.walk(fn))):
                elsewhere.append(f"{name}:{node.lineno} (setattr)")
    assert not elsewhere, f"branding written outside set_branding: {elsewhere}"
    print("PASS one function writes branding, and both writers call it")


if __name__ == "__main__":
    test_client_admin_rebrands_own_workspace_but_not_licence()
    test_founder_preview_edits_the_previewed_tenant()
    test_colour_and_logo_are_what_the_dashboard_can_apply()
    test_the_founder_is_held_to_the_same_rule()
    test_one_function_writes_branding()
    print("\nALL TENANT BRANDING TESTS PASSED")
