"""The JWT boundary's security contract, pinned at the function level.

`auth.py` is the gate every authenticated route passes through, and three of
its guarantees were only ever exercised sideways — minted as a helper to get a
token for some OTHER test, or asserted through an HTTP round-trip that could
keep passing while the guarantee itself regressed:

  * `verify_token` must REJECT (HTTP 401) an expired token and a forged one (a
    token signed with any key other than this process's SECRET_KEY). A decoder
    that quietly accepted either would let a stale or minted token through.

  * `decode_token_optional` must FAIL SAFE — return None, never raise and never
    a payload — for a missing, malformed, expired or forged token, because it
    backs non-critical tenant scoping where a raise would 500 the request. It
    had NO test at all; nothing stopped a future edit from letting it throw, or
    worse, return a half-decoded dict for an unverified token.

  * `create_access_token` must stamp its OWN expiry, overriding any `exp` a
    caller passed in data — otherwise a caller handing in a stale/forever `exp`
    would mint a token that never expires or is dead on arrival.

Plus the two authorization guards every factory route leans on:
`get_current_user` turns an OEM principal away from the factory API (403,
ADR-0017), and `require_roles` refuses a role outside its allow-list (403).

These are pinned as direct calls, with independently-derived expectations (the
401/403 codes and the None fail-safe come from the contract, not from echoing
the code), so a regression here fails THIS suite rather than surfacing as a
mystery auth outage.

The crafted tokens are signed against `auth.SECRET_KEY` as the module actually
resolved it (a real on-key token for the expiry case) and against a key
GUARANTEED to differ from it (the forged case) — never a hard-coded value — so
the suite is correct whether auth booted on a set key or a per-process
ephemeral dev key, and whether it was imported before or after this module in a
combined test run.

Run:  python backend/test_auth_token_contract.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

import auth

# A key that is DEFINITELY not auth's signing key, whatever auth resolved to.
# Deriving it from auth.SECRET_KEY (rather than hard-coding one) means it stays
# different even in the astronomically-unlikely case that a random dev key
# collided with a constant — the assertion below makes that certainty explicit.
_FORGED_KEY = (auth.SECRET_KEY or "") + "::definitely-not-the-real-signing-key"
assert _FORGED_KEY != auth.SECRET_KEY, "forged key must differ from the real signing key"


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    """The credentials object FastAPI's HTTPBearer would hand get_current_user."""
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _expect_http(fn, status, label):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == status, f"{label}: expected {status}, got {exc.status_code}"
        return exc
    raise AssertionError(f"{label}: expected HTTP {status}, but no HTTPException was raised")


def test_the_signing_key_is_usable():
    # The fixtures below assume auth has a resolved signing key and the expected
    # algorithm; assert that up front so a misconfigured auth fails loudly here
    # rather than as a confusing downstream signature error.
    assert isinstance(auth.SECRET_KEY, str) and auth.SECRET_KEY, "auth has no signing key"
    assert auth.ALGORITHM == "HS256"
    print("PASS auth exposes a usable HS256 signing key")


def test_create_and_verify_roundtrip_preserves_claims():
    token = auth.create_access_token(
        {"sub": "u1", "role": "Supervisor", "tenant": "GMATS", "principal": "factory"})
    payload = auth.verify_token(token)
    assert payload["sub"] == "u1"
    assert payload["role"] == "Supervisor"
    assert payload["tenant"] == "GMATS"
    assert payload["principal"] == "factory"
    # A freshly minted token is not expired: its exp is in the future.
    assert payload["exp"] > datetime.utcnow().timestamp(), "fresh token already expired"
    print("PASS create/verify round-trips sub/role/tenant/principal and sets a future exp")


def test_create_access_token_overrides_a_caller_supplied_exp():
    # A caller who slips a past `exp` into the data dict must NOT get a
    # dead-on-arrival token — create_access_token stamps its own expiry. (The
    # same override protects against a caller passing a far-future/forever exp.)
    stale = datetime.utcnow() - timedelta(hours=5)
    token = auth.create_access_token({"sub": "u2", "exp": stale})
    payload = auth.verify_token(token)              # would 401 if the stale exp survived
    assert payload["sub"] == "u2"
    assert payload["exp"] > datetime.utcnow().timestamp(), "caller's stale exp was not overridden"
    print("PASS create_access_token overrides a caller-supplied exp with its own")


def test_verify_token_rejects_an_expired_token():
    expired = jwt.encode(
        {"sub": "x", "exp": datetime.utcnow() - timedelta(minutes=1)},
        auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    exc = _expect_http(lambda: auth.verify_token(expired), 401, "expired token")
    assert "expired" in exc.detail.lower() or "invalid" in exc.detail.lower()
    print("PASS verify_token rejects an expired token with 401")


def test_verify_token_rejects_a_forged_token():
    # Signed with a key other than this deployment's SECRET_KEY — a token minted
    # by anyone who does not hold it. The signature check must fail.
    forged = jwt.encode(
        {"sub": "x", "exp": datetime.utcnow() + timedelta(hours=1)},
        _FORGED_KEY, algorithm=auth.ALGORITHM)
    _expect_http(lambda: auth.verify_token(forged), 401, "forged token")
    # And a structurally-broken string is a 401 too, never an uncaught 500.
    _expect_http(lambda: auth.verify_token("not.a.jwt"), 401, "garbage token")
    print("PASS verify_token rejects a forged/garbage token with 401, not a 500")


def test_decode_token_optional_fails_safe_to_none():
    # The fail-safe decoder: None for anything it cannot trust, a payload only
    # for a valid token. It must NEVER raise (it backs tenant scoping, where a
    # raise is a 500) and NEVER return a payload it did not verify.
    expired = jwt.encode(
        {"sub": "x", "exp": datetime.utcnow() - timedelta(minutes=1)},
        auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    forged = jwt.encode(
        {"sub": "x", "exp": datetime.utcnow() + timedelta(hours=1)},
        _FORGED_KEY, algorithm=auth.ALGORITHM)
    for bad, label in [("", "empty string"), (None, "None"), ("not.a.jwt", "garbage"),
                       (expired, "expired"), (forged, "forged")]:
        assert auth.decode_token_optional(bad) is None, f"decode_token_optional({label}) was not None"
    good = auth.create_access_token({"sub": "u3", "role": "Operator"})
    payload = auth.decode_token_optional(good)
    assert payload is not None and payload["sub"] == "u3", "a valid token did not decode"
    print("PASS decode_token_optional returns None for missing/garbage/expired/forged, payload only for valid")


def test_get_current_user_turns_an_oem_principal_away():
    # ADR-0017: an OEM session has no business on a factory route. Defence in
    # depth beside the ORM tenant scoping — a 403 that says what happened, not
    # a silently empty factory.
    oem = auth.create_access_token({"sub": "o", "role": "Admin", "principal": "oem"})
    _expect_http(lambda: auth.get_current_user(credentials=_bearer(oem)), 403, "OEM principal")
    # A factory principal passes and gets its claims back.
    factory = auth.create_access_token({"sub": "f", "role": "Admin", "principal": "factory"})
    claims = auth.get_current_user(credentials=_bearer(factory))
    assert claims["sub"] == "f" and claims["role"] == "Admin"
    # A token with no principal at all is a factory user (the common case) and
    # is admitted — the guard rejects only an explicit "oem".
    plain = auth.create_access_token({"sub": "p", "role": "Supervisor"})
    assert auth.get_current_user(credentials=_bearer(plain))["sub"] == "p"
    print("PASS get_current_user rejects an OEM principal (403) and admits a factory/plain user")


def test_require_roles_enforces_the_allow_list():
    checker = auth.require_roles(["Admin", "Supervisor"])
    # An allowed role is returned unchanged.
    assert checker(current_user={"role": "Supervisor"})["role"] == "Supervisor"
    assert checker(current_user={"role": "Admin"})["role"] == "Admin"
    # A role outside the list, and a claim set with NO role, are both 403.
    _expect_http(lambda: checker(current_user={"role": "Operator"}), 403, "disallowed role")
    _expect_http(lambda: checker(current_user={}), 403, "missing role")
    print("PASS require_roles admits an allowed role and 403s a disallowed / missing one")


if __name__ == "__main__":
    test_the_signing_key_is_usable()
    test_create_and_verify_roundtrip_preserves_claims()
    test_create_access_token_overrides_a_caller_supplied_exp()
    test_verify_token_rejects_an_expired_token()
    test_verify_token_rejects_a_forged_token()
    test_decode_token_optional_fails_safe_to_none()
    test_get_current_user_turns_an_oem_principal_away()
    test_require_roles_enforces_the_allow_list()
    print("ALL AUTH TOKEN CONTRACT TESTS PASSED")
