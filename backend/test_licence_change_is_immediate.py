"""Revoking a module left it usable for another minute.

THE DEFECT
----------
`plan_gate` caches each tenant's licensed packs for 60 seconds
(`_CACHE_TTL_SECONDS`), so the API gate does not hit the database on every
request. Any write that changes a tenant's licence must therefore drop that
cache entry, or the gate keeps enforcing the OLD licence.

Four handlers write a tenant's licence. Three call `plan_gate.invalidate`:

  * `platform_routes.apply_plan_tier`                (used at tenant creation)
  * `PATCH /tenant-configs/{tenant_code}`            (update_tenant_license)
  * `POST  /tenant-configs/{tenant_code}/apply-plan` (apply_plan)

The fourth, `PATCH /tenant-config` (`update_tenant_config`), does not. It sets
`plan` and `enabled_modules` on the platform-owner branch and commits, and the
gate goes on answering from the stale entry.

WHAT MAKES IT WORTH A TEST RATHER THAN A ONE-LINE PATCH
-------------------------------------------------------
The comment sitting in `update_tenant_license`, sixty lines below, reads:

    # A licence change must take effect at once — the plan-gate caches each
    # tenant's packs for ~60s, so drop the stale entry (the self-service
    # update_tenant_config already does this; this cross-tenant path didn't).

`update_tenant_config` did not already do this. So the rule was written out at
three call sites, omitted at a fourth, and the omission was then documented as
if it were handled — which is how the next person reading that comment would
also conclude there was nothing to add.

It also hides from grep. `update_tenant_config` assigns the plan through
`setattr(c, f, payload[f])` over a tuple of field names, so `grep "\.plan ="`
finds the three that were right and not the one that was wrong.

The direction that matters is REVOCATION. Granting a module late is a nuisance;
continuing to serve a module after the licence for it was withdrawn is the gate
failing at the only job it has. `plan_gate` is deliberately fail-open on a
database error (availability beats enforcement), but that is a decision about
an UNREADABLE licence — not a licence that was read, changed, and ignored.

THE FIX THIS SUITE PINS
-----------------------
Not a fourth copy of the rule. One `commit_tenant_config(db, tenant_code)` that
commits and invalidates together, so "a tenant-config write takes effect at
once" is a property of the only way to write one, rather than something four
call sites each have to remember. The structural check below is the part that
holds: it fails for a FIFTH handler added later that commits on its own.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_licence_change_is_immediate.py
"""
import ast
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import plan_gate  # noqa: E402
import platform_routes  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402

failures = []

HERE = os.path.dirname(os.path.abspath(__file__))


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


class _GateSession:
    """plan_gate opens its OWN session (it runs in middleware, not a request).

    Point it at the test's session so the gate reads the same rows the handler
    writes, and make close() a no-op so the gate cannot close the session out
    from under the rest of the test.
    """

    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    def query(self, *a, **kw):
        return self.db.query(*a, **kw)

    def add(self, *a, **kw):
        return self.db.add(*a, **kw)

    def commit(self):
        return self.db.commit()

    def refresh(self, *a, **kw):
        return self.db.refresh(*a, **kw)

    def close(self):
        pass


def _owner():
    """The founder, acting on a tenant they have switched into.

    `is_platform_owner` is `current_user["tenant"] == "DEFAULT"`, and the tenant
    being edited comes from `tenancy.current_tenant()` — which is exactly the
    documented flow: "the founder, while switched, edits the previewed tenant".
    """
    return {"sub": "founder@marx8.com", "tenant": "DEFAULT", "role": "Admin"}


def main():
    print("=" * 74)
    print("1. REVOKING A MODULE TAKES EFFECT NOW, NOT IN A MINUTE")
    print("=" * 74)
    db = _fresh_session()
    db.add(models.TenantConfig(tenant_code="ACME", plan="enterprise",
                               subscription_status="active",
                               enabled_modules="core,operations,factory,intelligence,admin",
                               brand_name="Acme"))
    db.commit()

    real_session_local = plan_gate.SessionLocal
    plan_gate.SessionLocal = _GateSession(db)
    token = tenancy.set_current_tenant("ACME")
    try:
        plan_gate._licence_cache.clear()

        # Prime the cache the way a single real request does.
        before = plan_gate.licensed_packs("ACME")
        check("the gate sees the licence the tenant actually has",
              before is not None and "intelligence" in before, repr(before))
        check("...and has cached it, which is what makes this a race at all",
              "ACME" in plan_gate._licence_cache, "nothing was cached")

        # The founder, switched into ACME, withdraws the intelligence pack.
        platform_routes.update_tenant_config(
            {"enabled_modules": ["core", "operations", "factory", "admin"]},
            db, _owner())

        after = plan_gate.licensed_packs("ACME")
        check("the withdrawn pack is refused IMMEDIATELY",
              after is not None and "intelligence" not in after, repr(after))
        check("...and the packs that were kept still work",
              after is not None and "factory" in after, repr(after))

        # Granting is the same rule in the other direction.
        platform_routes.update_tenant_config(
            {"enabled_modules": ["core", "operations", "factory", "intelligence", "admin"]},
            db, _owner())
        regranted = plan_gate.licensed_packs("ACME")
        check("a re-granted pack is allowed immediately too",
              regranted is not None and "intelligence" in regranted, repr(regranted))

        print()
        print("=" * 74)
        print("2. A PLAN CHANGE IS A LICENCE CHANGE")
        print("=" * 74)
        # `plan` arrives through setattr() over a tuple of field names, which is
        # why grepping for ".plan =" never found this handler.
        plan_gate._licence_cache.clear()
        plan_gate.licensed_packs("ACME")
        platform_routes.update_tenant_config({"plan": "starter"}, db, _owner())
        check("changing the plan drops the cached licence as well",
              "ACME" not in plan_gate._licence_cache
              or plan_gate._licence_cache["ACME"][1] <= time.time(),
              repr(plan_gate._licence_cache.get("ACME")))

        print()
        print("=" * 74)
        print("3. A BRANDING-ONLY EDIT IS NOT A SPECIAL CASE")
        print("=" * 74)
        # Deliberately not "only invalidate when the licence fields changed".
        # That rule needs a correct list of which fields are licence fields, and
        # a wrong list is this same bug again. Dropping one cache entry costs one
        # query; getting the list wrong costs enforcement.
        plan_gate._licence_cache.clear()
        plan_gate.licensed_packs("ACME")
        platform_routes.update_tenant_config({"brand_name": "Acme Ltd"}, db, _owner())
        check("a branding write invalidates too, rather than reasoning about "
              "which fields were licence fields",
              "ACME" not in plan_gate._licence_cache,
              repr(plan_gate._licence_cache.get("ACME")))
    finally:
        tenancy.reset_current_tenant(token)
        plan_gate.SessionLocal = real_session_local
        plan_gate._licence_cache.clear()

    print()
    print("=" * 74)
    print("4. THE RULE HAS ONE HOME, SO A FIFTH HANDLER CANNOT FORGET IT")
    print("=" * 74)
    # The structural half. The behavioural checks above only prove the handler
    # that exists today is right; this one fails when someone adds the next
    # licence writer and commits it by hand.
    src = io.open(os.path.join(HERE, "platform_routes.py"),
                  encoding="utf-8", newline="").read()
    tree = ast.parse(src)

    LICENCE_FIELDS = {"plan", "enabled_modules"}

    def touches_licence(fn):
        """Does this function handle a tenant's plan or module list?

        Two forms, because the defect hid in the second. `c.plan = ...` is an
        attribute assignment; `setattr(c, f, payload[f])` over a literal tuple
        of field names is not, which is why `grep "\\.plan ="` found the three
        handlers that were right and missed the one that was wrong. Matching the
        field NAME wherever it appears covers both without having to model
        every way a field can be written.
        """
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr in LICENCE_FIELDS:
                        return True
            if isinstance(node, ast.Constant) and node.value in LICENCE_FIELDS:
                return True
        return False

    def calls(fn, name):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                if getattr(f, "id", "") == name or getattr(f, "attr", "") == name:
                    return True
        return False

    # "...and commits" is what separates a writer from a reader. `_config_dict`
    # names both licence fields while only serialising them; requiring it to
    # commit through anything would be nonsense.
    writers = [fn for fn in ast.walk(tree)
               if isinstance(fn, ast.FunctionDef) and touches_licence(fn)
               and (calls(fn, "commit") or calls(fn, "commit_tenant_config"))]
    check("the licence writers are still found by this guard "
          "(a guard that matches nothing reports all-clear)",
          len(writers) >= 4, f"found {len(writers)}")

    offenders = [fn.name for fn in writers
                 if not calls(fn, "commit_tenant_config")]
    check("every licence writer commits through commit_tenant_config",
          offenders == [], f"these commit on their own: {offenders}")

    bare = [fn.name for fn in writers if calls(fn, "commit")
            and not calls(fn, "commit_tenant_config")]
    check("...and none of them calls db.commit() directly", bare == [], repr(bare))

    # One home for the invalidation itself, in this module.
    invalidators = [fn.name for fn in ast.walk(tree)
                    if isinstance(fn, ast.FunctionDef) and calls(fn, "invalidate")]
    check("plan_gate.invalidate is called from exactly one function here",
          invalidators == ["commit_tenant_config"], repr(invalidators))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print("LICENCE CHANGE OK: every tenant-config write takes effect at once, "
          "through one commit path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
