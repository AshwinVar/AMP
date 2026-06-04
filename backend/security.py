import hashlib


def hash_password(password: str):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def verify_password(
    plain_password: str,
    hashed_password: str
):
    return hashlib.sha256(
        plain_password.encode("utf-8")
    ).hexdigest() == hashed_password