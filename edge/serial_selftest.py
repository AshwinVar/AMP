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
    python edge/serial_selftest.py --disconnected --watch
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


def handshake_loopback(port, baud):
    """A SECOND loopback, on different pins, that needs no data at all.

    RTS (pin 7) and CTS (pin 8) are adjacent on the bottom row, and the driver
    can drive one and read the other directly. Short them and toggling RTS
    moves CTS; leave them open and CTS does not follow.

    WHY BOTHER WHEN 2-3 EXISTS. It isolates the adapter from the bridging. If
    7-8 follows and 2-3 never does, the adapter and its driver are working and
    the problem is the short on 2-3, or those two pins specifically. If NEITHER
    follows while you are certain the metal is bridged, the adapter is the
    suspect. One paperclip answers a different question depending on where you
    put it, and that is worth more than running the same test harder.

    Returns (followed, detail). `followed` is True only when CTS tracked RTS in
    BOTH directions -- a CTS stuck high would otherwise read as a pass.
    """
    try:
        con = serial.Serial(port=port, baudrate=baud, timeout=0.2,
                            rtscts=False, dsrdtr=False)
    except Exception as exc:             # noqa: BLE001
        return False, f"could not open: {exc}"
    try:
        seen = []
        for state in (True, False, True):
            con.rts = state
            time.sleep(0.08)
            seen.append(con.cts)
        if seen == [True, False, True]:
            return True, "CTS followed RTS both ways"
        if len(set(seen)) == 1:
            return False, f"CTS stuck {'high' if seen[0] else 'low'}"
        return False, f"CTS moved but did not track ({seen})"
    except Exception as exc:             # noqa: BLE001
        return False, f"could not drive the lines: {exc}"
    finally:
        con.close()


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
        print()
        print("    1. FIND PINS 2 AND 3. The numbering MIRRORS between a plug")
        print("       and a socket, which is what catches everybody. Hold the")
        print("       connector with the D-shape's WIDE edge up, looking")
        print("       straight at the pins or holes:")
        print()
        print("         a PLUG   (pins)  top row, left to right: 1 2 3 4 5")
        print("         a SOCKET (holes) top row, left to right: 5 4 3 2 1")
        print()
        print("       So on a plug they are the 2nd and 3rd from the LEFT; on a")
        print("       socket, the 2nd and 3rd from the RIGHT. Most connectors")
        print("       have 1, 5, 6 and 9 moulded beside the end pins -- find a")
        print("       number before trusting the count.")
        print()
        print("    2. SHORT THE ADAPTER'S OWN PINS FIRST, with nothing else")
        print("       attached. That tests the adapter alone. If it passes,")
        print("       fit the lead and short the FAR end -- the end that goes")
        print("       into the machine. Passing that proves the adapter, the")
        print("       lead and both connectors, end to end, which is the whole")
        print("       chain you care about.")
        print()
        print("    3. A paperclip must touch METAL. On a plug, press it down")
        print("       into the gap so it bears on the sides of both pins; on a")
        print("       socket it has to go INTO both holes, which a thick clip")
        print("       will not do -- use a bent staple or a jumper wire.")
        print()
        print("    4. Try a different USB port, then a different adapter.")
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
    ap.add_argument("--watch", action="store_true",
                    help="retry until the bytes come back")
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

    if args.watch:
        # ONE HAND IS HOLDING THE PAPERCLIP. Re-running a command between every
        # attempt means putting the clip down, which is when it moves. This
        # retries until it sees the bytes come back, so you can wiggle the clip
        # and watch the line change.
        print(f"\nwatching {port}. Two independent loopbacks -- bridge EITHER:")
        print("    DATA       pins 2 + 3   (top row, adjacent)")
        print("    HANDSHAKE  pins 7 + 8   (bottom row, adjacent)")
        print("Ctrl+C to stop.\n")
        tries = 0
        hand_ok = False
        try:
            while True:
                tries += 1
                sent, got, verdict = loopback(port, args.baud, 0.6)
                followed, detail = handshake_loopback(port, args.baud)
                hand_ok = hand_ok or followed
                mark = "OK  " if verdict == "EXACT MATCH" else "    "
                print(f"  {mark}try {tries:<4} data: {len(got):>3}/{sent} "
                      f"{verdict:<20} handshake: "
                      f"{'FOLLOWED' if followed else detail}")
                if verdict == "EXACT MATCH":
                    print()
                    return report(port, args.baud, sent, got, verdict)
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n  stopped -- the data loopback never came back.")
            if hand_ok:
                print()
                print("  BUT THE HANDSHAKE LOOPBACK DID FOLLOW at least once.")
                print("  That means the adapter and its driver are working: it")
                print("  drove RTS and read it back on CTS. So the fault is in")
                print("  the bridge on pins 2 and 3, or in those two pins -- it")
                print("  is NOT a dead adapter.")
            else:
                print()
                print("  AND THE HANDSHAKE LOOPBACK NEVER FOLLOWED EITHER. If")
                print("  you are confident metal was bridging a pair, that")
                print("  points at the adapter rather than at your technique.")
                print("  Try another USB port, then another adapter.")
            return 1

    print(f"\nloopback self-test on {port} -- pins 2 and 3 must be shorted")
    sent, got, verdict = loopback(port, args.baud, args.seconds)
    return report(port, args.baud, sent, got, verdict)


if __name__ == "__main__":
    sys.exit(main())
