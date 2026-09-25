"""Proving that a gateway is the gateway it claims to be.

THE HOLE THIS CLOSES IS ONE LINE WIDE. AMP reads tenant and site out of
`{prefix}/{tenant}/{site}/machines`, and `mqtt_identity` is right that the topic
is the only part a broker can enforce. But that enforcement is the BROKER's, and
it depends entirely on the broker's ACLs being configured per gateway. Every
pilot gateway holds valid broker credentials by definition; on a broker where
the ACL is wrong, or absent, or simply `#`, a customer publishes as any other
customer by editing a string in a config file they own. No exploit — the
protocol working as designed.

So a gateway signs what it sends, with a key AMP issued for one workspace and
one site, and AMP checks three things that have to agree:

    1. the SIGNATURE verifies under that gateway's key       (it is that gateway)
    2. the gateway's WORKSPACE matches the topic's           (it is theirs)
    3. the gateway's SITE matches the topic's                (it is that plant)

Re-signing a stolen packet with your own key passes (1) and fails (2). That is
the whole design: the signature proves identity, and the LOOKUP proves what that
identity is allowed to say.

WHY THE ALGORITHM IS WRITTEN TWICE. The gateway must not import AMP and AMP must
not import the gateway (ADR-0040), so `edge/ampedge/signing.py` holds the same
function. `test_gateway_signature_parity.py` pins the two to the byte — if they
ever drift, that test fails here rather than a pilot's data failing to
authenticate at 3am with no explanation.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not look anything up. It is
given a secret and asked whether a payload matches; the database lives in the
caller. That keeps the part that must be exactly right testable without a
database, and keeps the tenancy decision where tenancy decisions already are.
"""
import hashlib
import hmac
import json
import time

#: Bumped only if the canonical form changes. A scheme AMP does not know is
#: refused rather than verified under a shape it cannot reproduce.
SCHEME = "amp-edge-hmac-sha256-v1"

#: How old a SIGNATURE may be. Not how old a reading may be: a record buffered
#: through a three-hour outage is re-signed at publish time and keeps its own
#: `ts`, so honesty about when it happened survives the window.
MAX_AGE_S = 300.0


class GatewayRejected(Exception):
    """A packet AMP will not attribute to the workspace its topic names."""


def canonical(payload: dict) -> bytes:
    """The exact bytes both sides sign. Sorted keys, no spaces, UTF-8.

    Only `signature` is excluded — it cannot cover itself. Everything else is
    covered, so editing the tenant, the machine, a count or a timestamp in
    flight breaks it.
    """
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def verify_signature(payload, secret, now=None, max_age=MAX_AGE_S):
    """(ok, reason). Never raises — a malformed packet is a refusal, not a crash.

    The reason reaches AMP's log and the conflict record. It never contains the
    secret, and never the EXPECTED signature: telling a caller how close they
    were is an oracle.
    """
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        return False, "not an object"
    if payload.get("scheme") != SCHEME:
        return False, f"unknown signing scheme {str(payload.get('scheme'))[:40]!r}"
    claimed = payload.get("signature")
    if not isinstance(claimed, str) or not claimed:
        return False, "no signature"
    try:
        signed_at = float(payload.get("signed_at") or 0)
    except (TypeError, ValueError):
        return False, "signed_at is not a time"
    age = now - signed_at
    if age > max_age:
        return False, f"signed {int(age)}s ago, older than the {int(max_age)}s window"
    if age < -max_age:
        return False, f"signed {int(-age)}s in the future"
    expected = hmac.new(_key_bytes(secret), canonical(payload), hashlib.sha256).hexdigest()
    # compare_digest, not ==, so the comparison does not leak the matching
    # prefix length through timing.
    if not hmac.compare_digest(expected, claimed):
        return False, "signature does not match"
    return True, ""


def authorise(route, payload, credential, now=None):
    """Check a verified gateway is allowed to speak for this topic.

    `credential` is any object carrying `tenant_code`, `site`, `is_active` and
    `secret` — the caller looked it up. Raises GatewayRejected with a reason a
    person can act on; returns the credential when everything agrees.

    THE ORDER MATTERS. Identity is checked before authority, so a packet signed
    with a key that is not this gateway's never reaches the tenant comparison —
    otherwise the error message would tell an attacker which workspace a
    gateway id belongs to.
    """
    if credential is None:
        raise GatewayRejected(
            f"gateway {str(payload.get('gateway_id'))[:64]!r} is not registered in AMP")
    if not getattr(credential, "is_active", True):
        raise GatewayRejected(
            f"gateway {credential.gateway_id!r} has been deactivated in AMP")

    ok, why = verify_signature(payload, credential.secret, now=now)
    if not ok:
        raise GatewayRejected(f"gateway {credential.gateway_id!r}: {why}")

    if credential.tenant_code != route.tenant:
        # The attack this exists for: a valid gateway, correctly signing, with
        # someone else's tenant in the topic. Deliberately does NOT name the
        # workspace the credential belongs to.
        raise GatewayRejected(
            f"gateway {credential.gateway_id!r} was issued for a different workspace than the "
            f"topic claims ({route.tenant!r}); the message is refused")
    if (credential.site or "") != (route.site or ""):
        raise GatewayRejected(
            f"gateway {credential.gateway_id!r} was issued for a different site than the topic "
            f"claims ({route.site or 'no site'!r}); the message is refused")
    return credential


def claimed_gateway_id(payload):
    """The id a payload claims, or None. Validated as an identifier, not trusted.

    Bounded and charset-restricted before it becomes a database lookup: an
    unbounded string from an unauthenticated publisher is the wrong thing to
    put in a WHERE clause, whatever the driver promises.
    """
    value = payload.get("gateway_id") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 64:
        return None
    if not value[0].isalnum():
        return None
    if not all(c.isalnum() or c in "_.-" for c in value):
        return None
    return value


def _key_bytes(key) -> bytes:
    if isinstance(key, bytes):
        return key
    return str(key).encode("utf-8")


def issue_secret() -> str:
    """A new gateway key. 256 bits from the OS, hex, shown once.

    `secrets`, not `random`: this is the only thing standing between a customer
    and another customer's plant data.
    """
    import secrets
    return secrets.token_hex(32)
