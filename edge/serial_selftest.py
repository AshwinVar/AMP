"""Prove the adapter, the lead and the driver work, before blaming the machine.

THE QUESTION THIS ANSWERS. A sweep that returns nothing but 0x00 has two
completely different explanations and no way to tell them apart from the output:

    the machine is not talking        -> stop sweeping, find another route
    your kit is not working           -> the machine was never heard at all

A half-seated DB9, a lead with no pin 2, a dead adapter and a controller that
simply does not transmit all produce the same screenful of zeros. Guessing
between them costs hours on a floor, so this spends thirty seconds instead.

HOW. Short pin 2 (RXD) to pin 3 (TXD) on the adapter and everything this tool
sends comes straight back. If the bytes return byte-for-byte, then the adapter,
its driver, the USB cable, the COM port and pyserial are all proven good -- and
a silent machine really is a silent machine. If they do not, the fault is on
your side of the DB9 and no amount of sweeping will find it.

    A paperclip bent into a U across pins 2 and 3 is a perfectly good loopback.
    Pin 2 is the second pin on the long row, pin 3 the third, counting from the
    end with pin 1 marked.

THIS ONE TRANSMITS, WHICH IS WHY IT IS ITS OWN FILE. serial_listen.py never
puts a byte on the wire and test_serial_listen.py reads its source to prove it;
that guarantee is worth keeping absolute, because the listener is the tool you
point at a stranger's running machine. A loopback test cannot be read-only, so
it lives here instead of weakening that promise.

UNPLUG THE LEAD FROM THE MACHINE FIRST. Sending into a live controller's port
is the one thing nobody should do by accident, so this refuses to run until you
say the lead is disconnected.

    python edge/serial_selftest.py --disconnected
    python edge/serial_selftest.py --disconnected --port COM4 --baud 9600
"""
import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:                      # pragma: no cover - import guard
    print("pyserial is not installed.  pip install pyserial")
    raise SystemExit(1)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: Printable, asymmetric, and includes bytes that expose a stuck bit or a
#: parity/framing mismatch: 0x00 and 0xFF would both be produced by a broken
#: line on their own, so neither is useful as a probe on its own.
PROBE = b"AMP-EDGE-LOOPBACK-0123456789-\x55\xAA-abcdefghijklmnopqrstuvwxyz"


def ports():
    """Print what is plugged in. Returns the device names found."""
    found = list(list_ports.comports())
    if not found:
        print("No serial ports found. Is the USB adapter plugged in, and did "
              "Windows install a driver for it? Check Device Manager -> Ports.")
        return []
    print(f"{len(found)} serial port(s):\n")
    for p in found:
        print(f"  {p.device:<8} {p.description}")
        if p.hwid:
            print(f"           {p.hwid}")
    return [p.device for p in found]


def choose_port(named):
    """The port to work on, or None with a sentence saying why not."""
    found = ports()
    if named:
        return named
    if len(found) == 1:
        print(f"\nno --port given and only {found[0]} is present, so using it.")
        return found[0]
    if found:
        print("\nSeveral ports are present. Name one with --port, e.g. "
              f"--port {found[0]}")
    return None


def loopback(port, baud, seconds=3.0):
    """Send PROBE, read it back. Returns (sent, received_bytes, verdict)."""
    try:
        con = serial.Serial(port=port, baudrate=baud, bytesize=8, parity="N",
                            stopbits=1, timeout=0.2)
    except Exception as exc:             # noqa: BLE001 - a probe never dies
        return 0, b"", f"could not open {port}: {exc}"

    try:
        con.reset_input_buffer()
        con.reset_output_buffer()
        con.write(PROBE)
        con.flush()
        got = bytearray()
        deadline = time.time() + seconds
        while time.time() < deadline and len(got) < len(PROBE):
            chunk = con.read(len(PROBE) - len(got))
            if chunk:
                got.extend(chunk)
    finally:
        con.close()

    got = bytes(got)
    if not got:
        return len(PROBE), got, "NOTHING CAME BACK"
    if got == PROBE:
        return len(PROBE), got, "EXACT MATCH"
    if set(got) == {0}:
        return len(PROBE), got, "ONLY ZEROS CAME BACK"
    return len(PROBE), got, "CAME BACK CHANGED"


def report(port, baud, sent, got, verdict):
    print(f"\n  sent {sent} bytes at {baud} 8N1, got {len(got)} back: {verdict}")
    if got and verdict != "EXACT MATCH":
        shown = got[:48]
        print("    " + " ".join(f"{b:02X}" for b in shown))
        print("    " + "".join(chr(b) if 32 <= b < 127 else "." for b in shown))

    print()
    if verdict == "EXACT MATCH":
        print("  THE KIT IS GOOD. Adapter, driver, lead and port all work.")
        print("  So a port that answers nothing really is answering nothing,")
        print("  and the next question is whether the controller speaks at all")
        print("  -- not whether you can hear it.")
        return 0

    if verdict == "NOTHING CAME BACK":
        print("  THE LOOPBACK IS NOT CLOSED, or the kit is faulty. In order:")
        print("    1. Is pin 2 actually shorted to pin 3? They are the second")
        print("       and third pins on the long row, from the pin-1 end.")
        print("    2. If you shorted them on the far end of a LEAD rather than")
        print("       on the adapter itself, the lead may have no pin 2 or 3 --")
        print("       many moulded 'serial' leads are wired for three signals")
        print("       and some for none. Short the ADAPTER's own pins to rule")
        print("       the lead out, then put the lead back and repeat.")
        print("    3. Try the adapter in a different USB port.")
        return 1

    if verdict == "ONLY ZEROS CAME BACK":
        print("  ZEROS, WHICH IS WHAT AN UNCONNECTED INPUT LOOKS LIKE. The")
        print("  adapter is receiving its own noise floor rather than your")
        print("  bytes -- the short is not making contact. If a DB9 will not")
        print("  seat fully because a panel or cover fouls the hood, the pins")
        print("  are not reaching either, and this is exactly what that looks")
        print("  like from the software side.")
        return 1

    print("  BYTES CAME BACK CHANGED, so the line works but something is")
    print("  mangling it: a baud mismatch cannot happen in a loopback, so")
    print("  suspect a marginal connection, a very long lead, or electrical")
    print("  noise coupling into an unshielded cable inside the cabinet.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Loopback self-test for a USB-serial adapter. TRANSMITS.")
    ap.add_argument("--ports", action="store_true",
                    help="list serial ports and exit")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--disconnected", action="store_true",
                    help="confirm the lead is NOT plugged into a machine")
    args = ap.parse_args(argv)

    if args.ports:
        ports()
        return 0

    if not args.disconnected:
        print("This tool TRANSMITS. Unplug the lead from the machine first,")
        print("short pin 2 to pin 3, then re-run with --disconnected:")
        print("\n  python edge/serial_selftest.py --disconnected\n")
        print("Nothing has been sent.")
        return 1

    port = choose_port(args.port)
    if port is None:
        return 1

    print(f"\nloopback self-test on {port} -- pins 2 and 3 must be shorted")
    sent, got, verdict = loopback(port, args.baud, args.seconds)
    return report(port, args.baud, sent, got, verdict)


if __name__ == "__main__":
    sys.exit(main())
