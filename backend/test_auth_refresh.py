"""Sliding-session token refresh tests (security PR2: DB revalidation).

POST /auth/refresh exchanges a valid token for a fresh one — but the fresh
token's claims are RE-DERIVED FROM THE DATABASE, never copied from the old
token. These tests pin that: a role change is picked up on the next slide, a
deleted user is refused (401), and a cancelled / trial-expired tenant is
refused (403) exactly like /login.

Run:  python backend/test_auth_refresh.py     (exit 0 = pass)
"""
import time
from datetime import datetime, timedelta

from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core_routes
import models
from auth import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _user(db, username, role="Supervisor", tenant="GMATS"):
    u = models.User(username=username, password="x", role=role, tenant_code=tenant)
    db.add(u)
    db.commit()
    return u


def test_refresh_rederives_identity_from_the_db_with_fresh_expiry():
    db = _fresh_session()
    _user(db, "tester", role="Supervisor", tenant="GMATS")
    before = time.time()

    out = core_routes.refresh_token(db=db, current_user={"sub": "tester", "role": "Supervisor", "tenant": "GMATS"})
    assert out["token_type"] == "bearer"
    payload = jwt.decode(out["access_token"], SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["sub"] == "tester"
    assert payload["role"] == "Supervisor"
    assert payload["tenant"] == "GMATS"
    expected = before + ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert abs(payload["exp"] - expected) < 120
    print("PASS refresh re-issues the DB identity with a fresh expiry")


def test_refresh_picks_up_a_role_change_not_the_stale_claim():
    db = _fresh_session()
    u = _user(db, "demoted", role="Operator", tenant="GMATS")   # DB says Operator now

    # the old token still claims Admin — the fresh token must NOT keep it
    out = core_routes.refresh_token(db=db, current_user={"sub": "demoted", "role": "Admin", "tenant": "GMATS"})
    payload = jwt.decode(out["access_token"], SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["role"] == "Operator", "stale Admin claim must not survive a refresh"
    assert out["role"] == "Operator"

    # same for tenant: DB moves the user, the slide follows the DB
    u.tenant_code = "ACME"
    db.commit()
    out2 = core_routes.refresh_token(db=db, current_user={"sub": "demoted", "role": "Operator", "tenant": "GMATS"})
    assert jwt.decode(out2["access_token"], SECRET_KEY, algorithms=[ALGORITHM])["tenant"] == "ACME"
    print("PASS a role/tenant change lands on the next slide (stale claims dropped)")


def test_refresh_refuses_a_deleted_user():
    db = _fresh_session()   # no users at all
    try:
        core_routes.refresh_token(db=db, current_user={"sub": "ghost", "role": "Admin", "tenant": "DEFAULT"})
        assert False, "a deleted user's token must not refresh"
    except Exception as e:
        assert getattr(e, "status_code", None) == 401, e
    print("PASS a deleted user's session ends at the next slide (401)")


def test_refresh_refuses_a_cancelled_or_expired_tenant():
    db = _fresh_session()
    _user(db, "worker", role="Operator", tenant="ACME")
    db.add(models.CompanyTenant(company_code="ACME", company_name="Acme", plan_name="Starter",
                                subscription_status="Cancelled", seats=5, monthly_fee=0))
    db.commit()
    try:
        core_routes.refresh_token(db=db, current_user={"sub": "worker", "role": "Operator", "tenant": "ACME"})
        assert False, "a cancelled tenant's token must not refresh"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403, e

    # trial expired blocks the same way (trial_expired derives from created_at
    # + TRIAL_DAYS, so backdate the registry row past the trial window)
    db2 = _fresh_session()
    _user(db2, "trialist", role="Operator", tenant="TRIALCO")
    db2.add(models.CompanyTenant(company_code="TRIALCO", company_name="TrialCo", plan_name="Starter",
                                 subscription_status="Trial", seats=5, monthly_fee=0,
                                 created_at=datetime.utcnow() - timedelta(days=models.TRIAL_DAYS + 1)))
    db2.commit()
    try:
        core_routes.refresh_token(db=db2, current_user={"sub": "trialist", "role": "Operator", "tenant": "TRIALCO"})
        assert False, "an expired trial's token must not refresh"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403, e
    print("PASS a cancelled / trial-expired tenant is refused (403), mirroring login")


if __name__ == "__main__":
    test_refresh_rederives_identity_from_the_db_with_fresh_expiry()
    test_refresh_picks_up_a_role_change_not_the_stale_claim()
    test_refresh_refuses_a_deleted_user()
    test_refresh_refuses_a_cancelled_or_expired_tenant()
    print("AUTH REFRESH OK: the sliding session re-derives identity from the DB — "
          "role changes land, deleted users end, cancelled tenants stop")
