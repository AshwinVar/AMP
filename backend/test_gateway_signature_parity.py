"""The gateway and AMP must compute the SAME signature, byte for byte.

ADR-0040 says the gateway imports nothing from AMP and AMP imports nothing from
the gateway. The cost of that boundary is this: the signing algorithm exists
twice, in `edge/ampedge/signing.py` and in `backend/gateway_auth.py`.

Duplicated code drifts. The failure mode is specific and horrible: a change to
the canonical form on one side, every pilot gateway in the field suddenly
failing authentication, and an error message that says only "signature does not
match" — which is indistinguishable from an attack, at 3am, on a customer site.

So the two are pinned here, to the byte, on payloads shaped like the ones a real
gateway sends. This test is the reason the duplication is acceptable.

Run: python backend/test_gateway_signature_parity.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "edge"))

import gateway_auth                        # noqa: E402  (AMP's half)
from ampedge import signing                # noqa: E402  (the gateway's half)

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


KEY = "a-gateway-key-issued-by-amp"
NOW = 1_800_000_000.0

# Shaped like what the gateway actually publishes, including the awkward parts:
# a float, a bool, a nested dict, a unicode machine name, and a list.
PAYLOADS = [
    {"machine": "CNC-01", "tenant": "ACME", "site": "plant-1", "status": "Running"},
    {"machine": "CNC-01", "tenant": "ACME", "site": "", "status": "Idle",
     "total_count": 7, "good_count": 6, "rejected_count": 1,
     "planned_minutes": 30, "runtime_minutes": 28, "ideal_cycle_time_seconds": 45},
    {"machine": "PRESS-02", "tenant": "FACTORY_A", "site": "-", "ts": 1800000000.125,
     "readings": {"temperature": 48.25, "pressure": 7.4, "operating_hours": 4281.6},
     "reading_units": {"temperature": "degC"}},
    {"machine": "Präzision-01", "tenant": "ACME", "site": "werk-2", "status": "Down",
     "fault_code": "E-207", "gateway_notes": ["counter_reset", "a note with, a comma"]},
    {"machine": "M", "tenant": "T", "site": "s", "buffered": True, "buffered_for": 7203.4,
     "gap_before": True},
]

# ── 1. the canonical bytes ──────────────────────────────────────────
section("1. THE BYTES BOTH SIDES SIGN ARE IDENTICAL")
for i, body in enumerate(PAYLOADS):
    a = signing.canonical(body)
    b = gateway_auth.canonical(body)
    check(f"payload #{i + 1} canonicalises identically", a == b,
          f"{a[:90]!r} != {b[:90]!r}")

# `signature` is excluded on both sides, and nothing else is.
with_sig = dict(PAYLOADS[0], signature="deadbeef")
check("the signature field is excluded by both",
      signing.canonical(with_sig) == gateway_auth.canonical(with_sig)
      == signing.canonical(PAYLOADS[0]),
      "one side included the signature in its own input")

# Key order must not matter — dicts arriving from JSON have arbitrary order.
shuffled = {k: v for k, v in reversed(list(PAYLOADS[1].items()))}
check("key order does not change the bytes",
      gateway_auth.canonical(shuffled) == gateway_auth.canonical(PAYLOADS[1]),
      "canonicalisation is order-dependent")

# ── 2. the scheme and the window ────────────────────────────────────
section("2. THE SCHEME AND THE REPLAY WINDOW AGREE")
check("both sides name the same scheme", signing.SCHEME == gateway_auth.SCHEME,
      f"{signing.SCHEME} vs {gateway_auth.SCHEME}")
check("both sides use the same window", signing.MAX_AGE_S == gateway_auth.MAX_AGE_S,
      f"{signing.MAX_AGE_S} vs {gateway_auth.MAX_AGE_S}")

# ── 3. gateway signs, AMP verifies ──────────────────────────────────
section("3. WHAT THE GATEWAY SIGNS, AMP ACCEPTS")
for i, body in enumerate(PAYLOADS):
    signed = signing.sign(body, "gw-acme-1", KEY, now=NOW)
    ok, why = gateway_auth.verify_signature(signed, KEY, now=NOW + 1)
    check(f"AMP verifies payload #{i + 1} as signed by the gateway", ok, why)
    # And the gateway's own verifier agrees, so the two are genuinely the same
    # function rather than merely both self-consistent.
    ok2, why2 = signing.verify(signed, KEY, now=NOW + 1)
    check(f"...and the gateway's verifier agrees about #{i + 1}", ok2, why2)

# ── 4. and rejects the same things ──────────────────────────────────
section("4. BOTH SIDES REFUSE THE SAME PACKETS")
signed = signing.sign(PAYLOADS[1], "gw-acme-1", KEY, now=NOW)

cases = [
    ("a tampered tenant", dict(signed, tenant="VICTIM")),
    ("a tampered count", dict(signed, total_count=70000)),
    ("a tampered machine", dict(signed, machine="OTHER-01")),
    ("a stripped signature", {k: v for k, v in signed.items() if k != "signature"}),
    ("an unknown scheme", dict(signed, scheme="something-else")),
]
for label, bad in cases:
    a_ok, _ = signing.verify(bad, KEY, now=NOW + 1)
    b_ok, _ = gateway_auth.verify_signature(bad, KEY, now=NOW + 1)
    check(f"{label} is refused by both", (not a_ok) and (not b_ok), f"edge={a_ok} amp={b_ok}")

for label, when in (("an expired signature", NOW + 4000), ("one from the future", NOW - 4000)):
    a_ok, _ = signing.verify(signed, KEY, now=when)
    b_ok, _ = gateway_auth.verify_signature(signed, KEY, now=when)
    check(f"{label} is refused by both", (not a_ok) and (not b_ok), f"edge={a_ok} amp={b_ok}")

check("another workspace's key is refused by both",
      not signing.verify(signed, "other-key", now=NOW + 1)[0]
      and not gateway_auth.verify_signature(signed, "other-key", now=NOW + 1)[0])

# ── 5. the id is validated before it becomes a lookup ───────────────
section("5. A CLAIMED GATEWAY ID IS VALIDATED BEFORE IT REACHES THE DATABASE")
check("a normal id is accepted",
      gateway_auth.claimed_gateway_id({"gateway_id": "gw-acme-plant1-01"}) == "gw-acme-plant1-01")
for bad in ("", None, 17, "x" * 65, "-leading-dash", "has space", "semi;colon",
            "' OR 1=1 --", "../../etc/passwd"):
    check(f"{bad!r} is refused as a gateway id",
          gateway_auth.claimed_gateway_id({"gateway_id": bad}) is None,
          str(gateway_auth.claimed_gateway_id({"gateway_id": bad})))
check("a payload with no gateway_id at all is None, not an error",
      gateway_auth.claimed_gateway_id({}) is None)

# ── 6. issued secrets ───────────────────────────────────────────────
section("6. AN ISSUED KEY IS FROM THE OS, AND NEVER REPEATS")
keys = {gateway_auth.issue_secret() for _ in range(50)}
check("50 issued keys are 50 distinct keys", len(keys) == 50, str(len(keys)))
check("...each 256 bits of hex", all(len(k) == 64 for k in keys),
      str(sorted({len(k) for k in keys})))
check("...and hex only", all(all(c in "0123456789abcdef" for c in k) for k in keys))

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
