"""Sliding-session token refresh test.

POST /auth/refresh exchanges a valid token for a fresh one carrying the same
identity claims (sub / role / tenant) with a new expiry.
Run:  python backend/test_auth_refresh.py     (exit 0 = pass)
"""
import time

from jose import jwt

import main
from auth import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES


def test_refresh_reissues_the_same_identity_with_fresh_expiry():
    user = {"sub": "tester", "role": "Supervisor", "tenant": "GMATS"}
    before = time.time()
    out = main.refresh_token(current_user=user)

    assert out["token_type"] == "bearer"
    payload = jwt.decode(out["access_token"], SECRET_KEY, algorithms=[ALGORITHM])
    # identity claims survive the exchange
    assert payload["sub"] == "tester"
    assert payload["role"] == "Supervisor"
    assert payload["tenant"] == "GMATS"
    # fresh expiry: roughly now + ACCESS_TOKEN_EXPIRE_MINUTES
    expected = before + ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert abs(payload["exp"] - expected) < 120

    # a caller with no tenant claim defaults to DEFAULT
    out2 = main.refresh_token(current_user={"sub": "x", "role": "Admin"})
    assert jwt.decode(out2["access_token"], SECRET_KEY, algorithms=[ALGORITHM])["tenant"] == "DEFAULT"


if __name__ == "__main__":
    test_refresh_reissues_the_same_identity_with_fresh_expiry()
    print("AUTH REFRESH OK: /auth/refresh reissues sub/role/tenant with a fresh expiry; tenant defaults to DEFAULT")
