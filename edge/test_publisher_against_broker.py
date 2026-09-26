"""The MQTT hop, proven against a broker that actually speaks MQTT.

WHY A BROKER IS WRITTEN HERE RATHER THAN MOCKED. The publisher's contract is not
"call paho" — it is "a record leaves the local queue ONLY when a broker has
acknowledged it". That is a statement about PUBACK, and a mock that returns True
proves nothing about it: the exact failure this guards against is a publisher
that deletes a shift's production because a send() returned without error.

So this is a real (small) MQTT 3.1.1 server: it parses CONNECT, answers CONNACK,
parses PUBLISH, and — crucially — can be told to WITHHOLD the PUBACK, which is
what a saturated or half-dead broker does in practice.

It is deliberately NOT a general broker. It handles what this client sends and
refuses to pretend about anything else.

WHAT THIS PINS:
  * a published record reaches the broker on the right topic, signed
  * an unacknowledged publish does NOT remove anything from the local queue
  * a broker that rejects the credentials is reported as that, not as "offline"
  * nothing in the transcript contains a password

Run: python edge/test_publisher_against_broker.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from broker_stub import TinyBroker         # noqa: E402
from ampedge import buffer as buffer_mod   # noqa: E402
from ampedge import publisher as pub_mod   # noqa: E402
from ampedge import signing                # noqa: E402
from ampedge.adapters import base          # noqa: E402

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


KEY = "gateway-key-value-sentinel"
SETTINGS = {"host": "127.0.0.1", "tls": False, "tenant": "ACME", "site": "plant-1",
            "gateway_id": "gw-acme-1", "gateway_key": KEY,
            "username": "amp-gw", "password": "broker-password-sentinel"}


# ── 1. a real publish, acknowledged ─────────────────────────────────
section("1. A RECORD REACHES THE BROKER, ON THE RIGHT TOPIC, SIGNED")
broker = TinyBroker(48841)
broker.start()
time.sleep(0.2)
publisher = pub_mod.Publisher(dict(SETTINGS, port=48841))
publisher.connect(timeout=10)
check("the publisher connected", publisher.state == base.CONNECTED, publisher.state)
check("the topic is built from CONFIG, not from anything a caller passed",
      publisher.topic() == "flowmes/ACME/plant-1/machines", publisher.topic())

ok = publisher.publish({"machine": "CNC-01", "status": "Running", "total_count": 7})
check("the publish was acknowledged", ok is True, str(ok))
time.sleep(0.2)
check("the broker received exactly one message", len(broker.published) == 1,
      str(len(broker.published)))
topic, raw = broker.published[0]
check("...on the workspace's own topic", topic == "flowmes/ACME/plant-1/machines", topic)
body = json.loads(raw)
check("...carrying the tenant and site as the gateway asserts them",
      (body.get("tenant"), body.get("site")) == ("ACME", "plant-1"), str(body)[:120])
verified, why = signing.verify(body, KEY)
check("...and a signature AMP can verify with the key it issued", verified, why)
check("...naming the gateway id so AMP can look up whose it is",
      body.get("gateway_id") == "gw-acme-1", str(body.get("gateway_id")))

# ── 2. the thing a mock cannot prove ────────────────────────────────
section("2. NO PUBACK MEANS THE RECORD STAYS IN THE QUEUE")
publisher.disconnect()
broker.stop()

silent = TinyBroker(48842, ack=False)
silent.start()
time.sleep(0.2)
publisher = pub_mod.Publisher(dict(SETTINGS, port=48842))
publisher.connect(timeout=10)
pub_mod.PUBLISH_TIMEOUT_S = 1.5          # keep the test quick; same code path
ok = publisher.publish({"machine": "CNC-01", "total_count": 7})
check("an unacknowledged publish reports FAILURE, not success", ok is False, str(ok))
check("...and is counted as refused", publisher.refused == 1, str(publisher.refused))
check("...naming the broker as the thing that did not answer",
      "acknowledge" in publisher.last_error, publisher.last_error)

# The consequence that matters: the drain loop only acks on True, so the record
# survives to be sent again. Proven here rather than asserted.
import tempfile                                                        # noqa: E402
buf = buffer_mod.Buffer(os.path.join(tempfile.mkdtemp(), "q.db"))
buf.put({"machine": "CNC-01", "total_count": 7})
batch = buf.peek(10)
sent = [row_id for row_id, _, queued_at, payload in batch
        if publisher.publish(buffer_mod.stamp_for_publish(payload, queued_at))]
buf.ack(sent)
check("so the production is STILL on disk after a failed publish", buf.depth() == 1,
      str(buf.depth()))
buf.close()
publisher.disconnect()
silent.stop()

# ── 3. bad credentials are reported as bad credentials ──────────────
section("3. A REJECTED LOGIN IS NOT 'AMP IS OFFLINE'")
hostile = TinyBroker(48843, refuse=True)
hostile.start()
time.sleep(0.2)
publisher = pub_mod.Publisher(dict(SETTINGS, port=48843))
refused = None
try:
    publisher.connect(timeout=8)
except base.AdapterError as e:
    refused = str(e)
check("the connection failed", refused is not None or publisher.state != base.CONNECTED,
      publisher.state)
check("...and the reason names the credentials, which is what an engineer must fix",
      "not authorised" in (publisher.last_error or "") or "username" in (publisher.last_error or ""),
      publisher.last_error)
check("the broker did receive the username, so the wire format is right",
      hostile.credentials and hostile.credentials[0][0] == "amp-gw",
      str(hostile.credentials))

# ── 4. nothing leaks ────────────────────────────────────────────────
section("4. THE GATEWAY KEY NEVER TRAVELS, AND NOTHING PRINTS A PASSWORD")
described = json.dumps(publisher.describe())
check("describe() carries no gateway key", KEY not in described, described[:200])
check("describe() carries no broker password", "broker-password-sentinel" not in described,
      described[:200])
check("...but does say whether messages are signed at all", '"signed": true' in described.lower(),
      described[:200])
check("the SIGNING key is never on the wire", all(KEY not in raw for _, raw in broker.published),
      "the key was published")
hostile.stop()

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
