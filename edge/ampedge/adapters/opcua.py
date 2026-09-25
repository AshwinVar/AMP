"""OPC UA client. A real one — `asyncua`, a session, a subscription, quality codes.

WHY OPC UA IS FIRST. It is the only protocol in this sprint that can tell you
what it has. A controls engineer's tag list arrives wrong the first three times,
and on OPC UA you can browse the server and see that `Machine.PartCount` is
actually `Machine.Counters.Parts` — on Modbus you can only try register 40001
and read whatever number comes back. Browsing turns commissioning from a
guessing game into a conversation.

WHAT IS REAL HERE, stated plainly because the support matrix will have to repeat
it and must not overclaim:

  * connect / disconnect / reconnect, with a timeout that is actually applied
  * anonymous and username+password sessions
  * browse the address space
  * read a node, with the SERVER'S timestamp and the SERVER'S status code
  * subscribe to data changes, so a fast signal is not missed between polls
  * every failure named, and never a substituted value

WHAT IS NOT. Certificate/`Sign&Encrypt` security policies are wired to the
config but only exercised against a self-signed test server, so the matrix lists
them EXPERIMENTAL and the pilot runs username+password over a private network.
Saying that out loud is the point of the matrix.

THE QUALITY CODE IS NOT DECORATION. OPC UA servers return `Bad_NodeIdUnknown`
for a typo'd tag and `Uncertain_LastUsableValue` for a value whose source has
stopped updating — and `.Value` still holds a number in both cases. Treating
that number as a reading is how a dead sensor's last value becomes a live one
forever. Anything not Good becomes a non-GOOD Reading with no value.
"""
import asyncio
import time
from urllib.parse import urlsplit, urlunsplit

from . import base

try:                                    # pragma: no cover - import guard
    from asyncua import Client, ua
    from asyncua.ua.uaerrors import UaError
    AVAILABLE = True
except ImportError:                     # pragma: no cover
    Client = None
    ua = None
    UaError = Exception
    AVAILABLE = False

# A first connection to an unreachable PLC must fail in seconds, not minutes: a
# commissioning engineer with the wrong IP needs to be told, and the runner
# needs the slot back to try again.
DEFAULT_TIMEOUT = 8.0
DEFAULT_SUBSCRIPTION_MS = 500.0


def _redact(url: str) -> str:
    """opc.tcp://user:secret@plc:4840 -> opc.tcp://plc:4840. Used everywhere."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.hostname is None:
            return url
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<unprintable endpoint>"


class _ChangeCollector:
    """asyncua calls this back on every data change. It only collects."""

    def __init__(self):
        self.latest = {}
        self.changes = 0

    def datachange_notification(self, node, val, data):
        self.changes += 1
        monitored = getattr(getattr(data, "monitored_item", None), "Value", None)
        status = getattr(monitored, "StatusCode", None)
        source = getattr(monitored, "SourceTimestamp", None)
        self.latest[str(node.nodeid)] = (val, status, source)


class OpcUaAdapter(base.Adapter):
    """Settings: url, username, password, timeout, security, subscription_ms."""

    protocol = "opcua"

    def __init__(self, settings: dict):
        super().__init__(settings)
        if not AVAILABLE:
            raise base.AdapterError(
                "the `asyncua` package is not installed, so this gateway cannot speak OPC UA. "
                "Install the edge requirements (pip install -r edge/requirements.txt).")
        self.url = str(self.settings.get("url") or "")
        if not self.url:
            raise base.AdapterError("opcua needs a `url`, e.g. opc.tcp://192.168.1.10:4840")
        self.timeout = float(self.settings.get("timeout") or DEFAULT_TIMEOUT)
        self._client = None
        self._sub = None
        self._collector = None
        self._handles = []
        self.bad_tags = set()

    def endpoint(self) -> str:
        return _redact(self.url)

    # -- lifecycle -----------------------------------------------------
    async def connect(self):
        self.state = base.CONNECTING
        client = Client(url=self.url, timeout=self.timeout)
        user = self.settings.get("username")
        if user:
            client.set_user(str(user))
            password = self.settings.get("password")
            if password:
                # Handed straight to the library. Never logged, never in
                # describe(), never in a diagnostic export.
                client.set_password(str(password))
        policy = self.settings.get("security")
        if policy:
            # e.g. "Basic256Sha256,SignAndEncrypt,cert.der,key.pem" — asyncua's
            # own string form, passed through rather than reinvented.
            await client.set_security_string(str(policy))
        try:
            # asyncua's own timeout covers the socket; this one covers the whole
            # handshake, which can otherwise hang on a server that accepts the
            # connection and then never finishes negotiating.
            await asyncio.wait_for(client.connect(), timeout=self.timeout + 2)
        except asyncio.TimeoutError:
            self.state = base.ERROR
            self.last_error = (f"no answer from {self.endpoint()} within {self.timeout:.0f}s "
                               f"(check the IP, the port and the firewall)")
            raise base.AdapterError(self.last_error)
        except UaError as e:
            self.state = base.ERROR
            self.last_error = f"{self.endpoint()} refused the session: {type(e).__name__}"
            raise base.AdapterError(self.last_error)
        except Exception as e:                       # noqa: BLE001 - reported, not swallowed
            self.state = base.ERROR
            # repr() of an arbitrary exception can carry the URL, and the URL can
            # carry a password. Type name only.
            self.last_error = f"could not reach {self.endpoint()}: {type(e).__name__}"
            raise base.AdapterError(self.last_error)
        self._client = client
        self.state = base.CONNECTED
        self.connected_at = time.time()
        self.last_error = ""
        self.bad_tags = set()
        return self

    async def disconnect(self):
        if self._sub is not None:
            try:
                await self._sub.delete()
            except Exception:                        # noqa: BLE001 - going away anyway
                pass
            self._sub = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:                        # noqa: BLE001
                pass
        self._client = None
        self._collector = None
        self._handles = []
        self.state = base.DISCONNECTED

    # -- reading -------------------------------------------------------
    async def read(self, addresses) -> list:
        """One Reading per node id. A bad node never stops the good ones."""
        if self._client is None or self.state not in (base.CONNECTED, base.DEGRADED):
            raise base.AdapterError("not connected")
        out = []
        for entry in addresses:
            # Either a bare node id or the whole mapping spec. Modbus NEEDS the
            # spec (a register number says nothing about its own type), so the
            # runner passes specs to every adapter and this one takes the
            # address out rather than having two calling conventions.
            address = entry.get("address") if isinstance(entry, dict) else entry
            out.append(await self._read_one(str(address)))
        self.last_read_at = time.time()
        # DEGRADED is connected-but-not-well: the session is fine and some tags
        # are not. A commissioning engineer seeing DEGRADED knows to look at the
        # tag list, not the network.
        self.state = base.DEGRADED if self.bad_tags else base.CONNECTED
        return out

    async def _read_one(self, address: str) -> base.Reading:
        try:
            node = self._client.get_node(address)
        except Exception:                            # noqa: BLE001 - malformed node id
            self.bad_tags.add(address)
            return base.no_data(address, "not a node id this server understands")
        try:
            dv = await asyncio.wait_for(node.read_data_value(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.bad_tags.add(address)
            return base.no_data(address, f"no answer within {self.timeout:.0f}s")
        except UaError as e:
            name = type(e).__name__
            self.bad_tags.add(address)
            return base.no_data(address, f"the server refused the read ({name})")
        except (ConnectionError, OSError) as e:
            # The SESSION is gone, not just this tag. Raising is right: the
            # runner reconnects, where returning no_data would quietly turn a
            # dropped PLC into forty missing tags forever.
            self.state = base.DISCONNECTED
            self.last_error = f"connection lost: {type(e).__name__}"
            raise base.AdapterError(self.last_error)

        status = getattr(dv, "StatusCode", None)
        if status is not None and not status.is_good():
            self.bad_tags.add(address)
            quality = base.BAD
            name = getattr(status, "name", None) or str(status)
            if "Uncertain" in str(name):
                quality = base.UNCERTAIN
            return base.Reading(tag=address, value=None, quality=quality,
                                detail=f"the server marked this value {name}")
        self.bad_tags.discard(address)

        variant = getattr(dv, "Value", None)
        value = getattr(variant, "Value", variant)
        if value is None:
            return base.no_data(address, "the server returned an empty value")
        source = getattr(dv, "SourceTimestamp", None) or getattr(dv, "ServerTimestamp", None)
        if source is not None:
            return base.Reading(tag=address, value=value, quality=base.GOOD,
                                timestamp=source.timestamp(), source_time=True)
        return base.Reading(tag=address, value=value, quality=base.GOOD)

    # -- subscription --------------------------------------------------
    async def subscribe(self, addresses, period_ms=None):
        """Ask the server to push changes, so a short pulse is not missed.

        A polled `running` bit at 5s misses a 400ms stoppage entirely, and a
        polled counter misses nothing but arrives late. Subscribing is how the
        fast signals stay honest without polling the whole tag list at 200ms.
        """
        if self._client is None:
            raise base.AdapterError("not connected")
        period = float(period_ms or self.settings.get("subscription_ms") or DEFAULT_SUBSCRIPTION_MS)
        self._collector = _ChangeCollector()
        self._sub = await self._client.create_subscription(period, self._collector)
        nodes = [self._client.get_node(str(a)) for a in addresses]
        self._handles = await self._sub.subscribe_data_change(nodes)
        return self._sub

    def drain_subscription(self) -> list:
        """Readings gathered since the last drain. Empty when nothing changed.

        EMPTY IS NOT ZERO. Nothing changed means exactly that, and the caller
        must keep the previous value rather than infer a stop.
        """
        if self._collector is None:
            return []
        out = []
        for address, (value, status, source) in list(self._collector.latest.items()):
            if status is not None and hasattr(status, "is_good") and not status.is_good():
                out.append(base.Reading(tag=address, value=None, quality=base.BAD,
                                        detail="the server marked this value bad"))
                continue
            stamp = source.timestamp() if source is not None else time.time()
            out.append(base.Reading(tag=address, value=value, quality=base.GOOD,
                                    timestamp=stamp, source_time=source is not None))
        self._collector.latest.clear()
        return out

    # -- discovery -----------------------------------------------------
    async def browse(self, root=None) -> list:
        """Readable variables under `root`, for the commissioning screen."""
        if self._client is None:
            raise base.AdapterError("not connected")
        node = self._client.get_node(root) if root else self._client.nodes.objects
        found = []
        await self._walk(node, found, depth=0)
        return found

    async def _walk(self, node, found, depth):
        # Bounded deliberately: a real server's address space is tens of
        # thousands of nodes, and an unbounded browse on a commissioning laptop
        # looks exactly like a hang.
        if depth > 4 or len(found) >= 500:
            return
        try:
            children = await node.get_children()
        except Exception:                            # noqa: BLE001 - unbrowsable branch
            return
        for child in children:
            try:
                class_ = await child.read_node_class()
                name = (await child.read_browse_name()).Name
            except Exception:                        # noqa: BLE001
                continue
            if class_ == ua.NodeClass.Variable:
                entry = {"node": str(child.nodeid), "name": name, "datatype": None}
                try:
                    dv = await child.read_data_value()
                    value = getattr(getattr(dv, "Value", None), "Value", None)
                    entry["datatype"] = type(value).__name__ if value is not None else None
                except Exception:                    # noqa: BLE001 - unreadable is still listable
                    pass
                found.append(entry)
            else:
                await self._walk(child, found, depth + 1)

    def describe(self) -> dict:
        out = super().describe()
        out["bad_tags"] = sorted(self.bad_tags)
        out["subscribed"] = self._sub is not None
        return out
