"""What every industrial adapter must be, and what it is never allowed to do.

TWO PROTOCOLS, ONE SHAPE. OPC UA and Modbus TCP agree about almost nothing — one
browses a typed address space over a session, the other reads numbered registers
over a socket with no types at all. Above this file, none of that is visible: an
adapter yields `Reading`s, and the normalizer, buffer, publisher and AMP itself
never learn which wire they came off.

THE RULE THIS FILE EXISTS TO ENFORCE is about absence. A PLC that is unreachable,
a tag that does not exist, a register that returns an exception code — every one
of those is a real and frequent state on a factory network, and the tempting
thing for each is to return 0, or False, or the last value. All three are lies
with the same shape: a stopped line looks exactly like a quiet one, and a
production counter that reads 0 while the PLC is unreachable reports a shift of
scrap. So:

    A Reading NEVER carries a substituted value. It carries a QUALITY, and a
    value only when quality is GOOD.

That is the same rule AMP's read-models already keep one layer up (`ev.Fact`
refuses None unless it is explicitly UNKNOWN; a rate over an empty denominator
is None and not 0). Keeping it here means the lie never gets made in the first
place, rather than being caught later by something that has to guess.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Quality ─────────────────────────────────────────────────────────
#
# OPC UA has a quality code on every value and Modbus has none, so this is the
# common denominator: what the adapter is prepared to claim about the reading.
GOOD = "GOOD"            # the PLC answered and the value is usable
BAD = "BAD"              # the PLC answered and said the value is not usable
UNCERTAIN = "UNCERTAIN"  # OPC UA's middle code: a value, but the server doubts it
STALE = "STALE"          # the server's own timestamp is older than we accept
NO_DATA = "NO_DATA"      # nothing came back at all — unreachable, timeout, no such tag

QUALITIES = (GOOD, BAD, UNCERTAIN, STALE, NO_DATA)

# Only this one may be written into AMP as a fact. UNCERTAIN is deliberately not
# included: an OPC UA server saying "here is a number but I doubt it" is a
# maintenance signal, not a production count.
USABLE = (GOOD,)


# ── Connection state ────────────────────────────────────────────────
#
# Named rather than boolean because "not connected" hides the distinction a
# commissioning engineer needs most: never got there at all (wrong IP, firewall)
# versus was fine and dropped (cable, PLC restart) versus refused us (auth).
DISCONNECTED = "DISCONNECTED"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
DEGRADED = "DEGRADED"        # connected, but some tags are failing
ERROR = "ERROR"              # connect failed and we know why
CONNECTION_STATES = (DISCONNECTED, CONNECTING, CONNECTED, DEGRADED, ERROR)


@dataclass
class Reading:
    """One value off the PLC, with everything needed to judge it.

    `value` is meaningless unless `quality` is GOOD — `is_usable` is the only
    correct way to ask. The dataclass deliberately does not default `value` to
    anything: a reading with no value must be constructed with value=None AND a
    non-GOOD quality, so the absence is explicit at the call site.
    """

    tag: str
    value: Any
    quality: str = GOOD
    # The PLC's OWN timestamp where the protocol has one (OPC UA does), because
    # it is the only clock that knows when the value was true. Modbus has none,
    # so the adapter stamps it at read time and says so via `source_time`.
    timestamp: float = field(default_factory=time.time)
    source_time: bool = False      # True when the timestamp came from the PLC
    detail: str = ""               # why, when quality is not GOOD; never a value

    @property
    def is_usable(self) -> bool:
        return self.quality in USABLE and self.value is not None

    def __repr__(self):            # keeps a value out of a log line by accident
        if self.is_usable:
            return f"<Reading {self.tag}={self.value!r} {self.quality}>"
        return f"<Reading {self.tag} {self.quality}: {self.detail or 'no detail'}>"


def no_data(tag: str, detail: str) -> Reading:
    """The only correct way to say 'we did not get this one'.

    Exists so that no adapter has to remember the convention, and so that
    grepping for `no_data(` finds every place absence is produced.
    """
    return Reading(tag=tag, value=None, quality=NO_DATA, detail=detail)


class AdapterError(Exception):
    """A protocol-level failure the runner should react to, with a reason."""


class Adapter:
    """The interface the runner drives. Subclasses implement the protocol bits.

    Adapters are given ALREADY-VALIDATED mappings: `mapping.validate()` has run,
    every tag names a signal AMP has, every counter has declared its mode. An
    adapter never inspects a mapping's meaning — it reads addresses and hands
    back raw values. Converting a raw value into a canonical signal is the
    mapper's job, and keeping those apart is what lets one protocol be tested
    without a PLC and the other without a mapping.
    """

    #: Shown in health output and in the support matrix. A subclass that has
    #: never been run against real hardware must say so here, not in a comment.
    protocol = "unset"

    def __init__(self, settings: dict):
        self.settings = dict(settings or {})
        self.state = DISCONNECTED
        self.last_error = ""
        self.last_read_at: Optional[float] = None
        self.connected_at: Optional[float] = None

    # -- lifecycle -----------------------------------------------------
    async def connect(self):
        raise NotImplementedError

    async def disconnect(self):
        raise NotImplementedError

    async def read(self, addresses) -> list:
        """Read these addresses. Returns one Reading per address, in order.

        MUST NOT raise for a tag that fails — a single bad node id is normal on
        a first commissioning pass, and it must not stop the other forty tags
        from being read. Return `no_data(tag, why)` for that one instead. Raise
        only when the CONNECTION is gone, because that is what the runner needs
        to react to by reconnecting.
        """
        raise NotImplementedError

    # -- discovery (optional; commissioning convenience, never required) --
    async def browse(self, root=None) -> list:
        """What can be read here. Empty list when the protocol cannot say.

        Modbus genuinely cannot: registers are numbers with no self-description,
        so a Modbus adapter returning [] is correct and not a missing feature.
        """
        return []

    # -- health --------------------------------------------------------
    def describe(self) -> dict:
        """The half of the health report only the adapter knows."""
        return {
            "protocol": self.protocol,
            "state": self.state,
            "connected_at": self.connected_at,
            "last_read_at": self.last_read_at,
            # Never the credential, never the value — the endpoint only, and
            # even that with any userinfo stripped by the subclass.
            "endpoint": self.endpoint(),
            "last_error": self.last_error,
        }

    def endpoint(self) -> str:
        """A printable endpoint WITHOUT credentials. Subclasses must redact."""
        return ""
