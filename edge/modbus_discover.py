"""Find a machine's counter on an unknown Modbus RTU map. READ ONLY.

THE PROBLEM THIS SOLVES. A Modbus register is a number at a number: it has no
name, no type and no self-description, so an adapter cannot browse one the way
it can browse OPC UA. Normally you need the vendor's register map. When the
handbook is vague, absent or in the wrong language, there is still one thing you
know that the document cannot take away:

    THE SHOT COUNTER ON THE HMI IS GOING UP.

So this takes two snapshots of the controller's registers a few seconds apart and
shows you only what MOVED. A running machine moves very few registers — the shot
counter, a cycle timer, maybe a temperature. Match one against the number on the
HMI and you have found it, with no handbook at all.

IT NEVER WRITES. Only read_holding_registers and read_input_registers are ever
called. A write to an injection moulding controller could change a setpoint on a
machine running unattended, and this tool is used by someone standing next to one.

IT IS DELIBERATELY GENTLE. Blocks of 8 with a pause between them, because an
older controller on a shared RS-485 line can be upset by being hammered, and a
commissioning tool that faults a customer's machine has failed no matter what it
found.

USAGE
  List the serial ports:            python modbus_discover.py --ports
  Probe the line settings:          python modbus_discover.py --port COM3 --probe
  Snapshot and diff (the good bit): python modbus_discover.py --port COM3 --unit 1 --watch
  One full scan:                    python modbus_discover.py --port COM3 --unit 1 --scan

If you do not know the slave/station address, --probe tries 1..8.
"""
import argparse
import sys
import time

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    print("pymodbus is not installed.  pip install pymodbus pyserial")
    raise SystemExit(1)

# Common on Asian plastics controllers. Probed in this order.
BAUDS = (9600, 19200, 38400, 4800)
FRAMES = (("N", 1), ("E", 1), ("O", 1), ("N", 2))
BLOCK = 8          # registers per request — small, so one bad address costs little
PAUSE = 0.05       # between requests, so the bus is never hammered


def ports():
    """Print what is plugged in. Returns the device names found."""
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is not installed.  pip install pyserial")
        return []
    found = list(list_ports.comports())
    if not found:
        print("no serial ports found - is the USB-RS485 adapter plugged in?")
        return []
    for p in found:
        print(f"  {p.device:<8} {p.description}")
        if p.hwid:
            print(f"           {p.hwid}")
    return [p.device for p in found]


def client_for(port, baud, parity, stopbits, timeout=0.4):
    return ModbusSerialClient(port=port, baudrate=baud, parity=parity,
                              stopbits=stopbits, bytesize=8, timeout=timeout)


def _read(cli, kind, addr, count, unit):
    """One bounded read. Returns a list or None. Never raises."""
    try:
        fn = cli.read_holding_registers if kind == "holding" else cli.read_input_registers
        try:
            rr = fn(addr, count=count, device_id=unit)
        except TypeError:                       # older pymodbus spelling
            rr = fn(addr, count=count, slave=unit)
        if rr is None or rr.isError():
            return None
        return list(getattr(rr, "registers", []) or [])
    except Exception:                            # noqa: BLE001 - a probe never dies
        return None


def probe(port, units=range(1, 9)):
    """Find a baud/parity/address combination the controller answers on."""
    print(f"probing {port} — this tries {len(BAUDS) * len(FRAMES)} line settings "
          f"against addresses {units.start}..{units.stop - 1}\n")
    for baud in BAUDS:
        for parity, stop in FRAMES:
            cli = client_for(port, baud, parity, stop)
            if not cli.connect():
                continue
            try:
                for unit in units:
                    for kind in ("holding", "input"):
                        regs = _read(cli, kind, 0, 2, unit)
                        if regs:
                            print(f"  ANSWERED  {baud} {8}{parity}{stop}  "
                                  f"address {unit}  {kind} registers -> {regs}")
                            print(f"\n  Use:  --baud {baud} --parity {parity} "
                                  f"--stopbits {stop} --unit {unit}")
                            return baud, parity, stop, unit
            finally:
                cli.close()
            print(f"  silent    {baud} 8{parity}{stop}")
    print("\nNothing answered. Check: A/B swapped (try them the other way round), "
          "GND connected, the controller's comms actually enabled, and the "
          "station address on its setup screen.")
    return None


def snapshot(cli, unit, start, end, kind):
    """Every readable register in the range, as {address: value}."""
    out = {}
    addr = start
    while addr < end:
        count = min(BLOCK, end - addr)
        regs = _read(cli, kind, addr, count, unit)
        if regs:
            for i, value in enumerate(regs):
                out[addr + i] = value
        addr += count
        time.sleep(PAUSE)
    return out


def scan(cli, unit, start, end):
    for kind in ("holding", "input"):
        snap = snapshot(cli, unit, start, end, kind)
        live = {a: v for a, v in snap.items() if v != 0}
        print(f"\n{kind} registers {start}..{end - 1}: "
              f"{len(snap)} readable, {len(live)} non-zero")
        for addr in sorted(live):
            print(f"   {addr:>6}  {live[addr]:>10}")
        if not snap:
            print("   (nothing readable — wrong address, or this block is not supported)")


def watch(cli, unit, start, end, gap):
    """Two snapshots, and only what moved between them.

    This is the whole point of the tool. On a running machine almost nothing
    changes; the few that do are the counters and timers. Compare them against
    the HMI and you have the map.
    """
    print(f"snapshot 1 of registers {start}..{end - 1} ...")
    first = {k: snapshot(cli, unit, start, end, k) for k in ("holding", "input")}
    print(f"waiting {gap}s — leave the machine running, and note the shot count "
          f"on the HMI now")
    time.sleep(gap)
    print("snapshot 2 ...")
    second = {k: snapshot(cli, unit, start, end, k) for k in ("holding", "input")}

    moved = 0
    for kind in ("holding", "input"):
        changes = [(a, first[kind][a], second[kind][a])
                   for a in sorted(first[kind])
                   if a in second[kind] and first[kind][a] != second[kind][a]]
        if not changes:
            continue
        print(f"\n{kind} registers that MOVED:")
        print(f"   {'addr':>6}  {'before':>10}  {'after':>10}  {'delta':>8}")
        for addr, a, b in changes:
            print(f"   {addr:>6}  {a:>10}  {b:>10}  {b - a:>+8}")
            moved += 1

    print()
    if not moved:
        print("Nothing moved. Either the machine was not cycling, the gap was too "
              "short for one cycle, or the counter lives outside this range — "
              "try --start 4000 --end 4200, or a longer --gap.")
    else:
        print(f"{moved} register(s) moved. The shot counter is almost certainly one "
              f"of them: find the one whose value matches the HMI's PARTS/SHOT "
              f"count, and whose delta equals the shots made during the wait.")
        print("A 32-bit counter spans TWO registers — if one jumped oddly, read it "
              "as a pair before concluding it is wrong.")


def main():
    ap = argparse.ArgumentParser(description="Read-only Modbus RTU discovery.")
    ap.add_argument("--ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="N", choices=["N", "E", "O"])
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=200)
    ap.add_argument("--gap", type=int, default=30, help="seconds between snapshots")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()

    if a.ports:
        ports()
        return 0

    # THE SAME NO-OP serial_listen.py had: `--probe` with no `--port` printed
    # the port list and returned 0, which on a floor with one adapter in one
    # machine is the whole command quietly doing nothing while looking like a
    # completed run. With exactly one port there is nothing to choose.
    found = ports()
    port = a.port
    if not port:
        if len(found) == 1:
            port = found[0]
            print(f"\nno --port given and only {port} is present, so using it.")
        else:
            print("\nPick a port, then:  --port COM3 --probe"
                  if found else "")
            return 1

    if a.probe:
        probe(port)
        return 0

    cli = client_for(port, a.baud, a.parity, a.stopbits)
    if not cli.connect():
        print(f"could not open {port}")
        return 1
    try:
        print(f"{port} @ {a.baud} 8{a.parity}{a.stopbits}, address {a.unit}  "
              f"(READ ONLY)\n")
        if a.watch:
            watch(cli, a.unit, a.start, a.end, a.gap)
        else:
            scan(cli, a.unit, a.start, a.end)
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    main()
