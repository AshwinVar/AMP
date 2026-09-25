"""Modbus TCP client. The protocol that tells you nothing, so the config must.

WHAT MAKES MODBUS DIFFERENT FROM OPC UA, and why this file is longer than it
looks like it should be: a Modbus register is a 16-bit number at a number. That
is the entire self-description. It has no name, no datatype, no unit, no quality
code and no timestamp. Whether register 40001 is a part count, a temperature in
tenths of a degree, or the low half of a 32-bit value depends entirely on a
document that lives in a drawer at the customer's site.

So EVERY assumption Modbus cannot state must be stated in configuration:

    register_type   coil | discrete | input | holding
    datatype        int16 | uint16 | int32 | uint32 | float32 | ...
    word_order      which half of a 32-bit value comes first
    scale / offset  raw 485 -> 48.5 degC
    unit            what the number is

and the thing that goes wrong most is `word_order`. A 32-bit counter read with
the halves swapped does not look broken — it looks like a machine that made
458,752 parts. There is no way to detect it from the wire, which is why the
commissioning step that shows a live value next to the engineer's expected value
is not a nicety.

THE OTHER TRAP IS ADDRESSING. Plant documentation is written in the 4xxxx
convention (40001 = holding register 0) and the wire is zero-based. Getting it
wrong reads the register next door, which usually returns a plausible number.
This adapter accepts BOTH and says which it used, rather than silently picking.

NO QUALITY CODE MEANS NO SUBSTITUTION. A Modbus exception response is a refusal,
and it becomes a non-GOOD Reading with no value — never the zero the wire would
happily hand over.
"""
import asyncio
import time

from . import base

try:                                    # pragma: no cover - import guard
    from pymodbus.client import AsyncModbusTcpClient
    AVAILABLE = True
except ImportError:                     # pragma: no cover
    AsyncModbusTcpClient = None
    AVAILABLE = False

DEFAULT_PORT = 502
DEFAULT_TIMEOUT = 5.0
DEFAULT_UNIT = 1

COIL = "coil"
DISCRETE = "discrete"
INPUT = "input"
HOLDING = "holding"
REGISTER_TYPES = (COIL, DISCRETE, INPUT, HOLDING)

# How many 16-bit registers each datatype occupies. The commonest bug this
# prevents: reading one register for a 32-bit value and getting the top half.
WIDTH = {
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}

#: 4xxxx / 3xxxx / 1xxxx / 0xxxx documentation offsets, and what they mean on
#: the wire. Accepted so an engineer can paste the register map as written.
CONVENTION = ((400001, 499999, HOLDING, 400001), (40001, 49999, HOLDING, 40001),
              (300001, 399999, INPUT, 300001), (30001, 39999, INPUT, 30001),
              (100001, 199999, DISCRETE, 100001), (10001, 19999, DISCRETE, 10001))


def resolve_address(raw, declared_type=None):
    """(zero_based_address, register_type, how_it_was_read).

    Accepts 40001-style documentation addresses and plain zero-based ones. The
    third return value exists so the commissioning screen can SHOW which
    interpretation was used — the difference is one register, and one register
    is a completely different number.
    """
    text = str(raw).strip()
    try:
        number = int(text)
    except ValueError:
        raise base.AdapterError(f"{raw!r} is not a Modbus address")
    if number < 0:
        raise base.AdapterError(f"{raw!r} is not a Modbus address")
    for low, high, kind, base_offset in CONVENTION:
        if low <= number <= high:
            # A documentation address wins over a declared type only when they
            # agree; when they disagree the engineer is told rather than
            # overruled, because both are claims about the same register.
            if declared_type and declared_type != kind:
                raise base.AdapterError(
                    f"address {number} is a {kind} register in the standard convention, but the "
                    f"mapping says register_type: {declared_type}. Use a plain zero-based address "
                    f"if you mean something else.")
            return number - base_offset, kind, f"{number} ({kind} {number - base_offset})"
    return number, declared_type or HOLDING, f"{number} (zero-based)"


class ModbusAdapter(base.Adapter):
    """Settings: host, port, unit_id, timeout."""

    protocol = "modbus"

    def __init__(self, settings: dict):
        super().__init__(settings)
        if not AVAILABLE:
            raise base.AdapterError(
                "the `pymodbus` package is not installed, so this gateway cannot speak Modbus "
                "TCP. Install the edge requirements (pip install -r edge/requirements.txt).")
        self.host = str(self.settings.get("host") or "")
        if not self.host:
            raise base.AdapterError("modbus needs a `host`, e.g. 192.168.1.20")
        self.port = int(self.settings.get("port") or DEFAULT_PORT)
        self.unit_id = int(self.settings.get("unit_id", self.settings.get("slave", DEFAULT_UNIT)))
        self.timeout = float(self.settings.get("timeout") or DEFAULT_TIMEOUT)
        self._client = None
        self.bad_tags = set()

    def endpoint(self) -> str:
        return f"{self.host}:{self.port} unit {self.unit_id}"

    # -- lifecycle -----------------------------------------------------
    async def connect(self):
        self.state = base.CONNECTING
        client = AsyncModbusTcpClient(self.host, port=self.port, timeout=self.timeout,
                                      retries=1)
        try:
            await asyncio.wait_for(client.connect(), timeout=self.timeout + 2)
        except asyncio.TimeoutError:
            self.state = base.ERROR
            self.last_error = (f"no answer from {self.endpoint()} within {self.timeout:.0f}s "
                               f"(check the IP, the port and the firewall)")
            raise base.AdapterError(self.last_error)
        except Exception as e:                       # noqa: BLE001
            self.state = base.ERROR
            self.last_error = f"could not reach {self.endpoint()}: {type(e).__name__}"
            raise base.AdapterError(self.last_error)
        if not client.connected:
            self.state = base.ERROR
            self.last_error = (f"{self.endpoint()} did not accept a Modbus TCP connection. "
                               f"Many PLCs need Modbus enabled explicitly in the controller "
                               f"configuration.")
            raise base.AdapterError(self.last_error)
        self._client = client
        self.state = base.CONNECTED
        self.connected_at = time.time()
        self.last_error = ""
        self.bad_tags = set()
        return self

    async def disconnect(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:                        # noqa: BLE001 - going away anyway
                pass
        self._client = None
        self.state = base.DISCONNECTED

    # -- reading -------------------------------------------------------
    async def read(self, addresses) -> list:
        """One Reading per address. `addresses` are mapping specs or plain numbers.

        A spec is preferred, because a bare number cannot say what it is. When
        one is given, its declared register_type, datatype and word_order are
        used; a bare number is read as a holding register of one word, which is
        stated in the reading's detail so nobody has to guess what happened.
        """
        if self._client is None or self.state not in (base.CONNECTED, base.DEGRADED):
            raise base.AdapterError("not connected")
        out = []
        for entry in addresses:
            out.append(await self._read_one(entry))
        self.last_read_at = time.time()
        self.state = base.DEGRADED if self.bad_tags else base.CONNECTED
        return out

    async def _read_one(self, entry) -> base.Reading:
        spec = entry if isinstance(entry, dict) else {"address": entry}
        raw_address = spec.get("address")
        tag = str(raw_address)
        datatype = str(spec.get("modbus_type") or spec.get("raw_type") or "uint16").lower()
        declared = spec.get("register_type")
        try:
            address, kind, how = resolve_address(raw_address, declared)
        except base.AdapterError as e:
            self.bad_tags.add(tag)
            return base.no_data(tag, str(e))

        count = WIDTH.get(datatype, 1)
        try:
            response = await asyncio.wait_for(
                self._call(kind, address, count), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.bad_tags.add(tag)
            return base.no_data(tag, f"no answer for {how} within {self.timeout:.0f}s")
        except (ConnectionError, OSError) as e:
            # The SOCKET is gone. Raising lets the runner reconnect instead of
            # reporting forty separately-missing tags.
            self.state = base.DISCONNECTED
            self.last_error = f"connection lost: {type(e).__name__}"
            raise base.AdapterError(self.last_error)
        except Exception as e:                       # noqa: BLE001
            self.bad_tags.add(tag)
            return base.no_data(tag, f"read of {how} failed ({type(e).__name__})")

        if response is None or response.isError():
            # A Modbus exception response. The wire would happily give us a zero
            # here; a zero is exactly what must not be returned.
            self.bad_tags.add(tag)
            return base.no_data(
                tag, f"the device refused {how}: {self._why(response)}. On most PLCs this means "
                     f"the register does not exist or is outside the configured range.")
        self.bad_tags.discard(tag)

        if kind in (COIL, DISCRETE):
            bits = getattr(response, "bits", None) or []
            if not bits:
                return base.no_data(tag, f"{how} returned no bits")
            # Modbus has no clock. Stamped here, and `source_time=False` says so
            # — the normalizer needs to know this timestamp is the gateway's.
            return base.Reading(tag=tag, value=bool(bits[0]), quality=base.GOOD, source_time=False)

        registers = getattr(response, "registers", None) or []
        if len(registers) < count:
            self.bad_tags.add(tag)
            return base.no_data(
                tag, f"{how} returned {len(registers)} register(s) but {datatype} needs {count}")
        value = self._decode(registers[:count], datatype, spec)
        if value is None:
            self.bad_tags.add(tag)
            return base.no_data(tag, f"could not read {registers[:count]} as {datatype}")
        return base.Reading(tag=tag, value=value, quality=base.GOOD, source_time=False)

    async def _call(self, kind, address, count):
        client = self._client
        if kind == COIL:
            return await client.read_coils(address, count=1, device_id=self.unit_id)
        if kind == DISCRETE:
            return await client.read_discrete_inputs(address, count=1, device_id=self.unit_id)
        if kind == INPUT:
            return await client.read_input_registers(address, count=count, device_id=self.unit_id)
        return await client.read_holding_registers(address, count=count, device_id=self.unit_id)

    def _decode(self, registers, datatype, spec):
        """Registers to a number, honouring the declared word order.

        `word_order` is the setting that silently produces wrong numbers rather
        than errors, so it is explicit here and surfaced in the commissioning
        preview rather than buried.
        """
        if datatype in ("uint16", "int16") and len(registers) == 1:
            raw = registers[0]
            if datatype == "int16" and raw >= 0x8000:
                raw -= 0x10000
            return raw
        order = str(spec.get("word_order") or "big").lower()
        if order not in ("big", "little"):
            order = "big"
        try:
            kind = getattr(AsyncModbusTcpClient.DATATYPE, datatype.upper())
        except AttributeError:
            return None
        try:
            return AsyncModbusTcpClient.convert_from_registers(
                list(registers), kind, word_order=order)
        except Exception:                            # noqa: BLE001 - a decode failure is no_data
            return None

    @staticmethod
    def _why(response):
        code = getattr(response, "exception_code", None)
        return {
            1: "illegal function",
            2: "illegal data address",
            3: "illegal data value",
            4: "device failure",
            6: "device busy",
        }.get(code, f"exception code {code}" if code else "no usable response")

    def describe(self) -> dict:
        out = super().describe()
        out["bad_tags"] = sorted(self.bad_tags)
        out["unit_id"] = self.unit_id
        return out
