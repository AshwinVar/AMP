"""How a gateway proves which workspace it is publishing for.

THE ATTACK THIS EXISTS TO STOP is one line long: change the topic. AMP reads
tenant and site out of `{prefix}/{tenant}/{site}/machines`, so a gateway that
can publish at all can publish as ANY customer simply by editing a string in its
own config file. Broker credentials do not help — every pilot gateway holds
valid broker credentials by definition. The topic is an assertion the publisher
makes about itself, and an assertion is not authentication.

So each gateway is issued an id and a key by AMP, and signs what it sends. The
key never travels; the signature does. AMP looks the id up, finds the tenant and
site it was issued for, and REJECTS the packet if the topic says something else.
Editing the topic now breaks the signature, and editing the id makes the
signature fail against the other tenant's key.

WHY HMAC AND NOT A BEARER TOKEN. A token in the payload is replayable by anyone
who sees one packet — and MQTT packets cross a plant network in the clear unless
TLS is configured, which on a pilot it may not be. An HMAC over the payload plus
a timestamp means a captured packet cannot be edited, and the timestamp plus
`record_id` means it cannot usefully be replayed either.

THE ALGORITHM IS WRITTEN TWICE ON PURPOSE — here and in the AMP backend — so
that the gateway package does not import AMP and AMP does not import the
gateway. `backend/test_gateway_signature_parity.py` pins the two to the byte;
if they ever disagree, that test fails rather than a pilot's data silently
failing to authenticate at 3am.
"""
import hashlib
import hmac
import json
import time
import uuid

#: Bumped only if the canonical form changes. AMP refuses a version it does not
#: know rather than trying to verify a shape it cannot reproduce.
SCHEME = "amp-edge-hmac-sha256-v1"

#: A signature older than this is refused. Long enough that a buffered record
#: re-signed at publish time is fine; short enough that a captured packet is not
#: useful tomorrow. Note this is the SIGNING time, never the reading's time —
#: a three-hour-old reading is published with a fresh signature and keeps its
#: own `ts`, so honesty about when it happened survives.
MAX_AGE_S = 300.0


def canonical(payload: dict) -> bytes:
    """The exact bytes both sides sign. Sorted keys, no spaces, UTF-8.

    `signature` is excluded (it cannot cover itself) and nothing else is. That
    means the machine name, the site, every sample and every timestamp are all
    covered: an attacker who flips `running` to false in flight breaks it.
    """
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sign(payload: dict, gateway_id: str, key: str, now=None) -> dict:
    """Return a copy of `payload` carrying its signature envelope."""
    now = time.time() if now is None else now
    out = dict(payload)
    out["gateway_id"] = str(gateway_id)
    out["scheme"] = SCHEME
    out["signed_at"] = round(float(now), 3)
    # A nonce so two identical readings a second apart are not byte-identical,
    # and so `record_id` is not the only thing standing between a replay and a
    # duplicate production count.
    out.setdefault("record_id", uuid.uuid4().hex)
    out["nonce"] = uuid.uuid4().hex
    out["signature"] = hmac.new(
        _key_bytes(key), canonical(out), hashlib.sha256).hexdigest()
    return out


def verify(payload: dict, key: str, now=None, max_age=MAX_AGE_S):
    """(ok, reason). Never raises — a malformed packet is a refusal, not a crash.

    The reason is for the gateway's own diagnostics and for AMP's log. It never
    contains the key, and never contains the expected signature: telling an
    attacker how close they were is an oracle.
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
    expected = hmac.new(_key_bytes(key), canonical(payload), hashlib.sha256).hexdigest()
    # compare_digest, not ==, so the comparison does not leak the prefix length
    # through timing.
    if not hmac.compare_digest(expected, claimed):
        return False, "signature does not match"
    return True, ""


def _key_bytes(key) -> bytes:
    if isinstance(key, bytes):
        return key
    return str(key).encode("utf-8")
