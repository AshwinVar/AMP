"""Can a gateway publish into another customer's factory? This answers no.

THE ATTACK IS ONE LINE OF CONFIG. AMP takes tenant and site from the MQTT topic.
Every pilot gateway holds valid broker credentials by definition. So unless
something binds a publisher to a workspace, a customer publishes as any other
customer by editing a string in a file they own — on any broker whose ACLs are
wrong, absent, or simply `#`.

`gateway_auth.authorise` is that binding, and this pins it. Tenant isolation is
on the sprint's test-this-immediately list and this is the newest way into it.

Note what is NOT tested here: the database lookup. That is deliberate —
`authorise` takes a credential the caller found, so the rule that decides
whether a packet is accepted can be driven with every hostile shape without a
database in the way.

Run: python backend/test_gateway_authorisation.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "edge"))

import gateway_auth                   # noqa: E402
from ampedge import signing           # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


NOW = 1_800_000_000.0
ACME_KEY = "acme-key-0123456789abcdef"
VICTIM_KEY = "victim-key-fedcba9876543210"


class Route:
    __slots__ = ("tenant", "site")

    def __init__(self, tenant, site):
        self.tenant = tenant
        self.site = site


class Credential:
    def __init__(self, gateway_id, tenant_code, site, secret, is_active=True):
        self.gateway_id = gateway_id
        self.tenant_code = tenant_code
        self.site = site
        self.secret = secret
        self.is_active = is_active


ACME_GW = Credential("gw-acme-1", "ACME", "plant-1", ACME_KEY)
VICTIM_GW = Credential("gw-victim-1", "VICTIM", "plant-9", VICTIM_KEY)


def signed_for(tenant, site, key=ACME_KEY, gateway_id="gw-acme-1", now=NOW):
    return signing.sign({"machine": "CNC-01", "tenant": tenant, "site": site,
                         "status": "Running", "total_count": 7, "good_count": 7,
                         "rejected_count": 0},
                        gateway_id, key, now=now)


def refuse(route, payload, credential, now=NOW + 1):
    try:
        gateway_auth.authorise(route, payload, credential, now=now)
        return None
    except gateway_auth.GatewayRejected as e:
        return str(e)


# ── 1. the honest case ──────────────────────────────────────────────
section("1. A GATEWAY PUBLISHING FOR ITS OWN WORKSPACE IS ACCEPTED")
route = Route("ACME", "plant-1")
payload = signed_for("ACME", "plant-1")
check("an ACME gateway on ACME's topic is accepted",
      refuse(route, payload, ACME_GW) is None, str(refuse(route, payload, ACME_GW)))

# ── 2. THE ATTACK ───────────────────────────────────────────────────
section("2. A VALID GATEWAY CANNOT SPEAK FOR ANOTHER WORKSPACE")
# The attacker owns gw-acme-1 and its key. They edit the topic to VICTIM's, and
# re-sign — which they CAN do, because it is their own key. The signature is
# perfectly valid. The credential is the thing that refuses.
stolen_topic = Route("VICTIM", "plant-9")
resigned = signed_for("VICTIM", "plant-9", key=ACME_KEY, gateway_id="gw-acme-1")
why = refuse(stolen_topic, resigned, ACME_GW)
check("a correctly-signed packet aimed at another tenant is REFUSED", why is not None,
      "it was accepted")
check("...and the reason names the gateway, not the victim's workspace",
      why and "gw-acme-1" in why, str(why))
check("...and does NOT disclose which workspace the credential belongs to",
      why and "ACME" not in why.replace("gw-acme-1", ""), str(why))

# The signature alone genuinely does verify — proving the credential lookup is
# what stops this, not the crypto.
ok, _ = gateway_auth.verify_signature(resigned, ACME_KEY, now=NOW + 1)
check("...even though the signature itself verifies, which is the point", ok,
      "the signature did not verify, so this test proves nothing")

# ── 3. the same, one site over ──────────────────────────────────────
section("3. A GATEWAY CANNOT SPEAK FOR ANOTHER SITE OF ITS OWN WORKSPACE")
other_site = Route("ACME", "plant-2")
why = refuse(other_site, signed_for("ACME", "plant-2"), ACME_GW)
check("a plant-1 gateway publishing as plant-2 is refused", why is not None, "accepted")
check("...naming the site the topic claimed", why and "plant-2" in why, str(why))

# The no-site case, which a single-plant pilot uses.
siteless = Credential("gw-single-1", "SOLO", "", ACME_KEY)
check("a no-site gateway on a no-site topic is accepted",
      refuse(Route("SOLO", ""), signed_for("SOLO", "", gateway_id="gw-single-1"),
             siteless) is None)
check("...but not on a sited topic",
      refuse(Route("SOLO", "plant-1"),
             signed_for("SOLO", "plant-1", gateway_id="gw-single-1"), siteless) is not None)

# ── 4. a key that is not this gateway's ─────────────────────────────
section("4. A PACKET SIGNED WITH THE WRONG KEY IS NOT THIS GATEWAY")
forged = signed_for("ACME", "plant-1", key="a-key-the-attacker-made-up")
why = refuse(route, forged, ACME_GW)
check("a forged signature is refused", why is not None, "accepted")
check("...as a signature failure", why and "signature does not match" in why, str(why))
check("...without echoing the expected signature",
      why and forged["signature"][:16] not in why, "the refusal leaked the expected value")

# IDENTITY BEFORE AUTHORITY: a packet signed with the wrong key AND aimed at
# another tenant must fail on the signature, not on the tenant — otherwise the
# error tells an attacker which workspace a gateway id belongs to before they
# have proved they hold its key.
why = refuse(stolen_topic, signed_for("VICTIM", "plant-9", key="wrong"), ACME_GW)
check("identity is checked before authority", why and "signature" in why, str(why))
check("...so an unauthenticated caller learns nothing about the workspace",
      why and "workspace" not in why, str(why))

# ── 5. revocation ───────────────────────────────────────────────────
section("5. A DEACTIVATED GATEWAY STOPS WORKING IMMEDIATELY")
revoked = Credential("gw-acme-1", "ACME", "plant-1", ACME_KEY, is_active=False)
why = refuse(route, signed_for("ACME", "plant-1"), revoked)
check("a deactivated credential is refused even with a perfect signature",
      why is not None, "accepted")
check("...saying it was deactivated, not that the signature failed",
      why and "deactivated" in why, str(why))

# ── 6. an id AMP has never issued ───────────────────────────────────
section("6. AN UNREGISTERED GATEWAY IS REFUSED, NOT GUESSED")
why = refuse(route, signed_for("ACME", "plant-1", gateway_id="gw-not-real"), None)
check("an unknown gateway id is refused", why is not None, "accepted")
check("...naming it so the operator can register it", why and "gw-not-real" in why, str(why))
long_id = {"gateway_id": "x" * 500, "scheme": gateway_auth.SCHEME}
check("a 500-character id never becomes a lookup",
      gateway_auth.claimed_gateway_id(long_id) is None)

# ── 7. replay ───────────────────────────────────────────────────────
section("7. A CAPTURED PACKET IS NOT A LICENCE")
old = signed_for("ACME", "plant-1", now=NOW - 4000)
why = refuse(route, old, ACME_GW)
check("an hour-old capture is refused", why is not None, "accepted")
check("...naming the window", why and "window" in why, str(why))
check("...and a fresh packet still works",
      refuse(route, signed_for("ACME", "plant-1", now=NOW), ACME_GW) is None)

# ── 8. nothing leaks ────────────────────────────────────────────────
section("8. NO REFUSAL EVER CONTAINS A KEY")
probes = [
    refuse(route, forged, ACME_GW),
    refuse(stolen_topic, resigned, ACME_GW),
    refuse(route, signed_for("ACME", "plant-1"), revoked),
    refuse(route, old, ACME_GW),
    refuse(route, {"gateway_id": "gw-acme-1"}, ACME_GW),
    refuse(route, {}, ACME_GW),
    refuse(route, None, ACME_GW),
]
for i, message in enumerate(probes):
    text = str(message)
    check(f"refusal #{i + 1} contains no secret",
          ACME_KEY not in text and VICTIM_KEY not in text, text[:120])
check("a malformed payload is a refusal, never a crash",
      all(m is not None for m in probes[-3:]), str(probes[-3:]))

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
