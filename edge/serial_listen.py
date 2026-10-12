"""Listen to a serial port and print whatever a controller says. READ ONLY.

WHY LISTEN BEFORE PROBING. A controller's serial port does one of two things: it
TALKS unprompted (many injection moulding controllers stream a line per cycle, or
per shot, to feed a printer or a counter box), or it ANSWERS when polled. The two
need completely different tools, and listening is the one that costs nothing and
risks nothing. If the port is talking, sixty seconds of silence on your side
tells you the protocol, the baud rate and the format all at once.

So this never transmits. Not a probe byte, not a Modbus frame, nothing. It opens
the port, reads, and prints what arrives as hex and as text. A port that is
wired into a running machine is not a place to experiment with writes.

IT TRIES THE LIKELY LINE SETTINGS FOR YOU. A serial port has no way to tell you
its baud rate; you find it by looking for the setting at which the bytes stop
being rubbish. ASCII that reads as words, or frames that repeat at a steady
length, is the signal that the setting is right.

USAGE
  List the ports:            python serial_listen.py --ports
  Listen on one setting:     python serial_listen.py --port COM3 --baud 9600
  Sweep the likely ones:     python serial_listen.py --port COM3 --sweep
  Listen for a long time:    python serial_listen.py --port COM3 --baud 9600 --seconds 120

Run --sweep first. If one setting produces readable text or repeating frames,
re-run on that setting with --seconds 120 and let the machine complete a few
cycles, so you can see whether a number in the line counts up.
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

# Ordered by how often they turn up on Asian plastics controllers.
BAUDS = (9600, 19200, 38400, 4800, 57600, 115200)
# (bytesize, parity, stopbits)
FRAMES = ((8, "N", 1), (8, "E", 1), (7, "E", 1), (8, "O", 1), (8, "N", 2))


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
    """The port to work on, or None with a sentence saying why not.

    THE DEFECT THIS FIXES. `--sweep` with no `--port` used to print the port
    list and exit 0, so it looked like it had run and found nothing. On a floor,
    with one adapter plugged into one machine, that is the whole command being
    silently a no-op -- and a sweep that produces no output is indistinguishable
    from a port that said nothing, which is the one answer this tool exists to
    tell apart.

    With exactly one port there is nothing to choose, so it is chosen.
    """
    found = ports()
    if named:
        return named
    if len(found) == 1:
        print(f"\nno --port given and only {found[0]} is present, so using it.")
        return found[0]
    if not found:
        return None
    print("\nSeveral ports are present. Name one with --port, e.g. "
          f"--port {found[0]}")
    return None


def _render(chunk):
    """The same bytes twice: hex, and text with unprintables as dots."""
    hexed = " ".join(f"{b:02X}" for b in chunk)
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return hexed, text


def listen(port, baud, frame, seconds, quiet=False):
    """Open, read, print. Returns the number of bytes seen. Never writes."""
    bytesize, parity, stopbits = frame
    label = f"{baud} {bytesize}{parity}{stopbits}"
    try:
        con = serial.Serial(
            port=port, baudrate=baud, bytesize=bytesize, parity=parity,
            stopbits=stopbits, timeout=0.3)
    except Exception as exc:             # noqa: BLE001 - a probe never dies
        print(f"  {label:<12} could not open: {exc}")
        return 0

    seen = 0
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            chunk = con.read(256)
            if not chunk:
                continue
            seen += len(chunk)
            hexed, text = _render(chunk)
            stamp = time.strftime("%H:%M:%S")
            print(f"  [{stamp}] {len(chunk):>3}B  {hexed[:72]}")
            print(f"             {text[:72]}")
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        con.close()

    if not seen and not quiet:
        print(f"  {label:<12} silent for {seconds}s")
    return seen


def sweep(port, seconds):
    """Every likely setting in turn. Anything that speaks is worth a longer look."""
    print(f"sweeping {port}: {len(BAUDS) * len(FRAMES)} settings, {seconds}s each\n"
          f"(total about {len(BAUDS) * len(FRAMES) * seconds // 60} minutes -- "
          f"let the machine keep running)\n")
    talking = []
    for baud in BAUDS:
        for frame in FRAMES:
            label = f"{baud} {frame[0]}{frame[1]}{frame[2]}"
            print(f"-- {label}")
            seen = listen(port, baud, frame, seconds, quiet=True)
            if seen:
                print(f"  ** {seen} bytes at {label} **")
                talking.append((label, seen))
    print()
    if not talking:
        print("NOTHING AT ANY SETTING.")
        print("That means the port does not talk unprompted. It may still ANSWER")
        print("a poll -- try modbus_discover.py next. Or the port is not wired,")
        print("or it is RS-485 and this is an RS-232 adapter (the electrical")
        print("levels are different and neither will hear the other).")
        return
    print("SETTINGS THAT PRODUCED BYTES:")
    for label, seen in sorted(talking, key=lambda t: -t[1]):
        print(f"  {label:<14} {seen} bytes")
    print("\nRe-run the best one with --seconds 120 and watch whether a number")
    print("in the output counts up as the machine makes parts.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only serial listener.")
    ap.add_argument("--ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", choices=("N", "E", "O"), default="N")
    ap.add_argument("--bytesize", type=int, default=8)
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--sweep", action="store_true", help="try the likely settings in turn")
    args = ap.parse_args(argv)

    if args.ports:
        ports()
        return 0

    port = choose_port(args.port)
    if port is None:
        return 1

    if args.sweep:
        print()
        sweep(port, max(3, min(args.seconds, 20)))
        return 0

    frame = (args.bytesize, args.parity, args.stopbits)
    print(f"\nlistening on {port} at {args.baud} "
          f"{frame[0]}{frame[1]}{frame[2]} for {args.seconds}s -- Ctrl+C to stop\n")
    listen(port, args.baud, frame, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
