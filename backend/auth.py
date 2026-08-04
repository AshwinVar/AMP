import os
import secrets
from datetime import datetime, timedelta

from dotenv import load_dotenv
from jose import jwt, JWTError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()


def _resolve_secret_key() -> str:
    """FAIL CLOSED on the JWT signing key (PR3 of the security tier).

    The old fallback was a constant published in this repository — anyone who
    read the source could mint a valid Admin token for any deployment that
    forgot to set SECRET_KEY. Now:

      * SECRET_KEY set        -> used verbatim (production path; Railway env).
      * unset, in production  -> refuse to boot. A crash with a clear message
        is strictly better than an API silently accepting forged tokens.
        (Production is detected by RAILWAY_ENVIRONMENT, which Railway sets on
        every deploy, or an explicit PRODUCTION=1.)
      * unset, in dev/CI      -> a random EPHEMERAL key for this process only.
        Local sessions reset on restart — harmless in dev, and never a key an
        attacker can look up.
    """
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if key:
        return key
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PRODUCTION"):
        raise RuntimeError(
            "SECRET_KEY is not set. Refusing to start in production with a "
            "known JWT signing key — set SECRET_KEY in the environment "
            "(Railway: Variables tab) and redeploy."
        )
    print("[AUTH] SECRET_KEY not set — using an ephemeral dev key (sessions reset on restart)")
    return secrets.token_urlsafe(64)


SECRET_KEY = _resolve_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 240

security = HTTPBearer()


def create_access_token(data: dict):
    payload = data.copy()
    payload.update(
        {
            "exp": datetime.utcnow()
            + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        }
    )

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def verify_token(token: str):
    try:
        return jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )


def decode_token_optional(token: str):
    """Decode a token WITHOUT raising — returns the payload, or None if the token
    is missing or invalid. For non-critical uses such as tenant scoping."""
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    return verify_token(credentials.credentials)


def require_roles(allowed_roles: list[str]):
    def role_checker(
        current_user: dict = Depends(get_current_user)
    ):
        role = current_user.get("role")

        if role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action"
            )

        return current_user

    return role_checker
