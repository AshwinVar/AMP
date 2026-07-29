"""Who may read the customer registry.

company_tenants is founder data: every client's company code, name, industry,
plan, subscription status, seat count and monthly fee, plus the aggregate MRR
that /analytics/saas derives from it.

_registry_scope used to narrow on the WORKSPACE claim alone, so every role
inside the founder DEFAULT workspace read the whole registry. Measured before
the fix:

    founder ADMIN     sees 2 tenants  MRR=2400 seats=35
    founder OPERATOR  sees 2 tenants  MRR=2400 seats=35   <- identical
    client  OPERATOR  sees 1 tenant   MRR=600  seats=10   <- correctly scoped

Both endpoints are polled on every 3s dashboard round for every role, so this
was a continuous read rather than a theoretical one.

Two things these tests deliberately pin together: that a non-Admin cannot see
another tenant's row, AND that a client workspace still gets DATA rather than a
403 — the batched dashboard fetch depends on that, which is why the fix scopes
inside the helper instead of bolting require_roles onto the two endpoints.

Run:  python backend/test_saas_registry_authz.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
import saas_routes as sr
from database import Base

FOUNDER_ADMIN = {"sub": "founder", "role": "Admin", "tenant": "DEFAULT"}
FOUNDER_SUPER = {"sub": "fsup", "role": "Supervisor", "tenant": "DEFAULT"}
FOUNDER_OP = {"sub": "demo_op", "role": "Operator", "tenant": "DEFAULT"}
CLIENT_ADMIN = {"sub": "gmats_admin", "role": "Admin", "tenant": "GMATS"}
CLIENT_OP = {"sub": "gmats_op", "role": "Operator", "tenant": "GMATS"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for code, name, plan, seats, fee in [
        ("GMATS", "GMATS Compressors", "growth", 10, 600),
        ("ACME", "Acme Pressings Ltd", "enterprise", 25, 1800),
    ]:
        db.add(models.CompanyTenant(company_code=code, company_name=name, plan_name=plan,
                                    subscription_status="Active", seats=seats, monthly_fee=fee))
    db.commit()
    return db


def _codes(db, user):
    return sorted(r.company_code for r in sr.get_company_tenants(db=db, current_user=user))


def test_a_non_admin_in_the_founder_workspace_reads_no_customer_rows():
    """THE BUG. An Operator demo login in the platform workspace listed every
    client's plan, seat count and monthly fee."""
    db = _sess()
    assert _codes(db, FOUNDER_OP) == [], _codes(db, FOUNDER_OP)
    assert _codes(db, FOUNDER_SUPER) == [], _codes(db, FOUNDER_SUPER)
    print("PASS a founder-workspace non-Admin sees no customer rows")


def test_the_aggregate_leaks_no_more_than_the_listing():
    """/analytics/saas derives MRR and seats from the same table. Scoping the
    listing while leaving the rollup open would still publish the revenue."""
    db = _sess()
    for user in (FOUNDER_OP, FOUNDER_SUPER):
        agg = sr.get_saas_analytics(db=db, current_user=user)
        assert agg["total_tenants"] == 0, agg
        assert agg["monthly_recurring_revenue"] == 0, agg
        assert agg["total_seats"] == 0, agg
    print("PASS the MRR/seat rollup is scoped the same way as the listing")


def test_the_founder_admin_still_sees_the_whole_registry():
    """The panel has to keep working for the person it is for."""
    db = _sess()
    assert _codes(db, FOUNDER_ADMIN) == ["ACME", "GMATS"], _codes(db, FOUNDER_ADMIN)
    agg = sr.get_saas_analytics(db=db, current_user=FOUNDER_ADMIN)
    assert agg["total_tenants"] == 2, agg
    assert agg["monthly_recurring_revenue"] == 2400, agg
    assert agg["total_seats"] == 35, agg
    print("PASS the founder Admin still sees every tenant and the true MRR")


def test_a_client_workspace_still_gets_its_own_row_as_data_not_a_403():
    """The deliberate contract in _registry_scope's docstring: a client dashboard
    batches this call, so it must return data. Any role, own row only — this is
    what the trial banner reads, and it must not regress to an empty list or a
    403 for a client Operator."""
    db = _sess()
    for user in (CLIENT_ADMIN, CLIENT_OP):
        assert _codes(db, user) == ["GMATS"], (user["role"], _codes(db, user))
        agg = sr.get_saas_analytics(db=db, current_user=user)
        assert agg["total_tenants"] == 1, agg
        assert agg["monthly_recurring_revenue"] == 600, agg   # not 2400
    print("PASS a client workspace still gets its own row, as data, for any role")


def test_a_token_with_no_role_claim_fails_closed():
    """A malformed or legacy token must narrow, not open."""
    db = _sess()
    assert _codes(db, {"sub": "x", "tenant": "DEFAULT"}) == []
    assert _codes(db, {"sub": "x", "tenant": "DEFAULT", "role": None}) == []
    assert _codes(db, {"sub": "x", "tenant": "DEFAULT", "role": "admin"}) == []   # case-sensitive
    print("PASS a missing/odd role claim narrows rather than opening the registry")


def test_no_new_saas_endpoint_is_left_ungated():
    """Rot guard.

    Five of the seven registered SaaS routes carry require_roles(["Admin"]).
    The two that do not are the reads above, and they are safe only because
    _registry_scope narrows them — a fact no type or signature records. A new
    endpoint added with a bare get_current_user would be silently open, exactly
    as these two were, so the allowlist is spelled out and any addition to it
    has to be a deliberate edit.
    """
    SCOPED_BY_HELPER = {"/saas/tenants", "/analytics/saas"}

    ungated = set()
    for r in main.app.routes:
        path = getattr(r, "path", "")
        if not (path.startswith("/saas") or path == "/analytics/saas"):
            continue
        dep_names = {getattr(d.call, "__qualname__", "") for d in r.dependant.dependencies}
        if not any("role_checker" in n for n in dep_names):
            ungated.add(f"{sorted(r.methods)[0]} {path}")

    unexpected = {u for u in ungated if u.split(" ", 1)[1] not in SCOPED_BY_HELPER}
    assert not unexpected, (
        "SaaS endpoint(s) with no role dependency and not covered by "
        f"_registry_scope: {sorted(unexpected)}. Either add require_roles(['Admin']) "
        "or route the query through _registry_scope and add it to SCOPED_BY_HELPER."
    )
    # ...and the two that are exempt must still exist, or the allowlist is stale.
    assert ungated, "expected the two helper-scoped reads to be present"
    print(f"PASS every SaaS route is role-gated or helper-scoped ({len(ungated)} exempt)")


if __name__ == "__main__":
    test_a_non_admin_in_the_founder_workspace_reads_no_customer_rows()
    test_the_aggregate_leaks_no_more_than_the_listing()
    test_the_founder_admin_still_sees_the_whole_registry()
    test_a_client_workspace_still_gets_its_own_row_as_data_not_a_403()
    test_a_token_with_no_role_claim_fails_closed()
    test_no_new_saas_endpoint_is_left_ungated()
    print("SAAS REGISTRY AUTHZ OK: only a founder Admin reads the customer registry")
