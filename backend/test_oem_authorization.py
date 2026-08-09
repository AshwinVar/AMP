"""An OEM principal cannot reach factory data, and cannot be forged (ADR-0017).

The keystone is structural, not procedural: an OEM request binds a SENTINEL
factory tenant that no factory can hold, so the ADR-0002 hook filters every
scoped table to a string no row carries. Factory operational data is invisible to
an OEM BY CONSTRUCTION — before any OEM route exists.

This suite attacks that from both ends: forged and stale credentials on the way
in, and the tenant binding on the way through.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_authorization.py
"""
import inspect
import os
import sys
from datetime import datetime, timedelta

from fastapi import HTTPException
from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import models
import oem_auth
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
ALGO = getattr(auth, "ALGORITHM", "HS256")

failures = []
_open = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()
    _open.append(db)
    return db


def seed(db):
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha", is_active=True))
    db.add(models.OemOrganization(oem_code="OEM_BETA", name="Beta", is_active=True))
    db.add(models.OemOrganization(oem_code="OEM_GONE", name="Gone", is_active=False))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN", is_active=True))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-viewer",
                          password="x", role="OEM_VIEWER", is_active=True))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-suspended",
                          password="x", role="OEM_ADMIN", is_active=False))
    db.add(models.OemUser(oem_code="OEM_BETA", username="beta-admin",
                          password="x", role="OEM_ADMIN", is_active=True))
    db.add(models.OemUser(oem_code="OEM_GONE", username="gone-admin",
                          password="x", role="OEM_ADMIN", is_active=True))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-leaver",
                          password="x", role="OEM_ADMIN", is_active=True))
    # A user whose organisation row does not exist. Without this the "unknown
    # organisation" case never reaches the org lookup at all — it is refused
    # earlier by the OEM-mismatch check, and the org guard mutation-tested as
    # dead. A guard nothing reaches is not a guard.
    db.add(models.OemUser(oem_code="OEM_ORPHAN", username="orphan-admin",
                          password="x", role="OEM_ADMIN", is_active=True))
    # A user carrying a FACTORY role string. The role is read from the database,
    # so this is the row that decides whether a factory role can authenticate
    # into the OEM portal.
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-factoryrole",
                          password="x", role="Admin", is_active=True))
    db.add(models.User(username="factory-admin", password="x", role="Admin",
                       tenant_code="FACTORY_A", is_active=True))
    db.commit()
    db.query(models.OemUser).filter(
        models.OemUser.username == "alpha-leaver").delete(synchronize_session=False)
    db.commit()


def claims(sub, oem, principal="oem", role="OEM_ADMIN", **extra):
    c = {"sub": sub, "role": role, "principal": principal, "oem": oem,
         "exp": datetime.utcnow() + timedelta(hours=1)}
    c.update(extra)
    return c


def refused(db, c):
    """(status, reason). The REASON is returned because a refusal by one guard
    and a refusal by the next look identical otherwise — two guards
    mutation-tested as dead until the reason was asserted."""
    try:
        oem_auth.resolve(db, c)
        return None, None
    except oem_auth.OemDenied as e:
        return e.status_code, e.detail


# =====================================================================
def test_the_gate_refuses_forged_and_stale_credentials():
    print("=" * 74)
    print("1. WHO IS LET IN")
    print("=" * 74)
    db = fresh_db()
    seed(db)

    # Each row asserts the CODE and the REASON. The reason matters: several of
    # these are refused by more than one guard, and only the reason says which
    # one actually fired — without it, two real guards mutation-tested as dead.
    cases = [
        ("a factory token is not an OEM session",
         {"sub": "factory-admin", "role": "Admin", "tenant": "FACTORY_A"},
         401, "Not an OEM session"),
        ("a token with principal=oem but no oem code",
         claims("alpha-admin", None), 401, "Not an OEM session"),
        ("a token naming no user", claims(None, "OEM_ALPHA"),
         401, "Token names no user"),
        ("a DELETED OEM user", claims("alpha-leaver", "OEM_ALPHA"),
         401, "This account no longer exists"),
        ("a DISABLED OEM user", claims("alpha-suspended", "OEM_ALPHA"),
         403, "This account has been disabled"),
        ("a user of ANOTHER OEM claiming this one",
         claims("beta-admin", "OEM_ALPHA"), 403, "Token does not match this account"),
        ("an OEM user carrying a FACTORY role cannot authenticate",
         claims("alpha-factoryrole", "OEM_ALPHA"), 403, "Not an OEM role"),
        ("a SUSPENDED organisation", claims("gone-admin", "OEM_GONE"),
         403, "This organisation has been suspended"),
        ("a user whose ORGANISATION no longer exists",
         claims("orphan-admin", "OEM_ORPHAN"), 403,
         "This organisation no longer exists"),
        ("principal claim missing entirely",
         {"sub": "alpha-admin", "oem": "OEM_ALPHA", "role": "OEM_ADMIN"},
         401, "Not an OEM session"),
        ("principal claim says factory",
         claims("alpha-admin", "OEM_ALPHA", principal="factory"),
         401, "Not an OEM session"),
        ("the oem claim is a list", claims("alpha-admin", ["OEM_ALPHA"]),
         401, "Not an OEM session"),
    ]
    for label, c, code, reason in cases:
        got, detail = refused(db, c)
        check(label, got == code and detail == reason,
              f"got {got}/{detail!r}, expected {code}/{reason!r}")

    # The ROLE comes from the database, not the token: a token claiming "Admin"
    # for an OEM_ADMIN account resolves to OEM_ADMIN, because the claim is never
    # trusted. That is correct behaviour, not a bypass.
    p = oem_auth.resolve(db, claims("alpha-admin", "OEM_ALPHA", role="Admin"))
    check("a forged role claim is ignored; the database decides",
          p["role"] == "OEM_ADMIN", p["role"])

    # CONTROL: a real OEM principal still resolves, or everything above is
    # satisfied by a gate that refuses everything.
    p = oem_auth.resolve(db, claims("alpha-admin", "OEM_ALPHA"))
    check("CONTROL: a valid OEM admin resolves",
          p["oem"] == "OEM_ALPHA" and p["role"] == "OEM_ADMIN", str(p))


def test_the_sentinel_is_bound_and_terminal():
    print()
    print("=" * 74)
    print("2. AN OEM TOKEN BINDS A TENANT NO FACTORY CAN HOLD")
    print("=" * 74)

    oem = claims("alpha-admin", "OEM_ALPHA")
    bound = tenancy.effective_tenant(oem.get("tenant"), None, oem.get("role"), oem)
    check("an OEM token binds the sentinel", bound == "OEM:OEM_ALPHA", str(bound))
    check("...which is never None (None disables the filter entirely)",
          bound is not None)
    check("...and is recognisable as a sentinel", oem_auth.is_sentinel(bound))

    # The founder-preview branch must NOT be reachable from an OEM token. An
    # X-Tenant header plus a forged tenant/role claim is the exact attack.
    forged = claims("alpha-admin", "OEM_ALPHA", tenant="DEFAULT", role="Admin")
    attacked = tenancy.effective_tenant("DEFAULT", "FACTORY_A", "Admin", forged)
    check("an OEM token + X-Tenant + forged Admin still binds the sentinel",
          attacked == "OEM:OEM_ALPHA", str(attacked))

    # CONTROLS: the factory paths are unchanged.
    fac = {"tenant": "FACTORY_A", "role": "Admin"}
    check("CONTROL: a factory token still binds its own tenant",
          tenancy.effective_tenant("FACTORY_A", None, "Admin", fac) == "FACTORY_A")
    founder = {"tenant": "DEFAULT", "role": "Admin"}
    check("CONTROL: the founder company-switcher still works",
          tenancy.effective_tenant("DEFAULT", "ACME", "Admin", founder) == "ACME")
    check("CONTROL: a non-Admin founder still cannot preview",
          tenancy.effective_tenant("DEFAULT", "ACME", "Operator",
                                   {"tenant": "DEFAULT", "role": "Operator"}) == "DEFAULT")


def test_the_sentinel_hides_every_factory_table():
    print()
    print("=" * 74)
    print("3. WITH THE SENTINEL BOUND, FACTORY DATA IS INVISIBLE")
    print("=" * 74)
    db = fresh_db()
    seed(db)
    tok = tenancy.set_current_tenant(None)
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.Machine(tenant_code=t, site="Plant 1", name="COMP-001",
                              status="Running", utilization=70))
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001",
                                part_number="SECRET-PART", batch_number="B1",
                                target_quantity=10, status="Planned"))
        db.add(models.InventoryItem(tenant_code=t, item_code="INV-001",
                                    item_name="Steel", category="Raw", unit="kg",
                                    current_stock=100, reorder_level=5))
        db.add(models.ProductionRecord(tenant_code=t, planned_minutes=480,
                                       runtime_minutes=400,
                                       ideal_cycle_time_seconds=30,
                                       total_count=600, good_count=570,
                                       rejected_count=30))
        db.add(models.CostRecord(tenant_code=t, cost_no=f"C-{t}", cost_type="Labour",
                                 description="confidential", amount=100))
    db.commit()
    tenancy.reset_current_tenant(tok)

    watched = {"machines": models.Machine, "work orders": models.WorkOrder,
               "inventory": models.InventoryItem,
               "production": models.ProductionRecord, "costs": models.CostRecord}

    tok = tenancy.set_current_tenant(oem_auth.sentinel_tenant("OEM_ALPHA"))
    seen = {k: db.query(m).count() for k, m in watched.items()}
    tenancy.reset_current_tenant(tok)
    check("an OEM sees ZERO rows across every factory table",
          all(v == 0 for v in seen.values()), str(seen))

    tok = tenancy.set_current_tenant("FACTORY_A")
    own = {k: db.query(m).count() for k, m in watched.items()}
    tenancy.reset_current_tenant(tok)
    check("CONTROL: a factory still sees its own rows",
          all(v == 1 for v in own.values()), str(own))

    tok = tenancy.set_current_tenant(None)
    unbound = {k: db.query(m).count() for k, m in watched.items()}
    tenancy.reset_current_tenant(tok)
    check("CONTROL: unbound sees BOTH factories (why the sentinel exists)",
          all(v == 2 for v in unbound.values()), str(unbound))

    # Two OEMs get two different sentinels, so one cannot ride the other's.
    check("two OEMs bind different sentinels",
          oem_auth.sentinel_tenant("OEM_ALPHA") != oem_auth.sentinel_tenant("OEM_BETA"))


def test_capabilities_are_enforced_per_role():
    print()
    print("=" * 74)
    print("4. OEM ROLES GRANT DIFFERENT CAPABILITIES")
    print("=" * 74)
    caps = oem_auth.ROLE_CAPABILITIES
    check("every OEM role may read the fleet",
          all("read_fleet" in v for v in caps.values()))
    check("only OEM_ADMIN manages users",
          {r for r, v in caps.items() if "manage_users" in v} == {"OEM_ADMIN"},
          str({r for r, v in caps.items() if "manage_users" in v}))
    check("only OEM_ADMIN manages branding",
          {r for r, v in caps.items() if "manage_branding" in v} == {"OEM_ADMIN"})
    check("a viewer cannot manage service",
          "manage_service" not in caps["OEM_VIEWER"])
    check("a viewer cannot commission", "commission" not in caps["OEM_VIEWER"])
    check("an engineer can commission but not manage users",
          "commission" in caps["OEM_SERVICE_ENGINEER"]
          and "manage_users" not in caps["OEM_SERVICE_ENGINEER"])
    check("no OEM role is a factory role",
          all(r.startswith("OEM_") for r in caps))
    check("the factory role strings grant nothing here",
          all(r not in caps for r in ("Admin", "Supervisor", "Operator")))


def test_the_two_principal_types_cannot_cross():
    print()
    print("=" * 74)
    print("5. THE TWO PRINCIPAL TYPES CANNOT BECOME EACH OTHER")
    print("=" * 74)
    db = fresh_db()
    seed(db)

    # A factory admin's token cannot resolve as an OEM principal even if the
    # username happens to exist on the other side.
    check("a factory admin cannot resolve as an OEM principal",
          refused(db, {"sub": "factory-admin", "role": "Admin",
                       "principal": "oem", "oem": "OEM_ALPHA"})[0] == 401)

    # An OEM token carries no tenant claim to give a factory route.
    src = inspect.getsource(oem_auth.resolve)
    check("oem_auth.resolve never reads a `tenant` claim",
          '"tenant"' not in src and "'tenant'" not in src)
    mid = inspect.getsource(tenancy.effective_tenant)
    check("the OEM branch returns before the founder-preview branch",
          mid.index("oem_auth.oem_of_claims") < mid.index("header_tenant and claim_role"))

    # The role check reads the database, so a demotion applies immediately.
    row = db.query(models.OemUser).filter(
        models.OemUser.username == "alpha-admin").one()
    row.role = "OEM_VIEWER"
    db.commit()
    p = oem_auth.resolve(db, claims("alpha-admin", "OEM_ALPHA", role="OEM_ADMIN"))
    check("a demotion takes effect on the next request, not at token expiry",
          p["role"] == "OEM_VIEWER", p["role"])


def run_all():
    test_the_gate_refuses_forged_and_stale_credentials()
    test_the_sentinel_is_bound_and_terminal()
    test_the_sentinel_hides_every_factory_table()
    test_capabilities_are_enforced_per_role()
    test_the_two_principal_types_cannot_cross()


def test_oem_authorization():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("AN OEM PRINCIPAL CANNOT BE FORGED, AND CANNOT SEE A FACTORY")
