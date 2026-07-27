"""Unit tests for security.py — password hashing + the legacy-hash upgrade path.

security.py is the sole authority on how a password becomes a stored hash and how
a login verifies against it. It had NO dedicated test, yet a slip here is a
security-critical failure in either direction: a broken verify locks every user
out, and an over-eager one accepts wrong passwords. The riskiest corner is the
transparent SHA-256 -> bcrypt migration (/login calls needs_rehash then re-hashes
on success): a wrong legacy detection would either re-hash a bcrypt hash forever
or leave old SHA-256 accounts unverifiable. These lock the behaviour down.

The legacy fixtures below are INDEPENDENTLY-DERIVED: the 64-char hex literal is a
real SHA-256 digest computed outside this file (`sha256("secret123")`), pinned as
a constant rather than recomputed inline — so the test would catch verify_password
silently changing which algorithm it applies to a legacy hash.

Run:  python backend/test_security.py     (exit 0 = pass)
"""
import hashlib

from security import (
    hash_password,
    verify_password,
    needs_rehash,
    _is_legacy_sha256,
)

# sha256("secret123").hexdigest(), derived independently and pinned as a literal.
LEGACY_SHA256_SECRET123 = "fcf730b6d95236ecd3c9fc2d92d7b6b2bb061514961aec041d6c7a7192f592e4"


def test_bcrypt_roundtrip():
    h = hash_password("s3cr3t!")
    # A bcrypt hash: 60 chars, "$2b$" prefix — NOT the plaintext, NOT reversible.
    assert h != "s3cr3t!" and len(h) == 60 and h.startswith("$2b$"), h
    assert verify_password("s3cr3t!", h) is True
    assert verify_password("wrong", h) is False
    print("PASS bcrypt hash roundtrips (right password verifies, wrong rejected)")


def test_bcrypt_salt_is_random_but_both_verify():
    # A per-hash random salt means the same password hashes to two DIFFERENT
    # strings, and BOTH must still verify — the property that makes a stolen hash
    # table useless (identical passwords don't collide).
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b, "salt should make two hashes of the same password differ"
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True
    print("PASS random salt: same password -> different hashes, both verify")


def test_legacy_sha256_is_verified_against_the_pinned_digest():
    # The migration's read side: an account still on the old SHA-256 scheme must
    # verify without a re-hash. Pinned literal = sha256("secret123").
    assert LEGACY_SHA256_SECRET123 == hashlib.sha256(b"secret123").hexdigest()  # sanity on the fixture
    assert verify_password("secret123", LEGACY_SHA256_SECRET123) is True
    assert verify_password("wrong", LEGACY_SHA256_SECRET123) is False
    print("PASS legacy SHA-256 hashes still verify (independently-derived digest)")


def test_needs_rehash_only_for_legacy():
    # The migration's trigger: legacy hashes are flagged for upgrade, bcrypt ones
    # are not (else /login would pointlessly re-hash a good bcrypt hash every time).
    assert needs_rehash(LEGACY_SHA256_SECRET123) is True
    assert needs_rehash(hash_password("anything")) is False
    print("PASS needs_rehash is True for SHA-256, False for bcrypt")


def test_sha256_to_bcrypt_upgrade_flow():
    # The full transparent upgrade as /login performs it: a legacy hash verifies
    # and is flagged, the code re-hashes the plaintext to bcrypt, and afterwards
    # the account verifies on bcrypt and is no longer flagged. The password never
    # changes — only its storage scheme.
    legacy = LEGACY_SHA256_SECRET123
    assert verify_password("secret123", legacy) is True and needs_rehash(legacy) is True

    upgraded = hash_password("secret123")            # what /login stores on success
    assert upgraded.startswith("$2b$")
    assert verify_password("secret123", upgraded) is True
    assert needs_rehash(upgraded) is False
    assert _is_legacy_sha256(upgraded) is False      # no longer mistaken for legacy
    print("PASS SHA-256 account upgrades to bcrypt, then verifies as bcrypt")


def test_legacy_detection_boundaries():
    # _is_legacy_sha256 is the single gate for the whole migration, so its edges
    # matter: exactly 64 lowercase-hex chars is legacy; anything else is not.
    assert _is_legacy_sha256(LEGACY_SHA256_SECRET123) is True
    assert _is_legacy_sha256("a" * 63) is False            # 63 chars — one short
    assert _is_legacy_sha256("a" * 65) is False            # 65 chars — one long
    assert _is_legacy_sha256("g" * 64) is False            # right length, non-hex
    assert _is_legacy_sha256("$2b$" + "x" * 56) is False   # a bcrypt-shaped string
    assert _is_legacy_sha256("") is False
    assert _is_legacy_sha256(None) is False
    print("PASS legacy detection: only a 64-char hex string counts as SHA-256")


def test_bcrypt_72_byte_truncation_is_consistent():
    # bcrypt only consumes the first 72 bytes; security.py truncates on BOTH hash
    # and verify so the two stay consistent (an inconsistent pair would make a long
    # password unverifiable). Two passwords sharing their first 72 bytes verify
    # interchangeably; one that differs within the first 72 bytes does not.
    h = hash_password("a" * 72 + "TAIL-ONE")
    assert verify_password("a" * 72 + "TAIL-TWO", h) is True     # differ only past byte 72
    assert verify_password("b" * 72 + "TAIL-ONE", h) is False    # differ inside the first 72
    print("PASS 72-byte truncation applied consistently on hash and verify")


def test_missing_or_malformed_hash_returns_false_not_error():
    # A user row with no/blank password, or a corrupt stored value, must fail
    # verification cleanly (return False) rather than raise and 500 the login.
    assert verify_password("anything", "") is False
    assert verify_password("anything", None) is False
    assert verify_password("anything", "$2b$this-is-not-a-valid-bcrypt-hash") is False
    print("PASS empty / None / malformed stored hash -> False, never raises")


if __name__ == "__main__":
    test_bcrypt_roundtrip()
    test_bcrypt_salt_is_random_but_both_verify()
    test_legacy_sha256_is_verified_against_the_pinned_digest()
    test_needs_rehash_only_for_legacy()
    test_sha256_to_bcrypt_upgrade_flow()
    test_legacy_detection_boundaries()
    test_bcrypt_72_byte_truncation_is_consistent()
    test_missing_or_malformed_hash_returns_false_not_error()
    print("ALL SECURITY TESTS PASSED")
