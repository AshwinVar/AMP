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
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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


def _varint(data, i):
    """MQTT's remaining-length encoding. Returns (value, next_index)."""
    multiplier, value = 1, 0
    while True:
        byte = data[i]
        i += 1
        value += (byte & 127) * multiplier
        if not byte & 128:
            return value, i
        multiplier *= 128
        if multiplier > 128 ** 3:
            raise ValueError("malformed remaining length")


class TinyBroker(threading.Thread):
    """Enough MQTT 3.1.1 to hold a real client honestly."""

    CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
    SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 12, 13, 14

    def __init__(self, port, ack=True, refuse=False):
        super().__init__(daemon=True)
        self.port = port
        self.ack = ack                 # withhold PUBACK when False
        self.refuse = refuse           # answer CONNACK with "bad credentials"
        self.published = []            # (topic, payload)
        self.credentials = []          # (username, password) as SEEN on the wire
        self.running = True
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(4)
        self._srv.settimeout(0.5)

    def run(self):
        while self.running:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(5)
        buf = b""
        try:
            while self.running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while True:
                    consumed = self._one_packet(conn, buf)
                    if consumed == 0:
                        break
                    buf = buf[consumed:]
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _one_packet(self, conn, buf):
        if len(buf) < 2:
            return 0
        kind = buf[0] >> 4
        flags = buf[0] & 0x0F
        try:
            length, header_end = _varint(buf, 1)
        except (IndexError, ValueError):
            return 0
        total = header_end + length
        if len(buf) < total:
            return 0
        body = buf[header_end:total]

        if kind == self.CONNECT:
            self._read_connect(body)
            # 0 = accepted, 5 = not authorised
            conn.sendall(bytes([self.CONNACK << 4, 2, 0, 5 if self.refuse else 0]))
        elif kind == self.PUBLISH:
            topic_len = struct.unpack("!H", body[:2])[0]
            topic = body[2:2 + topic_len].decode("utf-8", "replace")
            rest = body[2 + topic_len:]
            qos = (flags >> 1) & 3
            packet_id = None
            if qos > 0:
                packet_id = struct.unpack("!H", rest[:2])[0]
                rest = rest[2:]
            self.published.append((topic, rest.decode("utf-8", "replace")))
            if qos > 0 and self.ack:
                conn.sendall(bytes([self.PUBACK << 4, 2]) + struct.pack("!H", packet_id))
        elif kind == self.SUBSCRIBE:
            packet_id = struct.unpack("!H", body[:2])[0]
            conn.sendall(bytes([self.SUBACK << 4, 3]) + struct.pack("!H", packet_id) + b"\x00")
        elif kind == self.PINGREQ:
            conn.sendall(bytes([self.PINGRESP << 4, 0]))
        elif kind == self.DISCONNECT:
            return total
        return total

    def _read_connect(self, body):
        i = 0
        name_len = struct.unpack("!H", body[i:i + 2])[0]
        i += 2 + name_len
        i += 1                                   # protocol level
        connect_flags = body[i]
        i += 1
        i += 2                                   # keepalive
        client_len = struct.unpack("!H", body[i:i + 2])[0]
        i += 2 + client_len
        if connect_flags & 0x04:                 # will
            for _ in range(2):
                field_len = struct.unpack("!H", body[i:i + 2])[0]
                i += 2 + field_len
        username = password = None
        if connect_flags & 0x80:
            field_len = struct.unpack("!H", body[i:i + 2])[0]
            username = body[i + 2:i + 2 + field_len].decode("utf-8", "replace")
            i += 2 + field_len
        if connect_flags & 0x40:
            field_len = struct.unpack("!H", body[i:i + 2])[0]
            password = body[i + 2:i + 2 + field_len].decode("utf-8", "replace")
        self.credentials.append((username, password))

    def stop(self):
        self.running = False
        try:
            self._srv.close()
        except OSError:
            pass


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
