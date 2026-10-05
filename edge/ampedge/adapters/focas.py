"""FANUC FOCAS over TCP. The protocol that is a product, read without the product.

WHY THIS FILE EXISTS. FANUC is the most common CNC control in the job shops AMP
sells to, and it speaks neither OPC UA nor Modbus. Its own interface is FOCAS —
a proprietary binary protocol on TCP 8193, normally reached through FANUC's
`fwlib` C library, which is licensed software. Depending on that library would
have put a commercial negotiation in front of every FANUC pilot and FANUC's own
binary inside AMP's distribution.

So this adapter speaks the wire format instead, via `pyfocas` (MIT), which
reimplements the protocol over an ordinary socket. No vendor library, no DLL, no
32-bit interpreter. What that costs is coverage: `pyfocas` implements four
functions, and the two this adapter uses are the two that matter.

WHAT WAS LEARNED FROM A REAL CONTROL, because none of it is in a datasheet.
Verified 2026-10-05 against a BFW BMV 45+ TC24 on a FANUC Series 0i-MF Plus:

  * A RANGE READ IS REFUSED. `read_macro(3901, 3902)` comes back with a payload
    the library cannot match to the command; `read_macro(3901)` answers
    immediately. So this adapter reads one macro per request and never batches,
    which is slower and is the only thing that works.

  * AN UNDEFINED MACRO IS FLAGGED, NOT ZERO. The control sets the trailing
    bytes of the value to FF FF, and `pyfocas` raises `DecodingError` on the
    sentinel. That is caught here and becomes `no_data` — which is exactly
    right, and is the protocol handing us AMP's own absence-is-not-zero rule for
    free. A macro nobody has set must never read as a machine that has made
    nothing. (The first draft of this adapter guessed `AssertionError` from
    reading pyfocas's source; the real control raised `DecodingError`, which is
    the kind of thing only a physical machine tells you.)

  * #3002 IS NOT THE OPERATOR'S RUN TIME. It read 2128.36 hours against an HMI
    showing 602H59M. #3002 is a general-purpose hour timer; the operating time
    on the screen lives in parameters 6751/6752, and a parameter read is not one
    of the four functions available. So this adapter does NOT offer
    `operating_hours`, rather than offering a number that looks like it and is
    not. AMP derives runtime from `running` over time anyway.

THE ADAPTER RETURNS RAW VALUES, as every adapter must. `status:run` yields the
integer FANUC puts on the wire — 0, 1, 2 or 3 — and the mapping says which of
those the plant calls running. Deciding here that 3 means running would put a
control vendor's vocabulary inside AMP, which is the thing `signals.py` exists to
prevent.

THIS ADAPTER NEVER WRITES. `pyfocas` can set macro variables; a macro on a CNC
can be a tool offset or a cycle parameter. Nothing here calls it, and nothing
here should ever be given a reason to.
"""
import asyncio
import time

from . import base

try:                                    # pragma: no cover - import guard
    from pyfocas.protocol.protocol import FOCAS
    from pyfocas.protocol.packet import DecodingError
    AVAILABLE = True
except ImportError:                     # pragma: no cover
    FOCAS = None

    class DecodingError(Exception):     # so the except clause below still binds
        pass

    AVAILABLE = False

DEFAULT_PORT = 8193
DEFAULT_TIMEOUT = 5.0

MACRO = "macro"
STATUS = "status"

#: The seven fields FANUC's status record carries. Enumerated so `browse()` can
#: answer honestly for the half of this protocol that IS enumerable, and so a
#: typo in a config is refused at read time with the alternatives named rather
#: than silently reading nothing.
STATUS_FIELDS = ("aut", "run", "motion", "mstb", "emergency", "alarm", "edit")


def resolve_address(raw):
    """(kind, key, how_it_will_be_read). Raises rather than guessing.

    FOCAS has several namespaces and a bare number belongs to none of them
    unambiguously, so an address says which it means:

        macro:3901     a macro variable
        status:run     a field of the status record

    A bare number is accepted as a macro, because that is the only namespace
    whose addresses are numbers, and the interpretation is stated back in the
    reading's detail rather than assumed silently — the same contract the Modbus
    adapter keeps for 4xxxx-style addresses.
    """
    text = str(raw).strip()
    if not text:
        raise base.AdapterError("an empty FOCAS address names nothing")
    if ":" in text:
        prefix, _, rest = text.partition(":")
        kind = prefix.strip().lower()
        rest = rest.strip()
    else:
        kind, rest = MACRO, text

    if kind == STATUS:
        field = rest.lower()
        if field not in STATUS_FIELDS:
            raise base.AdapterError(
                f"{raw!r} names no FOCAS status field. Available: "
                f"{', '.join(STATUS_FIELDS)}.")
        return STATUS, field, f"status.{field}"

    if kind != MACRO:
        raise base.AdapterError(
            f"{raw!r} is not a FOCAS address. Use macro:<number> or status:<field>.")

    try:
        number = int(rest)
    except ValueError:
        raise base.AdapterError(f"{raw!r} is not a macro number")
    if number < 1:
        raise base.AdapterError(f"macro numbers start at 1, not {number}")
    how = f"macro #{number}"
    if ":" not in text:
        how += " (bare number read as a macro)"
    return MACRO, number, how


class FocasAdapter(base.Adapter):
    """Settings: host, port, timeout."""

    protocol = "focas"

    def __init__(self, settings: dict):
        super().__init__(settings)
        if not AVAILABLE:
            raise base.AdapterError(
                "the `pyfocas` package is not installed, so this gateway cannot speak FOCAS. "
                "Install it with `pip install pyfocas` — it is pure Python and needs no FANUC "
                "library.")
        self.host = str(self.settings.get("host") or "")
        if not self.host:
            raise base.AdapterError("focas needs a `host`, e.g. 192.168.1.1")
        self.port = int(self.settings.get("port") or DEFAULT_PORT)
        self.timeout = float(self.settings.get("timeout") or DEFAULT_TIMEOUT)
        self._client = None
        self.bad_tags = set()
        #: Reported in health so a commissioning engineer can confirm at a glance
        #: that the gateway is talking to the control they think it is.
        self.system = {}

    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    # -- lifecycle -----------------------------------------------------
    async def connect(self):
        self.state = base.CONNECTING
        client = FOCAS(self.host, self.port)
        try:
            opened = await asyncio.wait_for(
                asyncio.to_thread(client.connect), timeout=self.timeout + 2)
        except asyncio.TimeoutError:
            self.state = base.ERROR
            self.last_error = (f"no FOCAS answer from {self.endpoint()} within "
                               f"{self.timeout:.0f}s (check the IP and that the cable is in)")
            raise base.AdapterError(self.last_error)
        except Exception as e:                       # noqa: BLE001
            self.state = base.ERROR
            self.last_error = f"could not reach {self.endpoint()}: {type(e).__name__}"
            raise base.AdapterError(self.last_error)

        if not opened:
            self.state = base.ERROR
            # The commonest cause by a distance, and the one a datasheet hides:
            # the control has Ethernet but the FOCAS2 option was never enabled,
            # so the port is there and nothing is behind it.
            self.last_error = (
                f"{self.endpoint()} did not open a FOCAS session. Check SYSTEM -> EMBED PORT -> "
                f"FOCAS2 on the control: if that screen has no port number, the FOCAS2 option is "
                f"not enabled on this machine.")
            raise base.AdapterError(self.last_error)

        self._client = client
        self.state = base.CONNECTED
        self.connected_at = time.time()
        self.last_error = ""
        self.bad_tags = set()
        try:
            info = await asyncio.to_thread(client.get_sys_info)
            self.system = {
                "cnc_type": getattr(info, "cnc_type", ""),
                "mt_type": getattr(info, "mt_type", ""),
                "series": getattr(info, "series", ""),
                "version": getattr(info, "version", ""),
                "axes": getattr(info, "axes", ""),
            }
        except Exception:                            # noqa: BLE001
            # Identification is a convenience. A control that will not describe
            # itself can still be read, and refusing the session over it would
            # fail a commissioning for a cosmetic reason.
            self.system = {}
        return self

    async def disconnect(self):
        client, self._client = self._client, None
        if client is not None and getattr(client, "socket", None) is not None:
            try:
                client.socket.close()
            except Exception:                        # noqa: BLE001 - going away anyway
                pass
            client.connected = False
        self.state = base.DISCONNECTED

    # -- reading -------------------------------------------------------
    async def read(self, addresses) -> list:
        """One Reading per address, in order.

        The status record is fetched ONCE per poll however many status fields
        are mapped. Four status tags must describe the same instant: fetching
        per tag would let `running` come from one snapshot and `fault_active`
        from the next, and a machine that stopped between them would be reported
        as running with no alarm when it was neither.
        """
        if self._client is None or self.state not in (base.CONNECTED, base.DEGRADED):
            raise base.AdapterError("not connected")

        specs = []
        for entry in addresses:
            spec = entry if isinstance(entry, dict) else {"address": entry}
            specs.append(spec)

        status, status_error = None, ""
        if any(self._is_status(s.get("address")) for s in specs):
            try:
                status = await self._call(self._client.get_status_info)
            except base.AdapterError:
                raise
            except Exception as e:                   # noqa: BLE001
                status_error = f"status read failed ({type(e).__name__})"

        out = []
        for spec in specs:
            out.append(await self._read_one(spec, status, status_error))
        self.last_read_at = time.time()
        self.state = base.DEGRADED if self.bad_tags else base.CONNECTED
        return out

    @staticmethod
    def _is_status(raw) -> bool:
        try:
            return resolve_address(raw)[0] == STATUS
        except base.AdapterError:
            return False

    async def _read_one(self, spec, status, status_error) -> base.Reading:
        raw_address = spec.get("address")
        tag = str(raw_address)
        try:
            kind, key, how = resolve_address(raw_address)
        except base.AdapterError as e:
            self.bad_tags.add(tag)
            return base.no_data(tag, str(e))

        if kind == STATUS:
            if status is None:
                self.bad_tags.add(tag)
                return base.no_data(tag, status_error or "the status record was not read")
            value = getattr(status, key, None)
            if value is None:
                self.bad_tags.add(tag)
                return base.no_data(tag, f"the control's status record has no {how}")
            self.bad_tags.discard(tag)
            # FOCAS stamps nothing. `source_time=False` tells the normalizer this
            # clock is the gateway's, exactly as Modbus does.
            return base.Reading(tag=tag, value=int(value), quality=base.GOOD, source_time=False)

        try:
            # ONE macro per request. A range is refused by the control; see the
            # module docstring.
            got = await self._call(self._client.read_macro, key)
        except base.AdapterError:
            raise
        except (DecodingError, AssertionError):
            # The control flagged the value invalid (FF FF sentinel), which is
            # how an undefined macro arrives. It is absence, not zero. Both
            # exception types are caught because which one pyfocas raises
            # depends on where the sentinel lands in the payload.
            self.bad_tags.add(tag)
            return base.no_data(
                tag, f"{how} is not defined on this control (the control flagged the value "
                     f"invalid, which is not the same as zero)")
        except Exception as e:                       # noqa: BLE001
            self.bad_tags.add(tag)
            return base.no_data(tag, f"read of {how} failed ({type(e).__name__}: {e})")

        if not isinstance(got, dict) or key not in got:
            self.bad_tags.add(tag)
            return base.no_data(tag, f"{how} returned nothing")
        value = got[key]
        if value is None:
            self.bad_tags.add(tag)
            return base.no_data(tag, f"{how} is not defined on this control")
        self.bad_tags.discard(tag)
        return base.Reading(tag=tag, value=value, quality=base.GOOD, source_time=False)

    async def _call(self, fn, *args):
        """Run a blocking pyfocas call off the event loop, mapping socket death.

        `pyfocas` is synchronous. Calling it inline would block the poll loop,
        and the runner's whole design is that reading and publishing cannot
        block each other. A dead socket is raised as AdapterError so the runner
        reconnects rather than reporting every tag separately missing.
        """
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.state = base.DISCONNECTED
            self.last_error = f"no answer from {self.endpoint()} within {self.timeout:.0f}s"
            raise base.AdapterError(self.last_error)
        except (ConnectionError, OSError) as e:
            self.state = base.DISCONNECTED
            self.last_error = f"connection lost: {type(e).__name__}"
            raise base.AdapterError(self.last_error)

    # -- discovery -----------------------------------------------------
    async def browse(self, root=None) -> list:
        """The status fields, which are enumerable. Macros, which are not.

        A FOCAS macro space is 10,000 numbers with no names and no way to ask
        which are in use, so listing them would mean reading all of them and
        calling whatever answered a discovery. That is not discovery, it is a
        scan of someone's live CNC. The status record genuinely can be listed,
        so it is — and a commissioning engineer gets the half the protocol can
        actually describe.
        """
        if self._client is None:
            return []
        try:
            status = await self._call(self._client.get_status_info)
        except Exception:                            # noqa: BLE001
            return []
        return [{"address": f"status:{field}",
                 "value": getattr(status, field, None),
                 "datatype": "int"}
                for field in STATUS_FIELDS]

    # -- health --------------------------------------------------------
    def describe(self) -> dict:
        out = super().describe()
        out["bad_tags"] = sorted(self.bad_tags)
        out["system"] = dict(self.system)
        return out
