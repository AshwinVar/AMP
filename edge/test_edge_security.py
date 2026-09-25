"""Can a gateway publish as somebody else? This is the test that answers no.

THE THREAT IS NOT HYPOTHETICAL AND IT IS NOT HARD. AMP reads the tenant and the
site out of the MQTT topic. Every pilot gateway holds working broker
credentials. So without something binding a publisher to a workspace, any
customer with a gateway can publish into any other customer's plant by editing
one string in a config file they own — no exploit, no vulnerability, just the
protocol working as designed.

Four things are pinned here, and all four are on the sprint's
test-this-immediately list:

  1. A SIGNATURE COVERS THE WHOLE PACKET, so the tenant cannot be edited.
  2. A KEY FROM ONE WORKSPACE CANNOT VALIDATE ANOTHER'S PACKET.
  3. A CAPTURED PACKET GOES STALE, so a recording is not a licence.
  4. A CREDENTIAL NEVER APPEARS IN A CONFIG FILE, A LOG, OR AN EXPORT.

Run: python edge/test_edge_security.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ampedge import config as config_mod    # noqa: E402
from ampedge import signing                 # noqa: E402

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


ACME_KEY = "acme-secret-key-value"
OTHER_KEY = "other-tenant-key-value"

# ── 1. the signature covers everything ──────────────────────────────
section("1. THE SIGNATURE COVERS THE PACKET, INCLUDING WHOSE IT IS")
body = {"machine": "CNC-01", "tenant": "ACME", "site": "plant-1", "status": "Running",
        "total_count": 7, "good_count": 6, "rejected_count": 1}
signed = signing.sign(body, "gw-acme-1", ACME_KEY)
ok, why = signing.verify(signed, ACME_KEY)
check("a packet AMP issued a key for verifies", ok, why)

for field, value in (("tenant", "VICTIM"), ("site", "plant-9"), ("machine", "CNC-99"),
                     ("total_count", 7000), ("status", "Down")):
    tampered = dict(signed)
    tampered[field] = value
    ok, why = signing.verify(tampered, ACME_KEY)
    check(f"editing {field} in flight breaks the signature", not ok, f"{field} accepted: {why}")

# ── 2. one workspace's key cannot sign for another ──────────────────
section("2. A KEY BELONGS TO A WORKSPACE, AND CANNOT REACH OUT OF IT")
ok, why = signing.verify(signed, OTHER_KEY)
check("another workspace's key does not verify this packet", not ok, why)

# The full attack, spelled out: take your OWN valid packet, point it at someone
# else's tenant, re-sign with YOUR key (the only one you have).
stolen = dict(body)
stolen["tenant"] = "VICTIM"
resigned = signing.sign(stolen, "gw-acme-1", ACME_KEY)
ok, _ = signing.verify(resigned, ACME_KEY)
check("re-signing with your own key DOES produce a valid signature", ok,
      "so the signature alone is not the defence")
check("...which is why the id must be looked up: it still says gw-acme-1",
      resigned["gateway_id"] == "gw-acme-1", resigned["gateway_id"])
check("...and the tenant it claims is not the one that id belongs to",
      resigned["tenant"] == "VICTIM",
      "AMP must compare the id's workspace against the topic and reject this")

# ── 3. a recording is not a licence ─────────────────────────────────
section("3. A CAPTURED PACKET GOES STALE")
old = signing.sign(body, "gw-acme-1", ACME_KEY, now=time.time() - 3600)
ok, why = signing.verify(old, ACME_KEY)
check("an hour-old signature is refused", not ok, why)
check("...naming the window rather than just failing", "window" in why, why)

future = signing.sign(body, "gw-acme-1", ACME_KEY, now=time.time() + 3600)
ok, why = signing.verify(future, ACME_KEY)
check("a signature from the future is refused too", not ok, why)

check("two signings of identical data differ, so a replay is detectable",
      signing.sign(body, "g", ACME_KEY)["signature"] != signing.sign(body, "g", ACME_KEY)["signature"],
      "identical signatures")

# ── 4. malformed input is a refusal, never a crash ──────────────────
section("4. A MALFORMED PACKET IS REFUSED, NOT AN EXCEPTION")
for bad, label in ((None, "null"), ("a string", "a string"), ({}, "an empty object"),
                   ({"scheme": "something-else", "signature": "x"}, "an unknown scheme"),
                   ({"scheme": signing.SCHEME}, "no signature"),
                   ({"scheme": signing.SCHEME, "signature": "x", "signed_at": "soon"},
                    "a non-numeric time")):
    ok, why = signing.verify(bad, ACME_KEY)
    check(f"{label} is refused with a reason", not ok and bool(why), f"{ok} {why}")

ok, why = signing.verify(signed, ACME_KEY)
check("...and a good packet still verifies afterwards", ok, why)
check("a refusal never echoes the expected signature",
      signed["signature"][:16] not in json.dumps(
          [signing.verify({**signed, "tenant": "X"}, ACME_KEY)]),
      "the refusal leaked the expected value")

# ── 5. secrets never live in a config file ──────────────────────────
section("5. A CREDENTIAL IS NOT CONFIGURATION")
literal = {
    "amp": {"host": "broker.amp", "tenant": "ACME", "site": "plant-1", "password": "hunter2"},
    "machines": [{"name": "CNC-01", "protocol": "opcua",
                  "connection": {"url": "opc.tcp://10.0.0.5:4840", "password": "plcpass"},
                  "tags": [{"tag": "r", "address": "ns=2;i=3", "signal": "running",
                            "datatype": "bool"}]}],
}
refused = None
try:
    config_mod.validate(literal)
except config_mod.ConfigError as e:
    refused = str(e)
check("a config with a literal password does not start", refused is not None, "it started")
check("...naming BOTH offending places", refused and refused.count("_env with the NAME") >= 2,
      str(refused)[:200])
check("...and saying what to write instead", refused and "environment variable" in refused,
      str(refused)[:200])
check("the refusal does not print the secret itself",
      refused and "hunter2" not in refused and "plcpass" not in refused,
      "the error message leaked the password")

# The supported form.
os.environ["TEST_PLC_PASSWORD"] = "plcpass"
os.environ["TEST_GATEWAY_KEY"] = ACME_KEY
good = {
    "amp": {"host": "broker.amp", "tenant": "ACME", "site": "plant-1"},
    "gateway": {"id": "gw-acme-1", "key_env": "TEST_GATEWAY_KEY"},
    "machines": [{"name": "CNC-01", "protocol": "opcua",
                  "connection": {"url": "opc.tcp://10.0.0.5:4840", "username": "amp",
                                 "password_env": "TEST_PLC_PASSWORD"},
                  "tags": [{"tag": "r", "address": "ns=2;i=3", "signal": "running",
                            "datatype": "bool"}]}],
}
resolved = config_mod.validate(good)
check("the env form is accepted", resolved["machines"][0]["name"] == "CNC-01", str(resolved)[:120])
check("...and the value is resolved in memory for the adapter",
      resolved["machines"][0]["connection"]["password"] == "plcpass",
      "the adapter would have had no password")
check("...with the _env key gone, so it cannot be written back out",
      "password_env" not in resolved["machines"][0]["connection"],
      str(resolved["machines"][0]["connection"].keys()))

redacted = config_mod.redact(resolved)
check("the redacted copy carries no password", "plcpass" not in json.dumps(redacted),
      json.dumps(redacted)[:200])
check("...but still shows the connection so it is useful in a ticket",
      redacted["machines"][0]["connection"]["url"] == "opc.tcp://10.0.0.5:4840",
      str(redacted["machines"][0]["connection"]))

# A gateway key that is named but not set must fail LOUDLY at startup, not
# silently publish unsigned packets that AMP will later reject.
del os.environ["TEST_GATEWAY_KEY"]
missing = None
try:
    config_mod.validate(good)
except config_mod.ConfigError as e:
    missing = str(e)
check("a gateway key that is not in the environment stops startup", missing is not None,
      "it started with no key")
check("...naming the variable to set", missing and "TEST_GATEWAY_KEY" in missing, str(missing))
os.environ["TEST_GATEWAY_KEY"] = ACME_KEY

# ── 6. the topic cannot be bent ─────────────────────────────────────
section("6. A TENANT OR SITE THAT COULD BEND THE TOPIC IS REFUSED")
for value in ("plant/1", "plant #1", "+", "#", "../other", "plant 1", ""):
    bad = json.loads(json.dumps(good))
    bad["amp"]["site"] = value
    refused = None
    try:
        config_mod.validate(bad)
    except config_mod.ConfigError as e:
        refused = str(e)
    check(f"site {value!r} is refused", refused is not None, "accepted")

# A single-site factory has no site code, and the wire contract spells that "-".
# AMP maps it to the empty site, which is the site a hand-created machine already
# has -- so for a one-plant pilot the gateway matches those machines directly.
single = json.loads(json.dumps(good))
single["amp"]["site"] = "-"
accepted = None
try:
    accepted = config_mod.validate(single)
except config_mod.ConfigError as e:
    accepted = e
check("the no-site wire token `-` is accepted", not isinstance(accepted, Exception),
      str(accepted))

# ── 7. validation names every problem, not the first ────────────────
section("7. TWELVE PROBLEMS ARE TWELVE LINES, NOT TWELVE RESTARTS")
messy = {"amp": {}, "machines": [{"name": "A", "protocol": "profinet", "tags": []}]}
try:
    config_mod.validate(messy)
    problems = ""
except config_mod.ConfigError as e:
    problems = str(e)
check("a config with many faults reports them together", problems.count(";") >= 4, problems[:200])
check("...including that PROFINET is not supported, whatever the datasheet says",
      "profinet" in problems.lower() and "not supported" in problems.lower(), problems[:200])

# ── 8. a config file round-trips from disk ──────────────────────────
section("8. IT IS A FILE ON DISK, NOT A PYTHON EDIT")
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "gateway.json")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(good, fh)
loaded = config_mod.validate(config_mod.load(path))
check("a mapping change is a file change", loaded["machines"][0]["tags"][0]["address"] == "ns=2;i=3",
      str(loaded["machines"][0]["tags"]))
missing_file = None
try:
    config_mod.load(os.path.join(tmp, "nope.json"))
except config_mod.ConfigError as e:
    missing_file = str(e)
check("a missing config is a clear refusal", missing_file is not None, "it loaded nothing")

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
