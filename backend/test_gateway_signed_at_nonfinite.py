"""A non-finite `signed_at` is a refusal, not a crash.

Both signature verifiers document the same guarantee: "Never raises — a
malformed packet is a refusal, not a crash." A `signed_at` of NaN or +-inf broke
it. Those ARE floats, so `float(...)` accepts them; and JSON permits them —
Python's `json.loads` decodes `Infinity`, `NaN` and a plain `1e400` to inf — and
the MQTT ingest reads raw json BEFORE AMP has authenticated the gateway. So an
unauthenticated publisher could put one in a packet. inf then reached
`int(-age)` in the "signed in the future" branch and raised OverflowError;
the listener's catch-all swallowed it as an INTERNAL error, so the packet was
never recorded as the gateway refusal it is and never reached the operator's
notification feed (`_record_gateway_refusal`). NaN made both age comparisons
false and fell through to a misleading "signature does not match".

This pins the fix on BOTH halves of the duplicated algorithm — AMP's
`gateway_auth.verify_signature` and the gateway's `ampedge.signing.verify` —
and that they agree, the same contract `test_gateway_signature_parity.py` keeps
for every other input shape.

Run: python backend/test_gateway_signed_at_nonfinite.py
"""
import math
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
KEY = "a-gateway-key-issued-by-amp"


def both(payload, now):
    """(amp_result, edge_result), each either (ok, reason) or the raised exception."""
    results = []
    for fn in (gateway_auth.verify_signature, signing.verify):
        try:
            results.append(fn(payload, KEY, now=now))
        except Exception as exc:  # noqa: BLE001 - the whole point is that this never happens
            results.append(exc)
    return results[0], results[1]


# ── 0. a correctly-signed packet still verifies (no regression) ──────
section("0. THE FIX DOES NOT BREAK A VALID SIGNATURE")
good = signing.sign({"machine": "CNC-01", "tenant": "ACME", "site": "plant-1"},
                    "gw-acme-1", KEY, now=NOW)
# sign() stamps a finite signed_at, so verification an instant later succeeds.
check("a finite signed_at is finite", math.isfinite(float(good["signed_at"])))
amp, edge = both(good, NOW + 1)
check("AMP accepts the valid packet", amp == (True, ""), str(amp))
check("the gateway verifier agrees", edge == (True, ""), str(edge))


# ── 1. every non-finite signed_at is a clean refusal on BOTH sides ───
section("1. A NON-FINITE signed_at NEVER RAISES — IT IS REFUSED")

# Each value is something `json.loads` can actually hand the verifier:
#   1e400         -> float('inf')   (a plain JSON number that overflows)
#   Infinity/NaN  -> float          (Python's json extension)
#   "inf"/"nan"   -> str            (float("inf")/float("nan") succeed)
NON_FINITE = [
    ("+inf as 1e400", float("inf")),
    ("-inf", float("-inf")),
    ("nan", float("nan")),
    ('string "inf"', "inf"),
    ('string "-inf"', "-inf"),
    ('string "nan"', "nan"),
    ('string "Infinity"', "Infinity"),
]

for label, value in NON_FINITE:
    payload = {"scheme": gateway_auth.SCHEME, "signature": "deadbeef",
               "signed_at": value, "gateway_id": "gw-acme-1"}
    amp, edge = both(payload, NOW)
    check(f"AMP does not raise on {label}", not isinstance(amp, Exception),
          type(amp).__name__ if isinstance(amp, Exception) else "")
    check(f"the gateway verifier does not raise on {label}", not isinstance(edge, Exception),
          type(edge).__name__ if isinstance(edge, Exception) else "")
    # A refusal: (False, <reason>). Not accepted — the signature is bogus anyway.
    if not isinstance(amp, Exception):
        check(f"AMP refuses {label}", amp[0] is False, str(amp))
    if not isinstance(edge, Exception):
        check(f"the gateway verifier refuses {label}", edge[0] is False, str(edge))
    # And the two halves agree, byte for byte, as the parity contract requires.
    check(f"AMP and the gateway agree on {label}", amp == edge, f"amp={amp} edge={edge}")


# ── 2. the "future" branch specifically — where int(inf) crashed ─────
section("2. THE OVERFLOW WAS IN THE 'SIGNED IN THE FUTURE' BRANCH")
# A finite far-future signed_at is refused with the real future-branch message,
# so we know that branch is exercised and still works for finite values...
finite_future = {"scheme": gateway_auth.SCHEME, "signature": "deadbeef",
                 "signed_at": NOW + 10_000, "gateway_id": "gw-acme-1"}
amp, edge = both(finite_future, NOW)
check("a finite future time is refused, not crashed", amp[0] is False and "future" in amp[1], str(amp))
check("...and the gateway verifier says the same", amp == edge, f"amp={amp} edge={edge}")
# ...while +inf would have hit `int(-age)` == int(inf) in that exact branch.
inf_future = dict(finite_future, signed_at=float("inf"))
amp, edge = both(inf_future, NOW)
check("+inf in the future branch is refused, not OverflowError",
      (not isinstance(amp, Exception)) and amp[0] is False, repr(amp))


# ── 3. the refusal reaches authorise() as GatewayRejected, not a crash ─
section("3. THE HANDLER PATH TURNS IT INTO A CLEAN GATEWAY REJECTION")


class Route:
    def __init__(self, tenant, site):
        self.tenant, self.site = tenant, site


class Credential:
    def __init__(self, gateway_id, tenant_code, site, secret, is_active=True):
        self.gateway_id, self.tenant_code, self.site = gateway_id, tenant_code, site
        self.secret, self.is_active = secret, is_active


cred = Credential("gw-acme-1", "ACME", "plant-1", KEY)
route = Route("ACME", "plant-1")
payload = {"scheme": gateway_auth.SCHEME, "signature": "deadbeef",
           "signed_at": float("inf"), "gateway_id": "gw-acme-1"}
raised = None
try:
    gateway_auth.authorise(route, payload, cred, now=NOW)
except gateway_auth.GatewayRejected as rej:
    raised = rej
except Exception as exc:  # noqa: BLE001
    raised = exc
check("authorise() raises GatewayRejected (a refusal), not OverflowError",
      isinstance(raised, gateway_auth.GatewayRejected),
      type(raised).__name__ if raised else "nothing raised")
check("the rejection names signed_at, so the operator can act on it",
      raised is not None and "signed_at" in str(raised), str(raised))


print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    if __name__ == "__main__":
        sys.exit(1)
else:
    print("ALL CHECKS PASSED")


def test_everything():
    """Whatever the checks above recorded."""
    assert not failures, "\n  " + "\n  ".join(str(f) for f in failures)


if __name__ == "__main__":
    pass
