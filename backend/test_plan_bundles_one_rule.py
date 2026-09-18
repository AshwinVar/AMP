"""Which module packs a plan includes is written in ONE place: modules.json.

THE DEFECT
----------
modules.json calls itself "the SINGLE source of truth", and each pack lists the
plans that bundle it. The founder's POST /tenant-configs/{code}/apply-plan read
it. Nothing else did:

  * platform_routes.PLAN_MODULE_TIERS, used when the founder creates a company
    or changes its plan in SaaS Admin, spelled each plan's packs out again;
  * _TENANT_DEFAULTS spelled out the demo and growth bundles for DEFAULT and
    GMATS, and get_or_create_config the enterprise one for everyone else;
  * models.TenantConfig's column default spelled out the enterprise bundle.

The copies agreed on the day this was found, so nothing was wrong yet. But a
pack added to a plan in modules.json reached only tenants licensed through
apply-plan: a company created or re-planned in SaaS Admin, or seeded, kept the
old bundle, and the plan gate then refused it the new pack.

THE RULE
--------
module_manifest.plan_modules(plan) is the one reader of a plan's bundle. SaaS
plan names that are not manifest plans are aliases (Professional -> growth),
and an unknown name still fails open to enterprise, as it did.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_plan_bundles_one_rule.py
"""
import ast
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import module_manifest
import platform_routes
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def bundle(plan):
    return ",".join(module_manifest.plan_bundles()[plan])


def founder():
    return {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}


def section_every_path_reads_the_manifest():
    print("=" * 74)
    print("1. EVERY PATH THAT LICENSES A TENANT GIVES THE MANIFEST'S BUNDLE")
    print("=" * 74)
    db = fresh_db()
    for name, tier in (("Starter", "starter"), ("Professional", "growth"), ("Growth", "growth"),
                       ("Enterprise", "enterprise"), ("demo", "demo")):
        c = platform_routes.apply_plan_tier(db, f"T_{name.upper()}", name)
        check(f"SaaS plan {name!r} -> {tier}: {bundle(tier)}",
              (c.plan, c.enabled_modules) == (tier, bundle(tier)), f"{c.plan}: {c.enabled_modules}")
    c = platform_routes.apply_plan_tier(db, "T_UNKNOWN", "Mystery Plan")
    check("an unknown SaaS plan still fails open to the enterprise bundle",
          (c.plan, c.enabled_modules) == ("enterprise", bundle("enterprise")),
          f"{c.plan}: {c.enabled_modules}")
    for code, plan in (("DEFAULT", "demo"), ("GMATS", "growth"), ("NEWCO", "enterprise")):
        c = platform_routes.get_or_create_config(db, code)
        check(f"a first-seen {code} is created on the {plan} bundle",
              (c.plan, c.enabled_modules) == (plan, bundle(plan)), f"{c.plan}: {c.enabled_modules}")
    for plan in module_manifest.plan_bundles():
        cfg = platform_routes.apply_plan(f"A_{plan.upper()}", {"plan": plan}, db=db, current_user=founder())
        check(f"POST apply-plan {plan} gives its bundle",
              ",".join(cfg["enabled_modules"]) == bundle(plan), str(cfg["enabled_modules"]))
    row = models.TenantConfig(tenant_code="BARE")
    db.add(row)
    db.commit()
    db.refresh(row)
    check("a TenantConfig created with no modules defaults to the enterprise bundle",
          row.enabled_modules == bundle("enterprise"), row.enabled_modules)
    db.close()


def section_an_edit_to_the_manifest_reaches_every_path():
    print()
    print("=" * 74)
    print("2. MOVE A PACK OUT OF A PLAN IN THE MANIFEST: EVERY PATH FOLLOWS")
    print("=" * 74)
    # The drift this guards against, made to happen: 'factory' leaves growth and
    # enterprise. Each path must now leave it out, not keep a private copy.
    original = module_manifest.PACKS
    module_manifest.PACKS = [dict(p, plans=[x for x in p.get("plans", []) if x not in ("growth", "enterprise")])
                             if p["id"] == "factory" else p for p in original]
    try:
        db = fresh_db()
        check("CONTROL: the edited manifest's growth bundle has no factory pack",
              "factory" not in module_manifest.plan_bundles()["growth"],
              str(module_manifest.plan_bundles()["growth"]))
        c = platform_routes.apply_plan_tier(db, "EDIT_SAAS", "Professional")
        check("SaaS plan change follows the edit", "factory" not in c.enabled_modules.split(","),
              c.enabled_modules)
        c = platform_routes.apply_plan_tier(db, "EDIT_UNKNOWN", "Mystery Plan")
        check("the fail-open enterprise bundle follows the edit",
              "factory" not in c.enabled_modules.split(","), c.enabled_modules)
        c = platform_routes.get_or_create_config(db, "GMATS")
        check("a seeded tenant follows the edit", "factory" not in c.enabled_modules.split(","),
              c.enabled_modules)
        row = models.TenantConfig(tenant_code="EDIT_BARE")
        db.add(row)
        db.commit()
        db.refresh(row)
        check("the model default follows the edit", "factory" not in row.enabled_modules.split(","),
              row.enabled_modules)
        db.close()
    finally:
        module_manifest.PACKS = original
    check("CONTROL: the manifest is restored", "factory" in module_manifest.plan_bundles()["growth"])


def bundle_literals(source, pack_ids):
    """String constants in `source` that are a CSV of two or more pack ids."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "," in node.value:
            parts = node.value.split(",")
            if len(parts) >= 2 and all(p in pack_ids for p in parts):
                found.append((node.lineno, node.value))
    return found


def section_no_second_copy():
    print()
    print("=" * 74)
    print("3. NO SOURCE FILE SPELLS A BUNDLE OUT AGAIN")
    print("=" * 74)
    pack_ids = module_manifest.valid_pack_ids()
    check("CONTROL: the scan recognises a bundle literal",
          bundle_literals('X = {"growth": "core,operations,factory"}', pack_ids) == [(1, "core,operations,factory")])
    check("CONTROL: ...and ignores other text", not bundle_literals('X = "core, but not a bundle,here"', pack_ids))
    offenders = []
    scanned = 0
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        scanned += 1
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            for line, value in bundle_literals(fh.read(), pack_ids):
                offenders.append(f"{name}:{line} {value!r}")
    check(f"the scan read the backend's modules ({scanned} files, platform_routes and models included)",
          scanned > 50)
    check("no backend module spells out a plan's packs (modules.json does)", not offenders,
          "; ".join(offenders))


def main():
    section_every_path_reads_the_manifest()
    section_an_edit_to_the_manifest_reaches_every_path()
    section_no_second_copy()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_plan_bundles_one_rule():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
