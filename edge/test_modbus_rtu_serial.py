"""Modbus over a serial line, for the controllers that have no Ethernet at all.

WHY THIS EXISTS. An injection moulding plant's fourteen controllers have no
Ethernet port between them -- two DB9s on the board and nothing else. The Modbus
adapter spoke TCP only, so reading them meant buying an RTU-to-TCP gateway per
bus BEFORE anyone knew whether the controller answers Modbus at all. A
USB-to-serial lead and a laptop answer that for the price of the lead.

WHAT IS PINNED
  * one connection is one wire: `host` means TCP, `serial_port` means RTU, and
    both together is a refusal rather than a silent preference
  * neither is a refusal too, naming both options
  * the serial line settings reach the client, because a line that disagrees
    about baud or parity reads as RUBBISH or as SILENCE, never as an error
  * the endpoint and the diagnostics describe the wire actually in use -- "check
    the firewall" is useless advice about a cable
  * EVERYTHING ELSE IS UNCHANGED. Modbus RTU is the same protocol over a
    different wire, so the addressing, the register map and the quality handling
    are shared. A second adapter would have forked all of it.

Run: python edge/test_modbus_rtu_serial.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ampedge import config                      # noqa: E402
from ampedge.adapters import base, modbus       # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def refuses(settings, needle, label):
    try:
        modbus.ModbusAdapter(settings)
        check(label, False, "it was accepted")
    except base.AdapterError as e:
        check(label, needle.lower() in str(e).lower(), str(e))


def main():
    section("1. ONE CONNECTION IS ONE WIRE")
    tcp = modbus.ModbusAdapter({"host": "10.0.0.5", "unit_id": 2})
    check("a host alone is Modbus TCP", tcp.is_serial is False)
    check("...and the endpoint names the address", "10.0.0.5:502" in tcp.endpoint(),
          tcp.endpoint())

    rtu = modbus.ModbusAdapter({"serial_port": "COM3", "unit_id": 7})
    check("a serial_port alone is Modbus RTU", rtu.is_serial is True)
    check("...and the endpoint names the LINE, not an address",
          "COM3" in rtu.endpoint() and ":502" not in rtu.endpoint(), rtu.endpoint())

    refuses({"host": "10.0.0.5", "serial_port": "COM3"}, "one wire",
            "both together is refused, not silently preferred")
    refuses({}, "serial_port", "neither is refused, naming both options")

    section("2. THE LINE SETTINGS ARE CARRIED, AND VALIDATED")
    d = modbus.ModbusAdapter({"serial_port": "/dev/ttyUSB0"})
    check("baud defaults to 9600", d.baudrate == 9600, str(d.baudrate))
    check("...parity to N, 8 bits, 1 stop",
          (d.parity, d.bytesize, d.stopbits) == ("N", 8, 1),
          str((d.parity, d.bytesize, d.stopbits)))

    e = modbus.ModbusAdapter({"serial_port": "COM4", "baudrate": 19200, "parity": "e",
                              "bytesize": 7, "stopbits": 2})
    check("...and are taken from the config when given",
          (e.baudrate, e.parity, e.bytesize, e.stopbits) == (19200, "E", 7, 2),
          str((e.baudrate, e.parity, e.bytesize, e.stopbits)))
    check("...with the endpoint spelling the frame out",
          "19200 7E2" in e.endpoint(), e.endpoint())

    # A wrong parity is not a wrong answer on a serial line -- it is noise or
    # nothing. Refusing it at load time is the only place it can be told apart.
    refuses({"serial_port": "COM3", "parity": "Z"}, "N, E or O",
            "an impossible parity is refused rather than guessed")

    section("3. THE DIAGNOSTICS DESCRIBE THE WIRE IN USE")
    # Both fail to connect here (there is no COM-NOPE and no route to 203.0.113.1).
    # WHAT THEY SAY ABOUT IT is the thing under test: "check the firewall" is
    # useless advice about a cable, and "check the baud rate" is useless advice
    # about an IP. Which failure branch fires depends on the machine running the
    # test, so this asserts the vocabulary rather than one sentence.
    import asyncio
    # Which branch fires is platform-dependent -- Windows tolerates a
    # nonexistent COM name until the open, Linux refuses at construction -- so
    # the vocabulary is what is asserted, not one sentence. CI found this
    # difference; the machine that wrote the code could not have.
    SERIAL_WORDS = ("baud", "parity", "driver", "port exists", "port name")
    TCP_WORDS = ("firewall", "ip", "modbus enabled")

    serial = modbus.ModbusAdapter({"serial_port": "COM-NOPE", "timeout": 0.2})
    tcp = modbus.ModbusAdapter({"host": "203.0.113.1", "timeout": 0.2})
    for adapter in (serial, tcp):
        try:
            asyncio.run(adapter.connect())
        except Exception:                        # noqa: BLE001 - any failure is fine
            pass

    smsg = (serial.last_error or "").lower()
    tmsg = (tcp.last_error or "").lower()
    check("the serial failure names the line it tried",
          "com-nope" in smsg, smsg[:110])
    check("...and gives serial advice", any(w in smsg for w in SERIAL_WORDS), smsg[:110])
    check("...and never mentions a firewall",
          "firewall" not in smsg, smsg[:110])
    check("the TCP failure names the address it tried",
          "203.0.113.1" in tmsg, tmsg[:110])
    check("...and gives network advice", any(w in tmsg for w in TCP_WORDS), tmsg[:110])
    check("...and never mentions baud or parity",
          "baud" not in tmsg and "parity" not in tmsg, tmsg[:110])

    section("3b. A CLIENT THAT FAILS TO BUILD IS STILL A DIAGNOSTIC")
    # THE BUG THIS PINS. The client was constructed OUTSIDE the error handling.
    # A TCP client is just an address until it connects, but a serial one
    # resolves the device as it is built -- so a mistyped port raises there,
    # before connect() is reached, and the raw exception escaped with
    # last_error never set. Windows tolerated a nonexistent COM name until the
    # open and Linux did not, so it passed locally and failed in CI.
    #
    # Forced here rather than left to the platform, so this means the same thing
    # on every machine.
    real = modbus.AsyncModbusSerialClient

    def explodes(*a, **k):
        raise OSError("no such device")

    modbus.AsyncModbusSerialClient = explodes
    try:
        boom = modbus.ModbusAdapter({"serial_port": "COM-NOPE"})
        raised = None
        try:
            asyncio.run(boom.connect())
        except base.AdapterError as e:
            raised = str(e)
        except Exception as e:                   # noqa: BLE001
            raised = f"WRONG TYPE: {type(e).__name__}"
        check("a client that cannot be built raises AdapterError, not a raw OSError",
              raised is not None and not str(raised).startswith("WRONG TYPE"), str(raised))
        check("...and last_error is set rather than left empty",
              bool(boom.last_error), repr(boom.last_error))
        check("...naming the port and how to list the real ones",
              "COM-NOPE" in (boom.last_error or "")
              and "port name" in (boom.last_error or "").lower(), boom.last_error)
        check("...and the adapter is left in ERROR, not CONNECTING",
              boom.state == base.ERROR, str(boom.state))
    finally:
        modbus.AsyncModbusSerialClient = real

    section("4. THE CONFIG LOADER ACCEPTS A SERIAL CONNECTION")
    # Driven through validate(), the public entry point, so a renamed private
    # helper cannot turn this into a check of nothing. validate() RAISES on a
    # bad config, so the problems are read off the exception.
    def problems_for(connection):
        cfg = {
            "amp": {"host": "broker.example", "port": 8883, "tenant": "T",
                    "site": "plant-1", "username_env": "U", "password_env": "P"},
            "gateway": {"id": "gw-1", "key_env": "AMP_GATEWAY_KEY"},
            "machines": [{
                "name": "IMM-01", "protocol": "modbus", "poll_interval": 1.0,
                "connection": connection,
                "tags": [{"tag": "count", "address": 40001, "signal": "part_count",
                          "datatype": "int"}],
            }],
        }
        try:
            config.validate(cfg)
            return []
        except config.ConfigError as e:
            return str(e).split("; ")

    serial_problems = [p for p in problems_for({"serial_port": "COM3", "unit_id": 1})
                       if "connection" in p]
    check("a serial machine is accepted by the loader", not serial_problems,
          str(serial_problems))

    both = problems_for({"host": "10.0.0.5", "serial_port": "COM3"})
    check("...host AND serial_port is refused by the loader",
          any("one wire" in p for p in both), str(both)[:170])

    neither = problems_for({})
    check("...and neither is refused, naming both options",
          any("serial_port" in p and "host" in p for p in neither), str(neither)[:170])


if __name__ == "__main__":
    print("=" * 74)
    print("MODBUS OVER A SERIAL LINE")
    print("=" * 74)
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - Modbus RTU over RS-485/RS-232")
