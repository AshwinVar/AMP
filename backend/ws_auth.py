"""Authentication for the live WebSocket. Decides BEFORE the socket is accepted.

WHAT WAS MEASURED BEFORE THIS EXISTED
-------------------------------------
One real connection per row, driven at the ASGI layer against master:

    no token at all            ACCEPTED  bound_tenant=None
    empty token                ACCEPTED  bound_tenant=None
    garbage token              ACCEPTED  bound_tenant=None
    wrong-secret token         ACCEPTED  bound_tenant=None
    EXPIRED token              ACCEPTED  bound_tenant=None
    DELETED user's token       ACCEPTED  bound_tenant='FACTORY_A'
    no tenant claim            ACCEPTED  bound_tenant=None

Every one was accepted. `tenant_from_token` returns None for anything it cannot
decode, and the handler passed that straight to `manager.connect`, so the token
decided which broadcasts a client received — never whether it could connect at
all. Authentication failed OPEN.

The deleted user is the one that cost something: their token still decoded, so
the socket bound to FACTORY_A and kept streaming that factory's machine
telemetry until the token expired on its own. ADR-0015 made a revoked account
unable to approve a purchase order; without this it could still watch the plant.

CLOSE CODES
-----------
Refusals use the application range (4000-4999) so they cannot be confused with a
protocol-level close, and they are split by what the CLIENT should do about it:

    4401 NEEDS_AUTH       the credential is missing, malformed or expired.
                          Reconnecting with a NEW token will work.
    4403 REFUSED          the account is gone, disabled, or the token claims a
                          tenant the account does not belong to. Reconnecting
                          will never work; the client must stop.
    4400 NO_CLIENT_FRAMES the client sent a frame. The server has no inbound
                          protocol.

The split matters because the browser client retries on every close. Without a
code that means "stop", a revoked account would reconnect every 30 seconds
forever.

WHY A DATABASE LOOKUP HERE
--------------------------
Same reasoning as approvals.py, and the same trade: a SELECT on every HTTP
request would cost 282 routes to defend one. A WebSocket connect happens once
per session and then streams for hours, so it is the cheapest possible place to
ask "is this person still allowed" — and the most valuable, because the alternative
is a live feed that cannot be revoked.

The tenant is taken from the DATABASE ROW, not from the token's claim. The claim
must still match, or the connection is refused: a token asserting a tenant its
account does not belong to is not a request to serve, it is a request to refuse.
"""
from jose import JWTError, ExpiredSignatureError, jwt

import auth
import logging_config
import models

log = logging_config.get_logger(__name__)

# Refusal codes. See the module docstring for what a client should do with each.
NEEDS_AUTH = 4401
REFUSED = 4403
NO_CLIENT_FRAMES = 4400


class WsDenied(Exception):
    """Refused before accept. Carries the close code and a reason the client
    can act on — never a generic failure, because a client that cannot tell
    'your token expired' from 'your account is disabled' cannot do the right
    thing about either."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _claims(token):
    if not token:
        raise WsDenied(NEEDS_AUTH, "A token is required")
    try:
        return jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    except ExpiredSignatureError:
        # Explicit, and deliberately distinct from "invalid": this is the one
        # refusal a client can fix by itself, by getting a new token.
        raise WsDenied(NEEDS_AUTH, "Token expired")
    except JWTError:
        raise WsDenied(NEEDS_AUTH, "Invalid token")


def resolve(db, token):
    """The tenant this connection may receive, or raise WsDenied.

    Never returns None. A connection with no tenant is one the broadcast filter
    cannot reason about, which is exactly how an unauthenticated socket came to
    match any payload whose tenant_code was absent.
    """
    claims = _claims(token)

    username = claims.get("sub") or claims.get("username")
    if not username:
        raise WsDenied(REFUSED, "Token names no user")

    claimed = claims.get("tenant")
    if not claimed:
        raise WsDenied(REFUSED, "Token names no workspace")

    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise WsDenied(REFUSED, "This account no longer exists")
    if not user.is_active:
        raise WsDenied(REFUSED, "This account has been disabled")

    tenant = user.tenant_code or ""
    if tenant != claimed:
        # The account is real but the token asserts a workspace it does not
        # belong to. Refuse rather than quietly serving the account's own
        # tenant: a mismatch is either a forged claim or a stale one, and
        # neither should silently succeed.
        raise WsDenied(REFUSED, "Token does not match this account")

    # No "if not tenant" guard here. `claimed` is already known non-empty, and
    # this line is only reached when tenant == claimed, so an empty tenant is
    # unreachable. A mutation of that guard survived precisely because it could
    # never fire; keeping it would look like a check and be a comment.
    return tenant
