"""GET /saas/tenants — response serialisation is NULL-safe (ADR-0008).

`plan_name` / `subscription_status` / `seats` / `monthly_fee` on company_tenants
are declared Column(..., default=...) WITHOUT nullable=False (models.py), so a
legacy / raw-SQL / migration row — or a row written before the update-handler's
write-boundary guard existed — can carry a NULL. CompanyTenantResponse typed all
four non-optional, so ONE such NULL row raised Pydantic ValidationError during
response serialisation and 500-ed the WHOLE GET /saas/tenants list (one poisoned
row hiding the entire tenant registry). test_saas_analytics_null_safe.py names
that symptom in its docstring but only ever exercised the /analytics/saas rollup
and the WRITE boundary — never the list serialisation itself. This pins it.

The list heal must agree with the /analytics/saas rollup (which COALESCEs a NULL
fee/seat to 0), so the per-tenant seats sum reconciles with the total-seats
headline (rule-3). plan_name / subscription_status heal to their declared string
defaults. A real recorded value — including a genuine 0 — is left untouched.

Run:  python backend/test_saas_tenants_list_null_safe.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import saas_routes
import schemas
from database import Base

FOUNDER = {"tenant": "DEFAULT", "sub": "founder", "role": "Admin"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add(db, code, plan, status, fee, seats):
    db.add(models.CompanyTenant(
        company_code=code, company_name=code.title(),
        plan_name=plan, subscription_status=status, monthly_fee=fee, seats=seats,
    ))


def _serialise_list(db, user):
    """Reproduce what FastAPI does for response_model=List[CompanyTenantResponse]:
    validate every ORM row the handler returns through the response schema. This
    is the exact step that raised before the heal — calling the handler alone did
    not, because the handler returns ORM objects and never serialises them."""
    rows = saas_routes.get_company_tenants(db, user)
    return [schemas.CompanyTenantResponse.model_validate(r) for r in rows]


def _seed(db):
    # Four fully-populated rows with independently-known numbers, plus one raw-SQL
    # NULL row the ORM default never touched.
    _add(db, "APEX", "Professional", "Active", 1000, 10)
    _add(db, "BOLT", "Starter", "Trial", 500, 5)
    _add(db, "CRUX", "Enterprise", "Cancelled", 2000, 20)
    _add(db, "DELTA", "Starter", "Active", 0, 0)      # a REAL zero-fee / zero-seat tenant
    _add(db, "NULLR", "Starter", "Trial", 100, 3)     # values overwritten to NULL below
    db.commit()
    db.execute(text(
        "UPDATE company_tenants "
        "SET plan_name=NULL, subscription_status=NULL, monthly_fee=NULL, seats=NULL "
        "WHERE company_code='NULLR'"
    ))
    db.commit()


def test_list_survives_null_row_and_heals_to_defaults():
    db = _fresh_session()
    _seed(db)

    items = _serialise_list(db, FOUNDER)   # must NOT raise (500 before the heal)
    assert len(items) == 5, len(items)

    by_code = {i.company_code: i for i in items}

    # The NULL row is healed: numeric columns -> 0 (the basis /analytics/saas uses),
    # string columns -> their declared defaults.
    nul = by_code["NULLR"]
    assert nul.seats == 0, nul.seats
    assert nul.monthly_fee == 0, nul.monthly_fee
    assert nul.plan_name == "Starter", nul.plan_name
    assert nul.subscription_status == "Trial", nul.subscription_status
    print("PASS a NULL registry row heals to seats=0/fee=0/plan=Starter/status=Trial")


def test_real_values_including_genuine_zero_are_untouched():
    db = _fresh_session()
    _seed(db)
    by_code = {i.company_code: i for i in _serialise_list(db, FOUNDER)}

    # DELTA's real 0 fee / 0 seats must survive as real 0s (not confused with NULL).
    assert by_code["DELTA"].seats == 0 and by_code["DELTA"].monthly_fee == 0
    assert by_code["DELTA"].subscription_status == "Active"
    # A fully-populated row is byte-for-byte unchanged.
    assert by_code["APEX"].plan_name == "Professional"
    assert by_code["APEX"].subscription_status == "Active"
    assert by_code["APEX"].seats == 10 and by_code["APEX"].monthly_fee == 1000
    assert by_code["CRUX"].plan_name == "Enterprise" and by_code["CRUX"].seats == 20
    print("PASS real recorded values (incl. a genuine 0) are untouched by the heal")


def test_list_seats_reconcile_with_analytics_total_seats():
    # rule-3: the per-tenant list and the /analytics/saas headline share a basis.
    # Analytics COALESCEs a NULL seat to 0; the healed list does too, so the sum
    # of the list's seats must equal the analytics total_seats exactly.
    db = _fresh_session()
    _seed(db)

    items = _serialise_list(db, FOUNDER)
    list_seats = sum(i.seats for i in items)

    a = saas_routes.get_saas_analytics(db, FOUNDER)
    # Independently derived: 10 + 5 + 20 + 0 + NULL(0) = 35.
    assert a["total_seats"] == 35, a
    assert list_seats == a["total_seats"], (list_seats, a["total_seats"])

    # And MRR is unaffected by the display heal (raw NULL status stays outside the
    # Trial+Active filter): APEX 1000 + BOLT 500 + DELTA 0 = 1500.
    assert a["monthly_recurring_revenue"] == 1500, a
    print("PASS per-tenant seats sum reconciles with analytics total_seats (35)")


def test_empty_registry_serialises_to_empty_list():
    db = _fresh_session()
    items = _serialise_list(db, FOUNDER)
    assert items == [], items
    print("PASS an empty registry serialises to an empty list (no crash)")


if __name__ == "__main__":
    test_list_survives_null_row_and_heals_to_defaults()
    test_real_values_including_genuine_zero_are_untouched()
    test_list_seats_reconcile_with_analytics_total_seats()
    test_empty_registry_serialises_to_empty_list()
    print("ALL SAAS TENANTS LIST NULL-SAFE TESTS PASSED")
