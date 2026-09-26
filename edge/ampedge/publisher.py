"""The one outbound connection. The gateway dials AMP; AMP never dials the gateway.

WHY THIS DIRECTION AND NO OTHER. A cloud service that connects INTO a factory
network needs an inbound hole in the plant firewall, a static address, and an IT
department willing to grant both — which is a six-week conversation before the
pilot can start, and a permanent liability afterwards. An outbound MQTT
connection over 8883 is the same shape as every other thing on that network
already reaching the internet, and it can be revoked by unplugging one PC.

So there is no listener in this package. Nothing in the factory accepts a
connection from AMP.

DELIVERY IS AT-LEAST-ONCE, DELIBERATELY. QoS 1, and a record is removed from
the local queue only after the broker has acknowledged it. The cost is that AMP
can see a record twice after an awkward reconnection; the alternative cost is
losing a shift's parts to a flaky link, and `record_id` makes the first one
recoverable while nothing makes the second one recoverable.

CREDENTIALS NEVER REACH A LOG. Not in an error, not in a repr, not in the
diagnostic export. `describe()` is the only thing that prints, and it prints a
host and a port.
"""
import os
import threading
import time

try:                                    # pragma: no cover - import guard
    import paho.mqtt.client as mqtt
    AVAILABLE = True
except ImportError:                     # pragma: no cover
    mqtt = None
    AVAILABLE = False

from . import signing
from .adapters import base

DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883
DEFAULT_KEEPALIVE = 30
PUBLISH_TIMEOUT_S = 10.0


class Publisher:
    """Settings: host, port, username, password, tls, topic_prefix, tenant, site,
    gateway_id, gateway_key, client_id."""

    def __init__(self, settings: dict):
        if not AVAILABLE:
            raise base.AdapterError(
                "the `paho-mqtt` package is not installed, so this gateway cannot publish. "
                "Install the edge requirements (pip install -r edge/requirements.txt).")
        self.settings = dict(settings or {})
        self.host = str(self.settings.get("host") or "")
        if not self.host:
            raise base.AdapterError("the AMP connection needs a `host`")
        self.tls = bool(self.settings.get("tls", True))
        self.port = int(self.settings.get("port") or (DEFAULT_TLS_PORT if self.tls else DEFAULT_PORT))
        self.prefix = str(self.settings.get("topic_prefix") or "flowmes")
        self.tenant = str(self.settings.get("tenant") or "")
        self.site = str(self.settings.get("site") or "")
        self.gateway_id = str(self.settings.get("gateway_id") or "")
        self._key = self.settings.get("gateway_key") or ""
        self.state = base.DISCONNECTED
        self.last_error = ""
        self.last_publish_at = None
        self._refused = False
        self.published = 0
        self.refused = 0
        self._client = None
        self._connected = threading.Event()

    # ── the topic ───────────────────────────────────────────────────
    def topic(self) -> str:
        """`{prefix}/{tenant}/{site}/machines` — the contract AMP already reads.

        Note what this does NOT do: it does not let a caller pass a topic. The
        only tenant this gateway can address is the one in its own config, and
        the signature binds even that to the credential AMP issued.
        """
        return f"{self.prefix}/{self.tenant}/{self.site}/machines"

    # ── lifecycle ───────────────────────────────────────────────────
    def connect(self, timeout=15.0):
        self.state = base.CONNECTING
        client = mqtt.Client(client_id=self.settings.get("client_id") or f"amp-edge-{self.gateway_id}",
                             clean_session=False,   # so QoS1 survives a short drop
                             # THE GATEWAY OWNS RECONNECTION, not paho. The drain
                             # loop already backs off with jitter and reports the
                             # state; leaving paho's own retry on as well means two
                             # uncoordinated loops, and its growing internal sleep
                             # makes loop_stop() block for up to two minutes on a
                             # broker that is refusing us.
                             reconnect_on_failure=False)
        username = self.settings.get("username")
        if username:
            client.username_pw_set(str(username), self.settings.get("password") or None)
        if self.tls:
            # `tls_set()` with no arguments trusts the SYSTEM CA store, which is
            # the right answer for a broker holding a publicly-issued
            # certificate and useless for AMP's, which is signed by a CA we run
            # ourselves (infra/mosquitto). `ca_cert` points at that CA's
            # certificate, shipped with the gateway.
            #
            # Note what is NOT offered: there is no `tls_insecure` setting. The
            # one thing a commissioning engineer reaches for when a certificate
            # does not verify is the switch that stops it verifying, and a
            # gateway that skips verification is a gateway whose credentials can
            # be collected by anything that can get in the path. If the name
            # does not match, fix the name.
            ca_cert = self.settings.get("ca_cert")
            if ca_cert:
                ca_cert = str(ca_cert)
                if not os.path.isfile(ca_cert):
                    raise base.AdapterError(
                        f"amp.ca_cert points at {ca_cert}, which does not exist. "
                        f"That file is the CA certificate this gateway uses to "
                        f"recognise AMP's broker; without it every connection is "
                        f"refused. Copy it from the broker's startup log.")
                client.tls_set(ca_certs=ca_cert)
            else:
                client.tls_set()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._connected.clear()
        # Reset per attempt: a gateway that was refused once and is retrying
        # after the operator fixed the credentials must not read its own stale
        # refusal as the answer to the new attempt.
        self._refused = False
        try:
            client.connect(self.host, self.port, keepalive=DEFAULT_KEEPALIVE)
        except Exception as e:                       # noqa: BLE001 - named, not swallowed
            self.state = base.ERROR
            # Type name only: a paho error string can echo the connect
            # parameters, and those include a password.
            self.last_error = f"could not reach {self.host}:{self.port} ({type(e).__name__})"
            raise base.AdapterError(self.last_error)
        client.loop_start()
        if self._connected.wait(timeout=timeout) and self._refused:
            client.loop_stop()
            self.state = base.ERROR
            raise base.AdapterError(self.last_error)
        if not self._connected.is_set():
            # disconnect() first so the network thread leaves its loop; otherwise
            # loop_stop() waits on a thread that is still mid-handshake.
            try:
                client.disconnect()
            except Exception:                        # noqa: BLE001 - already failing
                pass
            client.loop_stop()
            self.state = base.ERROR
            self.last_error = (f"{self.host}:{self.port} accepted the socket but never completed "
                               f"the MQTT handshake (check the credentials and the TLS setting)")
            raise base.AdapterError(self.last_error)
        self._client = client
        self.state = base.CONNECTED
        self.last_error = ""
        return self

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc == 0:
            self._connected.set()
            self.state = base.CONNECTED
        else:
            # paho's rc 4/5 are bad credentials. Saying which is not a leak —
            # the operator needs it, and an attacker already knows.
            self.state = base.ERROR
            # Set the event so connect() stops waiting: the answer has arrived,
            # it is just a refusal. Waiting out the full timeout on a broker that
            # already said no delays every diagnostic by that timeout.
            self._refused = True
            self._connected.set()
            self.last_error = {
                1: "the broker refused the protocol version",
                2: "the broker rejected the client id",
                3: "the broker is unavailable",
                4: "the broker rejected the username or password",
                5: "the broker refused this client (not authorised)",
            }.get(rc, f"the broker refused the connection (code {rc})")

    def _on_disconnect(self, client, userdata, rc, *args):
        # NOT an error on its own: a clean disconnect is rc 0. Either way the
        # runner keeps queueing to disk, which is the whole point of the buffer.
        self.state = base.DISCONNECTED
        self._connected.clear()

    def disconnect(self):
        if self._client is not None:
            try:
                # DISCONNECT first, THEN stop the loop. The other order stops the
                # network thread before it can send the packet, and then joins a
                # thread that is still trying to deliver an unacknowledged QoS 1
                # publish — which hangs for as long as the broker stays silent.
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:                        # noqa: BLE001 - going away anyway
                pass
        self._client = None
        self.state = base.DISCONNECTED

    # ── publishing ──────────────────────────────────────────────────
    def publish(self, payload: dict, now=None) -> bool:
        """Sign, send, and WAIT for the broker's acknowledgement.

        Returns True only when the broker has it. The caller removes the record
        from the local queue on True and on nothing else — a publish that timed
        out may or may not have arrived, and re-sending a record AMP already has
        is recoverable where dropping one is not.
        """
        if self._client is None or self.state != base.CONNECTED:
            return False
        body = dict(payload)
        # The tenant and site are asserted here, once, from configuration — not
        # from anything the PLC said and not from anything a caller passed.
        body["tenant"] = self.tenant
        body["site"] = self.site
        if self.gateway_id and self._key:
            body = signing.sign(body, self.gateway_id, self._key, now=now)
        try:
            import json
            info = self._client.publish(self.topic(), json.dumps(body, default=str), qos=1)
        except Exception as e:                       # noqa: BLE001
            self.refused += 1
            self.last_error = f"publish failed ({type(e).__name__})"
            return False
        try:
            info.wait_for_publish(timeout=PUBLISH_TIMEOUT_S)
        except (ValueError, RuntimeError):
            self.refused += 1
            return False
        if not info.is_published():
            self.refused += 1
            self.last_error = f"the broker did not acknowledge within {PUBLISH_TIMEOUT_S:.0f}s"
            return False
        self.published += 1
        self.last_publish_at = time.time()
        return True

    # ── health ──────────────────────────────────────────────────────
    def describe(self) -> dict:
        return {
            "state": self.state,
            "broker": f"{self.host}:{self.port}",
            "tls": self.tls,
            # A PATH, never the file. "tls: true" alone does not distinguish a
            # gateway verifying AMP's CA from one trusting the system store and
            # about to be refused, and that is the first question when a
            # connection will not come up.
            "ca_cert": str(self.settings.get("ca_cert") or "") or None,
            "topic": self.topic(),
            "signed": bool(self.gateway_id and self._key),
            "gateway_id": self.gateway_id or None,
            "published": self.published,
            "refused": self.refused,
            "last_publish_at": self.last_publish_at,
            "last_error": self.last_error,
        }
