"""Password hashing.

Uses bcrypt directly (passlib 1.7.4 is incompatible with modern bcrypt).
Existing accounts created with the old SHA-256 scheme still verify (legacy
fallback) and are transparently upgraded to bcrypt on their next successful
login (see /login -> needs_rehash).
"""
import hashlib

import bcrypt


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes; truncate to avoid a ValueError on long inputs.
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def _is_legacy_sha256(hashed: str) -> bool:
    return bool(hashed) and len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed.lower())


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not hashed_password:
        return False
    if _is_legacy_sha256(hashed_password):
        return hashlib.sha256(plain_password.encode("utf-8")).hexdigest() == hashed_password
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8")[:72], hashed_password.encode("utf-8"))
    except Exception:
        return False


def needs_rehash(hashed_password: str) -> bool:
    """True if the stored hash should be re-hashed with bcrypt (i.e. it's a legacy SHA-256 hash)."""
    return _is_legacy_sha256(hashed_password)
