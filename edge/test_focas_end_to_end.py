"""FANUC FOCAS through the SAME pipeline, into the same AMP machine.

THE POINT IS THE THIRD PROTOCOL CHANGES NOTHING ABOVE THE ADAPTER. OPC UA is
typed and self-describing, Modbus is a numbered hole in a device's memory, and
FOCAS is a proprietary binary protocol whose addresses are macro numbers and
status fields. All three must arrive at AMP as identical canonical signals,
through the identical mapper and normalizer. If that is not true then every
read-model has to know which control a plant happens to own.

DRIVEN AGAINST A REAL SOCKET. The fake below is a FOCAS server, not a mock of
the adapter: it speaks the packet framing, and the adapter's own `pyfocas`
client decodes it. A mock would have proved only that the adapter calls the
functions it calls.

WHAT IT PINS, each of which is a real behaviour of a real FANUC that this
adapter had to be shaped around (verified 2026-10-05 against a BFW BMV 45+ TC24
on a Series 0i-MF Plus):

  * a macro read becomes a canonical counter, and the HMI's number is the one
    that arrives
  * AN UNDEFINED MACRO IS REFUSED, NOT READ AS ZERO. The control flags the value
    with an FF FF sentinel. A machine that has made nothing and a macro nobody
    configured must never look the same.
  * THE STATUS RECORD IS FETCHED ONCE PER POLL however many status tags are
    mapped — four tags must describe one instant, or `running` comes from one
    snapshot and `fault_active` from the next
  * `run` is returned RAW, so the mapping decides which FANUC run-state the
    plant calls running. Deciding here would put a control vendor's vocabulary
    inside AMP.
  * a dead socket raises AdapterError so the runner reconnects, rather than
    reporting every tag separately missing

Run: DATABASE_URL="sqlite:///./ci_edge.db" python edge/test_focas_end_to_end.py
"""
import asyncio
import os
import socket
import struct
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_edge.db")

from ampedge import mapping as mapping_mod        # noqa: E402
from ampedge import normalizer as normalizer_mod  # noqa: E402
from ampedge.adapters import base                 # noqa: E402
from ampedge.adapters.focas import (              # noqa: E402
    MACRO, STATUS, FocasAdapter, resolve_address)

from pyfocas.protocol.packet import (             # noqa: E402
    SYNC_PREFIX, FOCASSysInfo, PacketOrigin, PacketType)
# Private, deliberately: the fake server must pack the status record in exactly
# the layout the real client unpacks, and borrowing the client's own struct is
# the only way to guarantee that rather than re-deriving it and drifting.
from pyfocas.protocol.packet import _FOCAS_STATINFO_STRUCT  # noqa: E402

failures = []
HOST, PORT = "127.0.0.1", 45031

GET_SYS_INFO = (0x01, 0x18)
GET_STAT_INFO = (0x01, 0x19)
READ_MACRO = (0x01, 0x15)


def response(packet_type, frame: bytes) -> bytes:
    """Frame a server packet the way a real control does.

    NOT `pyfocas.create_packet`, which is asymmetric: it adds the subpacket
    header for a REQUEST and not for a RESPONSE. Passing a response through it
    produces a packet whose own client decodes as an empty payload — which is
    how this fake first "worked" while every value came back None.
    """
    body = struct.pack(">HH", 1, len(frame) + 2) + frame
    return SYNC_PREFIX + struct.pack(">HHH", PacketOrigin.SERVER,
                                     packet_type, len(body)) + body


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{name}: {detail}")


def scaled(mantissa: int, exponent: int) -> bytes:
    """A FOCAS macro value: int32, unused, base 10, valid, exponent."""
    return struct.pack(">i", mantissa) + b"\x00\x0a\x00" + struct.pack(">B", exponent)


#: The FF FF sentinel a control puts on a macro nobody has defined. The whole
#: absence-is-not-zero rule rests on this being refused rather than decoded.
UNDEFINED = struct.pack(">i", 0) + b"\x00\x0a\xff\xff"


class FakeFocas(threading.Thread):
    """A FOCAS server good enough for a real client to talk to.

    `macros` maps a number to 8 packed bytes; a number that is absent answers
    with the undefined sentinel, exactly as the control does.
    """

    daemon = True

    def __init__(self, macros, status=(5, 0, 0, 0, 0, 0, 0)):
        super().__init__()
        self.macros = dict(macros)
        self.status = tuple(status)
        self.status_reads = 0            # proves the once-per-poll guarantee
        self.errors = []
        self.macro_reads = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, PORT))
        self.sock.listen(4)
        self.stop_flag = False
        self.conns = []

    def run(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        # A fake that dies quietly is worse than no fake: the client just times
        # out and the failure looks like the adapter's.
        try:
            self._serve_inner(conn)
        except Exception as e:                       # noqa: BLE001
            self.errors.append(f"{type(e).__name__}: {e}")

    def _serve_inner(self, conn):
        while not self.stop_flag:
            try:
                data = conn.recv(1500)
            except OSError:
                return
            if not data:
                return
            packet_type = struct.unpack(">H", data[6:8])[0]
            if packet_type == PacketType.OPEN_REQUEST:
                conn.sendall(response(PacketType.OPEN_RESPONSE,
                                      struct.pack(">H", PacketOrigin.SERVER)))
                continue
            body = data[12:]             # 4 sync + 2 origin + 2 type + 2 len + 2 subpacket_count
            body = body[2:]              # subpacket_length
            command, args = body[:6], body[6:26]
            function = struct.unpack(">HH", command[2:6])
            payload = self._answer(function, struct.unpack(">iiiii", args))
            frame = command + b"\x00" * 6 + struct.pack(">H", len(payload)) + payload
            conn.sendall(response(PacketType.GENERIC_RESPONSE, frame))

    def _answer(self, function, args):
        if function == GET_STAT_INFO:
            self.status_reads += 1
            return _FOCAS_STATINFO_STRUCT.pack(*self.status)
        if function == GET_SYS_INFO:
            return FOCASSysInfo(addinfo=1570, max_axis=32, cnc_type="0", mt_type="M",
                                series="D4G3", version="57.0", axes="05").to_bytes()
        if function == READ_MACRO:
            first = args[0]
            self.macro_reads.append((args[0], args[1]))
            return self.macros.get(first, UNDEFINED)
        return b""

    def shutdown(self):
        self.stop_flag = True
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


TAGS = [
    {"tag": "parts", "address": "macro:3901", "signal": "part_count",
     "datatype": "int", "counter_mode": "cumulative"},
    {"tag": "run", "address": "status:run", "signal": "running", "datatype": "bool",
     "true_values": [3], "false_values": [0, 1, 2, 4]},
    {"tag": "alarm", "address": "status:alarm", "signal": "fault_active", "datatype": "bool"},
    {"tag": "mode", "address": "status:aut", "signal": "fault_code", "datatype": "int"},
    {"tag": "ghost", "address": "macro:100", "signal": "good_count",
     "datatype": "int", "counter_mode": "cumulative"},
]


async def main():
    print("\nFOCAS -> canonical signals -> AMP\n" + "-" * 74)

    # ---- address resolution, before a socket is opened -------------------
    check("macro:3901 resolves to a macro", resolve_address("macro:3901")[:2] == (MACRO, 3901))
    check("status:run resolves to a status field", resolve_address("status:run")[:2] == (STATUS, "run"))
    kind, key, how = resolve_address("3901")
    check("a bare number is read as a macro AND says so",
          (kind, key) == (MACRO, 3901) and "bare number" in how, how)
    for bad, why in (("status:nonsense", "unknown status field"),
                     ("pmc:17", "unsupported namespace"),
                     ("macro:0", "macro numbers start at 1"),
                     ("macro:abc", "not a number"),
                     ("", "empty")):
        try:
            resolve_address(bad)
            check(f"{why} is refused", False, f"{bad!r} was accepted")
        except base.AdapterError:
            check(f"{why} is refused", True)

    server = FakeFocas(macros={3901: scaled(168300000, 5)}, status=(5, 3, 1, 0, 0, 0, 0))
    server.start()
    try:
        adapter = FocasAdapter({"host": HOST, "port": PORT, "timeout": 3})
        await adapter.connect()
        check("a FOCAS session opens", adapter.state == base.CONNECTED, adapter.last_error)
        check("the control identifies itself in health",
              adapter.describe()["system"].get("series") == "D4G3",
              str(adapter.describe().get("system")))

        readings = await adapter.read(TAGS)
        by_tag = {r.tag: r for r in readings}

        check("one reading per address, in order",
              [r.tag for r in readings] == [t["address"] for t in TAGS],
              str([r.tag for r in readings]))
        check("the macro value is the number on the operator's screen",
              by_tag["macro:3901"].value == 1683.0, repr(by_tag["macro:3901"].value))
        check("an undefined macro is REFUSED, not zero",
              not by_tag["macro:100"].is_usable
              and by_tag["macro:100"].value is None
              and by_tag["macro:100"].quality == base.NO_DATA,
              repr(by_tag["macro:100"]))
        check("the refusal says it is not the same as zero",
              "not the same as zero" in by_tag["macro:100"].detail,
              by_tag["macro:100"].detail)
        check("run is returned RAW for the mapping to interpret",
              by_tag["status:run"].value == 3, repr(by_tag["status:run"].value))
        check("FOCAS carries no clock, so the reading says the stamp is ours",
              all(r.source_time is False for r in readings))

        # THE CONSISTENCY GUARANTEE. Three status tags, one snapshot.
        check("the status record is read ONCE per poll, not once per tag",
              server.status_reads == 1, f"{server.status_reads} status reads for 3 status tags")

        # A RANGE READ IS REFUSED BY REAL CONTROLS, so we must never send one.
        check("macros are read one at a time, never as a range",
              all(first == last for first, last in server.macro_reads),
              str(server.macro_reads))

        # ---- the pipeline above the adapter ------------------------------
        mappings = mapping_mod.validate(TAGS)
        normalizer = normalizer_mod.Normalizer(mappings)
        values = {s.signal: s.value for s in normalizer.absorb(readings)}
        check("running came through as a real bool from the raw 3",
              values.get("running") is True, repr(values.get("running")))
        check("fault_active is False when the alarm word is 0",
              values.get("fault_active") is False, repr(values.get("fault_active")))
        check("a counter's first reading is a baseline, not production",
              values.get("part_count") in (None, 0), repr(values.get("part_count")))
        check("the refused macro produced NO good_count at all",
              values.get("good_count") is None, repr(values.get("good_count")))

        # The counter advances: 1683 -> 1685 is two parts, not 1685.
        server.macros[3901] = scaled(168500000, 5)
        values2 = {s.signal: s.value for s in normalizer.absorb(await adapter.read(TAGS))}
        check("the second poll reports the INCREMENT, not the total",
              values2.get("part_count") == 2, repr(values2.get("part_count")))

        check("a degraded adapter names the bad tag",
              adapter.describe()["bad_tags"] == ["macro:100"],
              str(adapter.describe()["bad_tags"]))

        # ---- the socket dies ---------------------------------------------
        server.shutdown()
        try:
            await adapter.read(TAGS)
            check("a dead socket raises so the runner reconnects", False, "read() returned")
        except base.AdapterError:
            check("a dead socket raises so the runner reconnects", True)

        await adapter.disconnect()
        check("disconnect leaves the adapter DISCONNECTED",
              adapter.state == base.DISCONNECTED, adapter.state)
    finally:
        server.shutdown()

    # ---- the protocol is refused when the library is absent ---------------
    import ampedge.adapters.focas as focas_mod
    was = focas_mod.AVAILABLE
    focas_mod.AVAILABLE = False
    try:
        FocasAdapter({"host": HOST})
        check("no pyfocas means a named refusal, not a crash later", False, "constructed anyway")
    except base.AdapterError as e:
        check("no pyfocas means a named refusal, not a crash later", "pyfocas" in str(e), str(e))
    finally:
        focas_mod.AVAILABLE = was

    try:
        FocasAdapter({})
        check("a missing host is refused by name", False, "constructed without a host")
    except base.AdapterError as e:
        check("a missing host is refused by name", "host" in str(e), str(e))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - FOCAS to AMP canonical signals, same pipeline as OPC UA/Modbus")
