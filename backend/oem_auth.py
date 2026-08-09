"""OEM authentication and authorization (ADR-0017).

WHY THIS IS A SEPARATE MODULE FROM auth.py
------------------------------------------
`auth.require_roles` is a flat `role not in allowed` test against a JWT claim,
with no database access. That is the right trade for factory routes: the check
runs on all 282 of them, and the claims were minted from the database minutes
ago.

An OEM principal is different in two ways that matter. It spans several customer
factories, so a stale or forged claim is a cross-COMPANY breach rather than a
cross-page one; and OEM logins are rare and long-lived, so a `SELECT` per request
is affordable where it would not be on the factory side. This module therefore
re-reads the principal from the database on every OEM request — the same shape as
`approvals._check_actor` and `ws_auth.resolve`, and for the same reason.

THE SENTINEL
------------
The ADR-0002 hook filters every scoped model to the bound tenant, and is a NO-OP
when nothing is bound. Measured against the real hook, two machines in two
tenants:

    bound 'FACTORY_A'          -> 1 machine
    bound OEM sentinel         -> 0 machines
    bound None                 -> 2 machines   <- every tenant

So an OEM request must never run unbound. `sentinel_tenant()` produces a tenant
string no factory can hold, and the middleware binds it for every OEM principal.
Factory operational tables then return nothing for an OEM by CONSTRUCTION —
before any OEM route is written, and whether or not its author remembers to
filter. Everything else in the OEM layer is defence in depth behind this.
"""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import models
from auth import verify_token

# OEM roles are a DIFFERENT VOCABULARY from the factory roles ("Admin",
# "Supervisor", "Operator"). A token carrying "Admin" must never satisfy an OEM
# check, and an OEM token must never satisfy require_roles(["Admin"]).
OEM_ADMIN = "OEM_ADMIN"
OEM_SERVICE_MANAGER = "OEM_SERVICE_MANAGER"
OEM_SERVICE_ENGINEER = "OEM_SERVICE_ENGINEER"
OEM_VIEWER = "OEM_VIEWER"

OEM_ROLES = (OEM_ADMIN, OEM_SERVICE_MANAGER, OEM_SERVICE_ENGINEER, OEM_VIEWER)

# Every OEM role may read the fleet; only some may change it.
ROLE_CAPABILITIES = {
    OEM_ADMIN: {"read_fleet", "manage_models", "manage_installations",
                "manage_users", "manage_branding", "manage_service", "commission"},
    OEM_SERVICE_MANAGER: {"read_fleet", "manage_service", "manage_installations"},
    OEM_SERVICE_ENGINEER: {"read_fleet", "manage_service", "commission"},
    OEM_VIEWER: {"read_fleet"},
}

# The prefix that makes an OEM's bound tenant unmatchable by any factory row.
# A tenant code is a plain identifier (see mqtt_identity._identifier), so a colon
# cannot occur in one — this string can never collide with a real tenant.
SENTINEL_PREFIX = "OEM:"

_bearer = HTTPBearer(auto_error=False)


class OemDenied(HTTPException):
    """Refused by the OEM gate, with the precise reason. Never a generic 403:
    an operator who cannot tell 'your account is disabled' from 'not your
    machine' cannot act on either."""

    def __init__(self, status_code, detail):
        super().__init__(status_code=status_code, detail=detail)


def sentinel_tenant(oem_code):
    """The factory tenant an OEM request binds: one no factory can hold.

    NOT None. Binding None disables the ADR-0002 filter for the whole request,
    which is how an OEM would come to read every tenant's rows.
    """
    return f"{SENTINEL_PREFIX}{oem_code}"


def is_sentinel(tenant):
    return bool(tenant) and str(tenant).startswith(SENTINEL_PREFIX)


def oem_of_claims(claims):
    """The OEM code a token claims, or None if it is not an OEM token."""
    if not claims:
        return None
    if claims.get("principal") != "oem":
        return None
    code = claims.get("oem")
    return code if isinstance(code, str) and code else None


def resolve(db, claims):
    """The OEM principal behind a token, re-read from the database.

    Raises OemDenied. Never returns a principal for a token that merely SAYS it
    is an OEM: the organisation must exist and be active, the user must exist,
    be active, and still belong to that organisation, and the role must still be
    an OEM role. A demotion or a suspension therefore takes effect on the next
    request rather than at token expiry.
    """
    oem_code = oem_of_claims(claims)
    if not oem_code:
        raise OemDenied(401, "Not an OEM session")

    username = claims.get("sub")
    if not isinstance(username, str) or not username:
        raise OemDenied(401, "Token names no user")

    user = (db.query(models.OemUser)
              .filter(models.OemUser.username == username).first())
    if user is None:
        raise OemDenied(401, "This account no longer exists")
    if not user.is_active:
        raise OemDenied(403, "This account has been disabled")
    if (user.oem_code or "") != oem_code:
        # The account is real but the token asserts an organisation it does not
        # belong to. Refuse rather than quietly serving the account's own OEM:
        # a mismatch is either a forged claim or a stale one.
        raise OemDenied(403, "Token does not match this account")
    if user.role not in OEM_ROLES:
        raise OemDenied(403, "Not an OEM role")

    org = (db.query(models.OemOrganization)
             .filter(models.OemOrganization.oem_code == oem_code).first())
    if org is None:
        raise OemDenied(403, "This organisation no longer exists")
    if not org.is_active:
        raise OemDenied(403, "This organisation has been suspended")

    return {"oem": oem_code, "username": username, "role": user.role,
            "org": org, "user": user}


def _principal(db, credentials):
    if credentials is None or not credentials.credentials:
        raise OemDenied(401, "Not authenticated")
    try:
        claims = verify_token(credentials.credentials)
    except HTTPException:
        raise OemDenied(401, "Invalid or expired token")
    return resolve(db, claims)


def require_oem(*capabilities):
    """FastAPI dependency: an authenticated OEM principal with every capability.

    Capabilities rather than role names at the call site, so a route says what it
    needs ("manage_service") instead of enumerating which roles happen to have
    it — adding a role later cannot silently widen an existing route.
    """
    def dependency(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
        from database import SessionLocal
        db = SessionLocal()
        try:
            principal = _principal(db, credentials)
            allowed = ROLE_CAPABILITIES.get(principal["role"], set())
            missing = [c for c in capabilities if c not in allowed]
            if missing:
                raise OemDenied(
                    403, "You do not have permission to perform this action")
            # Detach what routes need; the Session closes with this dependency.
            return {"oem": principal["oem"], "username": principal["username"],
                    "role": principal["role"]}
        finally:
            db.close()

    return dependency
